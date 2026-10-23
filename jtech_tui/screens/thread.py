from __future__ import annotations

import io
import os
import time

from rich.console import Console
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, LoadingIndicator, Static

from ..api import ApiError
from ..editor import edit_markdown
from ..util import copy_to_clipboard, truncate
from ..widgets import Entry, PostStream
from .composer import ConfirmModal, CopyMenuModal, FilePickerModal, ReactionModal, ReactorsModal
from .smart_footer import SmartFooter
from .thread_mod import ThreadModMixin, topic_banner
from .user_profile import UserProfileScreen

CHUNK = 20
# Backup poll; live updates normally arrive over MessageBus within a second.
POLL_SECONDS = 120.0
PREFETCH_FRESH = 60.0


def strip_template(text: str) -> str:
    """Drop the ``<!-- … -->`` hint lines we put at the top of editor buffers."""
    lines = [
        line for line in text.splitlines()
        if not (line.strip().startswith("<!--") and line.strip().endswith("-->"))
    ]
    return "\n".join(lines)


class ThreadScreen(ThreadModMixin, Screen):
    BINDINGS = [
        Binding("escape,q", "back", "Back"),
        Binding("r", "reply", "Reply↩post"),
        Binding("t", "reply_topic", "Reply↩topic"),
        Binding("Q", "quote_reply", "Quote"),
        Binding("m", "post_menu", "Mod post"),
        Binding("M", "topic_menu", "Mod topic"),
        Binding("w", "whisper", "Whisper"),
        Binding("exclamation_mark", "flag", "Flag"),
        Binding("plus,enter", "react", "React"),
        Binding("ctrl+r", "react", "React", show=False),
        Binding("l", "show_reactors", "Reactors"),
        Binding("y", "yank", "Copy"),
        Binding("Y", "copy_menu", "Copy…"),
        Binding("o", "open_url", "Browser"),
        Binding("p", "jump_to_parent", "Jump→reply"),
        Binding("u", "user_profile", "Profile"),
        Binding("x", "toggle_collapse", "Collapse"),
        Binding("S", "toggle_spoilers", "Spoilers"),
        Binding("E", "edit_post", "Edit"),
        Binding("D", "delete_post", "Delete"),
        Binding("a", "toggle_auto_refresh", "Auto"),
        Binding("e", "open_in_editor", "Editor"),
        Binding("U", "upload", "Upload"),
        Binding("R", "reload", "Reload"),
        Binding("g,home", "goto_top", "Top"),
        Binding("G,end", "goto_bottom", "Bottom"),
    ]

    def __init__(
        self,
        topic: dict,
        prefetched: tuple[dict, float] | None = None,
        target_post: int | None = None,
    ) -> None:
        super().__init__()
        self._topic = topic
        self._tid = int(topic.get("id", 0))
        self._prefetched = prefetched
        self._target = target_post
        self._data: dict = {}
        self._ids: list[int] = []
        self._loading: set[str] = set()
        self._auto = None
        self._seen_buf: set[int] = set()
        self._seen_timer = None
        self._live_on = False
        self._live_timer = None

    # --- setup ---

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._topic.get("title", ""), id="thread-header")
        yield LoadingIndicator(id="loader")
        yield PostStream(self.app.site, self.app.client.base_url, self._tid, id="posts")
        yield SmartFooter()

    @property
    def stream(self) -> PostStream:
        return self.query_one("#posts", PostStream)

    def on_mount(self) -> None:
        self._auto = self.set_interval(POLL_SECONDS, self._poll)
        pre = self._prefetched
        self._prefetched = None
        if pre and time.monotonic() - pre[1] < PREFETCH_FRESH and not self._target:
            self._show(pre[0], None)
        else:
            self._load(self._target)

    def on_unmount(self) -> None:
        self._flush_seen()
        self._live_stop()

    # --- live updates ---

    def _live_start(self, last_id: int | None) -> None:
        if self._live_on:
            return
        self._live_on = True
        self.app.live.subscribe(f"/topic/{self._tid}", self._on_live, last_id)

    def _live_stop(self) -> None:
        if self._live_on:
            self._live_on = False
            self.app.live.unsubscribe(f"/topic/{self._tid}", self._on_live)

    def _on_live(self, data: dict) -> None:
        # Bursts (a post plus its reactions) collapse into one refresh.
        if self._live_timer is None:
            self._live_timer = self.set_timer(0.4, self._live_fire)

    def _live_fire(self) -> None:
        self._live_timer = None
        self._poll()

    def _landing(self, data: dict, target: int | None) -> int:
        if target:
            return target
        last_read = data.get("last_read_post_number") or self._topic.get("last_read_post_number")
        highest = data.get("highest_post_number") or 0
        if not last_read:
            return 1
        return last_read + 1 if last_read < highest else last_read

    def _load(self, target: int | None = None, *, quiet: bool = False) -> None:
        if not quiet:
            self.query_one("#loader").display = True
        self._fetch(target)

    @work(thread=True, exclusive=True, group="thread")
    def _fetch(self, target: int | None) -> None:
        near = target
        if near is None:
            last = self._topic.get("last_read_post_number")
            near = last + 1 if isinstance(last, int) and last > 0 else None
        try:
            data = self.app.client.topic(self._tid, near_post=near)
        except ApiError as e:
            self.app.call_from_thread(self._load_failed, e)
            return
        self.app.call_from_thread(self._show, data, target)

    def _load_failed(self, e: Exception) -> None:
        self.query_one("#loader").display = False
        self.app.report_error(e, "Load")

    def _show(self, data: dict, target: int | None) -> None:
        self._data = data
        self.app.site.set_reactions(data.get("valid_reactions"))
        ps = data.get("post_stream") or {}
        self._ids = [i for i in ps.get("stream") or [] if isinstance(i, int)]
        s = self.stream
        s.op_username = ((data.get("details") or {}).get("created_by") or {}).get("username", "")
        s.my_username = self.app.site.username or self.app.cfg.username
        self.query_one("#loader").display = False
        posts = ps.get("posts") or []
        got = {p.get("id") for p in posts}
        idx = [i for i, pid in enumerate(self._ids) if pid in got]
        s.set_posts(
            posts,
            cursor_number=self._landing(data, target),
            more_above=bool(idx) and idx[0] > 0,
            more_below=bool(idx) and idx[-1] < len(self._ids) - 1,
        )
        s.focus()
        self._update_header()
        if self._auto is not None:
            self._live_start(data.get("message_bus_last_id"))

    # --- header ---

    def _update_header(self) -> None:
        d = self._data or self._topic
        title = d.get("fancy_title") or d.get("title") or ""
        bits: list[str] = []
        cat = self.app.site.category_label(d.get("category_id"))
        if cat:
            bits.append(cat)
        tags = [t if isinstance(t, str) else t.get("name", "") for t in d.get("tags") or []]
        if tags:
            bits.append(" ".join(f"#{t}" for t in tags))
        flags = []
        if d.get("closed"):
            flags.append("🔒 closed")
        if d.get("archived"):
            flags.append("archived")
        if d.get("has_accepted_answer"):
            flags.append("✓ solved")
        if d.get("visible") is False:
            flags.append("unlisted")
        cur = self.stream.current
        pos = f"{cur.number}/{d.get('highest_post_number') or len(self._ids)}" if cur else ""
        width = (self.size.width or 80) - 2
        t = Text(truncate(title, max(10, width - len(pos) - 2)), style="bold")
        right = Text(pos, style="dim")
        pad = max(1, width - t.cell_len - right.cell_len)
        line = Text.assemble(t, " " * pad, right)
        if self.app.site.is_staff:
            banner = topic_banner(d)
            if banner is not None:
                flags.append(banner.plain)
        sub = " · ".join(bits + flags)
        if sub:
            line.append("\n" + truncate(sub, width), style="dim")
        self.query_one("#thread-header", Static).update(line)

    def on_resize(self) -> None:
        self._update_header()

    @on(PostStream.CursorMoved)
    def _cursor_moved(self, event: PostStream.CursorMoved) -> None:
        self._update_header()

    # --- lazy loading ---

    def _loaded_span(self) -> tuple[int, int]:
        """Index range in the stream covered by loaded posts."""
        loaded = {e.id for e in self.stream.entries}
        idx = [i for i, pid in enumerate(self._ids) if pid in loaded]
        if not idx:
            return 0, -1
        return idx[0], idx[-1]

    def _update_edges(self) -> None:
        lo, hi = self._loaded_span()
        s = self.stream
        s.more_above = lo > 0
        s.more_below = hi < len(self._ids) - 1

    @on(PostStream.NeedMore)
    def _need_more(self, event: PostStream.NeedMore) -> None:
        d = event.direction
        if d in self._loading:
            return
        lo, hi = self._loaded_span()
        if d == "above":
            ids = self._ids[max(0, lo - CHUNK):lo]
        else:
            ids = self._ids[hi + 1:hi + 1 + CHUNK]
        if not ids:
            return
        self._loading.add(d)
        self._fetch_more(d, ids)

    @work(thread=True, group="more")
    def _fetch_more(self, direction: str, ids: list[int]) -> None:
        try:
            posts = self.app.client.posts_by_ids(self._tid, ids)
        except ApiError as e:
            self.app.call_from_thread(self._more_failed, direction, e)
            return
        self.app.call_from_thread(self._got_more, direction, posts)

    def _more_failed(self, direction: str, e: Exception) -> None:
        self._loading.discard(direction)
        self.app.report_error(e, "Load")

    def _got_more(self, direction: str, posts: list[dict]) -> None:
        self._loading.discard(direction)
        self.stream.add_posts(posts)
        self._update_edges()
        self._update_header()

    # --- read tracking ---

    @on(PostStream.Seen)
    def _seen(self, event: PostStream.Seen) -> None:
        self._seen_buf.update(event.numbers)
        if self._seen_timer is None:
            self._seen_timer = self.set_timer(2.0, self._flush_seen)

    def _flush_seen(self) -> None:
        self._seen_timer = None
        if self._seen_buf:
            nums = sorted(self._seen_buf)
            self._seen_buf.clear()
            self._mark_read(nums)

    @work(thread=True, group="read")
    def _mark_read(self, numbers: list[int]) -> None:
        try:
            self.app.client.mark_read(self._tid, numbers)
        except ApiError:
            pass

    # --- navigation ---

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        cur = self.stream.current
        self._load(cur.number if cur else None)

    def action_goto_top(self) -> None:
        s = self.stream
        if self._ids and s.entries and s.entries[0].id == self._ids[0]:
            s.move_to(0, align="top")
        else:
            self._load(1)

    def action_goto_bottom(self) -> None:
        s = self.stream
        if self._ids and s.entries and s.entries[-1].id == self._ids[-1]:
            s.move_to(len(s.entries) - 1, align="top")
        else:
            self._load(self._data.get("highest_post_number") or 10**6)

    def action_jump_to_parent(self) -> None:
        e = self._current()
        if e is None:
            return
        rpn = e.post.get("reply_to_post_number")
        if not rpn:
            self.app.notify("This post isn't a reply.")
            return
        self.jump_to(rpn)

    def jump_to(self, number: int) -> None:
        idx = self.stream.index_of_number(number)
        if idx is not None:
            self.stream.move_to(idx, align="top")
        else:
            self._load(number)

    def action_toggle_collapse(self) -> None:
        e = self._current()
        if e is not None and not e.is_small:
            e.collapsed = not e.collapsed
            self.stream.refresh_entry(e)

    def action_toggle_spoilers(self) -> None:
        e = self._current()
        if e is None:
            return
        r = e.rendered
        if r is not None and not (r.has_spoiler or r.has_details):
            self.app.notify("No spoilers or collapsed sections in this post.")
            return
        e.reveal = not e.reveal
        e.expand = e.reveal
        self.stream.refresh_entry(e)

    # --- helpers ---

    def _current(self, warn: bool = True) -> Entry | None:
        e = self.stream.current if self._data else None
        if e is None and warn:
            self.app.notify("Select a post first.", severity="warning")
        return e

    def _post_url(self, number: int = 1) -> str:
        base = self.app.client.base_url
        slug = self._data.get("slug") or self._topic.get("slug") or "-"
        return f"{base}/t/{slug}/{self._tid}/{number}"

    # --- composing ---

    def _compose_and_send(self, template: str, reply_to: int | None, *, key: str) -> None:
        unsent = getattr(self.app, "unsent", {})
        self.app.unsent = unsent
        prev = unsent.pop(key, None)
        with self.app.suspend():
            content = edit_markdown(prev or template)
        if content is None:
            return
        body = strip_template(content).strip()
        if not body:
            self.app.notify("Empty reply, not posted.", severity="warning")
            return
        self._submit_reply(body, reply_to, key)

    def action_reply(self) -> None:
        if not self._data:
            return
        e = self._current(warn=False)
        reply_to = e.number if e is not None and e.number > 1 and not e.is_small else None
        if reply_to:
            hint = f"<!-- replying to @{e.post.get('username', '')} (post #{reply_to}); save & quit to send -->\n\n"
        else:
            hint = "<!-- reply to the topic; save & quit to send -->\n\n"
        self._compose_and_send(hint, reply_to, key=f"{self._tid}:{reply_to}")

    def action_reply_topic(self) -> None:
        if not self._data:
            return
        self._compose_and_send(
            "<!-- reply to the topic; save & quit to send -->\n\n", None, key=f"{self._tid}:None"
        )

    def action_quote_reply(self) -> None:
        e = self._current()
        if e is None:
            return
        self._quote_worker(e.post)

    @work(thread=True, group="raw")
    def _quote_worker(self, post: dict) -> None:
        try:
            raw = self.app.client.post_raw(int(post["id"]))
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Quote")
            return
        self.app.call_from_thread(self._open_quote, post, raw)

    def _open_quote(self, post: dict, raw: str) -> None:
        user = post.get("username", "")
        pn = post.get("post_number")
        block = f'[quote="{user}, post:{pn}, topic:{self._tid}"]\n{raw.strip()}\n[/quote]\n\n'
        hint = f"<!-- quoting @{user} (post #{pn}); save & quit to send -->\n\n"
        reply_to = pn if isinstance(pn, int) and pn > 1 else None
        self._compose_and_send(hint + block, reply_to, key=f"{self._tid}:q{pn}")

    @work(thread=True, group="post")
    def _submit_reply(self, body: str, reply_to: int | None, key: str) -> None:
        try:
            post = self.app.client.reply(self._tid, body, reply_to)
        except ApiError as e:
            self.app.unsent[key] = body
            self.app.call_from_thread(
                self.app.notify,
                f"Reply failed: {e}. Your text is kept; reply again to reopen it.",
                severity="error",
                timeout=10,
            )
            return
        self.app.call_from_thread(self._replied, post)

    def _replied(self, post: dict) -> None:
        self.app.notify("Reply posted.")
        pid = post.get("id")
        if isinstance(pid, int) and pid not in self._ids:
            self._ids.append(pid)
        s = self.stream
        if s.entries and self._ids and len(self._ids) >= 2 and s.entries[-1].id == self._ids[-2]:
            s.add_posts([post])
            self._update_edges()
            s.move_to(len(s.entries) - 1)
        else:
            self._load(post.get("post_number"))

    # --- copy / open ---

    def action_yank(self) -> None:
        e = self._current()
        if e is not None:
            self._yank_worker(int(e.id))

    @work(thread=True, group="raw")
    def _yank_worker(self, post_id: int) -> None:
        try:
            raw = self.app.client.post_raw(post_id)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Copy")
            return
        ok = copy_to_clipboard(raw)
        self.app.call_from_thread(
            self.app.notify,
            "Post markdown copied." if ok else "No clipboard tool found.",
            severity="information" if ok else "warning",
        )

    def action_copy_menu(self) -> None:
        e = self._current()
        if e is None:
            return
        url = self._post_url(e.number)
        r = e.rendered
        blocks = r.code_blocks if r else []
        links = r.links if r else []
        if not blocks and not links:
            self._copy(url, f"Copied: {url}")
            return

        def done(choice: tuple[str, int] | None) -> None:
            if not choice:
                return
            kind, i = choice
            if kind == "link":
                self._copy(url, f"Copied: {url}")
            elif kind == "code" and 0 <= i < len(blocks):
                self._copy(blocks[i], f"Code block {i + 1} copied.")
            elif kind == "url" and 0 <= i < len(links):
                self._copy(links[i][1], f"Copied: {links[i][1]}")

        self.app.push_screen(CopyMenuModal(blocks, links), done)

    def _copy(self, text: str, msg: str) -> None:
        if copy_to_clipboard(text):
            self.app.notify(msg)
        else:
            self.app.notify("No clipboard tool found.", severity="warning")

    def action_open_url(self) -> None:
        e = self._current(warn=False)
        self.app.open_url(self._post_url(e.number if e else 1))

    def action_open_in_editor(self) -> None:
        s = self.stream
        if not s.entries:
            return
        con = Console(width=100, record=True, file=io.StringIO(), color_system=None)
        con.print(f"# {self._data.get('title', '')}\n")
        for e in s.entries:
            p = e.post
            con.print(f"--- @{p.get('username')} · #{e.number} · {p.get('created_at', '')[:16]}")
            if e.rendered is not None:
                con.print(e.rendered.body)
            con.print()
        with self.app.suspend():
            edit_markdown(con.export_text(), read_only=True)

    # --- people / reactions ---

    def action_user_profile(self) -> None:
        e = self._current()
        if e is not None and e.post.get("username"):
            self.app.push_screen(UserProfileScreen(e.post["username"]))

    def action_react(self) -> None:
        e = self._current()
        if e is None or e.is_small:
            return
        if e.post.get("yours"):
            self.app.notify("You can't react to your own post.", severity="warning")
            return
        mine = e.post.get("current_user_reaction") or {}
        mine_id = mine.get("id") if isinstance(mine, dict) else None
        site = self.app.site

        def done(rid: str | None) -> None:
            if rid:
                self._toggle_reaction(e.id, rid)

        self.app.push_screen(ReactionModal(site.reactions, site.emoji, mine_id), done)

    @work(thread=True, group="react")
    def _toggle_reaction(self, post_id: int, reaction: str) -> None:
        client = self.app.client
        try:
            res = client.toggle_reaction(post_id, reaction)
            if not (isinstance(res, dict) and "reactions" in res):
                fresh = client.posts_by_ids(self._tid, [post_id])
                res = fresh[0] if fresh else {}
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Reaction")
            return
        self.app.call_from_thread(self._apply_post_fields, post_id, res)

    def _apply_post_fields(self, post_id: int, fields: dict) -> None:
        for e in self.stream.entries:
            if e.id == post_id:
                keep = (
                    "reactions", "current_user_reaction", "reaction_users_count",
                    "current_user_used_main_reaction", "actions_summary",
                )
                updated = dict(e.post)
                for k in keep:
                    if k in fields:
                        updated[k] = fields[k]
                self.stream.update_post(updated)
                return

    def action_show_reactors(self) -> None:
        e = self._current()
        if e is not None:
            self._reactors_worker(e.id)

    @work(thread=True, exclusive=True, group="reactors")
    def _reactors_worker(self, post_id: int) -> None:
        try:
            groups = self.app.client.reaction_users(post_id)
        except ApiError:
            groups = []
        if not groups:
            try:
                users = self.app.client.like_users(post_id)
            except ApiError:
                users = []
            if users:
                groups = [("+1", users)]
        self.app.call_from_thread(self._show_reactors, groups)

    def _show_reactors(self, groups: list[tuple[str, list[str]]]) -> None:
        if not groups:
            self.app.notify("No reactions on this post.")
            return
        self.app.push_screen(ReactorsModal(groups, self.app.site.emoji))

    # --- edit / delete ---

    def action_edit_post(self) -> None:
        e = self._current()
        if e is None:
            return
        if not e.post.get("can_edit"):
            self.app.notify("You can't edit this post.", severity="warning")
            return
        self._edit_worker(e.post)

    @work(thread=True, group="raw")
    def _edit_worker(self, post: dict) -> None:
        try:
            raw = self.app.client.post_raw(int(post["id"]))
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Edit")
            return
        self.app.call_from_thread(self._open_edit, post, raw)

    def _open_edit(self, post: dict, raw: str) -> None:
        mine = post.get("username") == (self.app.site.username or self.app.cfg.username)
        who = "your post" if mine else f"@{post.get('username')}'s post"
        hint = f"<!-- editing {who} #{post.get('post_number')}; save & quit to update -->\n"
        if not mine:
            hint += "<!-- edit reason (optional): put it on the next line after 'reason:' -->\nreason: \n"
        with self.app.suspend():
            content = edit_markdown(hint + "\n" + raw)
        if content is None:
            return
        body = strip_template(content)
        reason = ""
        lines = body.splitlines()
        if lines and lines[0].startswith("reason:"):
            reason = lines[0][7:].strip()
            body = "\n".join(lines[1:])
        body = body.strip()
        if not body:
            self.app.notify("Empty content, not updated.", severity="warning")
            return
        if body == raw.strip():
            self.app.notify("No changes.")
            return
        self._submit_edit(int(post["id"]), body, reason)

    @work(thread=True, group="post")
    def _submit_edit(self, post_id: int, body: str, reason: str) -> None:
        try:
            res = self.app.client.edit_post(post_id, body, reason)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Edit")
            return
        post = res.get("post") if isinstance(res, dict) else None
        self.app.call_from_thread(self.app.notify, "Post updated.")
        if post:
            self.app.call_from_thread(self._apply_full_post, post)

    def _apply_full_post(self, post: dict) -> None:
        for e in self.stream.entries:
            if e.id == post.get("id"):
                self.stream.update_post({**e.post, **post})
                return

    def action_delete_post(self) -> None:
        e = self._current()
        if e is None:
            return
        if not e.post.get("can_delete"):
            self.app.notify("You can't delete this post.", severity="warning")
            return
        mine = e.post.get("username") == (self.app.site.username or self.app.cfg.username)
        what = f"your post #{e.number}" if mine else f"@{e.post.get('username')}'s post #{e.number}"

        def done(ok: bool | None) -> None:
            if ok:
                self._delete_worker(e.id)

        self.app.push_screen(ConfirmModal(f"Delete {what}?"), done)

    @work(thread=True, group="post")
    def _delete_worker(self, post_id: int) -> None:
        try:
            self.app.client.delete_post(post_id)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Delete")
            return
        self.app.call_from_thread(self.app.notify, "Post deleted.")
        self.app.call_from_thread(self._poll)

    # --- live refresh ---

    def action_toggle_auto_refresh(self) -> None:
        if self._auto is not None:
            self._auto.stop()
            self._auto = None
            self._live_stop()
            self.app.notify("Live updates off.")
        else:
            self._auto = self.set_interval(POLL_SECONDS, self._poll)
            self._live_start(self._data.get("message_bus_last_id"))
            self._poll()
            self.app.notify("Live updates on.")

    def _poll(self) -> None:
        s = self.stream
        if not self._data or not s.entries:
            return
        self._poll_worker(s.entries[-1].number)

    @work(thread=True, exclusive=True, group="poll")
    def _poll_worker(self, near: int) -> None:
        try:
            fresh = self.app.client.topic(self._tid, near_post=near)
        except ApiError:
            return
        self.app.call_from_thread(self._merge, fresh)

    def _merge(self, fresh: dict) -> None:
        s = self.stream
        ps = fresh.get("post_stream") or {}
        new_ids = [i for i in ps.get("stream") or [] if isinstance(i, int)]
        if not new_ids:
            return
        was_at_end = bool(s.entries) and bool(self._ids) and s.entries[-1].id == self._ids[-1]
        old_ids = set(self._ids)
        self._ids = new_ids
        keep = set(new_ids)
        gone = {e.id for e in s.entries if e.id not in keep}
        s.remove_ids(gone)
        by_id = {e.id: e for e in s.entries}
        changed = 0
        added: list[dict] = []
        for p in ps.get("posts") or []:
            pid = p.get("id")
            e = by_id.get(pid)
            if e is not None:
                if (
                    p.get("updated_at") != e.post.get("updated_at")
                    or p.get("reactions") != e.post.get("reactions")
                    or p.get("reply_count") != e.post.get("reply_count")
                ):
                    s.update_post(p)
                    changed += 1
            elif was_at_end and pid not in old_ids:
                added.append(p)
        s.add_posts(added)
        # Small action posts ("closed this topic") aren't worth a notification.
        n = len([p for p in added if p.get("post_type") != 3])
        self._data = {**self._data, **{k: v for k, v in fresh.items() if k != "post_stream"}}
        self._update_edges()
        self._update_header()
        fresh_new = 0 if added else len([i for i in new_ids if i not in old_ids])
        if n:
            self.app.notify(f"{n} new post{'s' if n > 1 else ''}.")
        elif fresh_new and not was_at_end:
            self.app.notify(f"{fresh_new} new post{'s' if fresh_new > 1 else ''} at the end (G).")
        elif gone:
            self.app.notify(f"{len(gone)} post{'s' if len(gone) > 1 else ''} removed.")

    # --- upload ---

    def action_upload(self) -> None:
        def done(path: str | None) -> None:
            if path:
                self._upload_worker(path)

        self.app.push_screen(FilePickerModal("Upload to forum"), done)

    @work(thread=True, group="upload")
    def _upload_worker(self, path: str) -> None:
        upload_and_copy(self.app, path)


def upload_and_copy(app, path: str) -> None:
    """Upload a file and put its markdown on the clipboard. Runs in a worker."""
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        app.call_from_thread(app.notify, f"No such file: {expanded}", severity="error")
        return
    try:
        res = app.client.upload_file(expanded)
    except ApiError as e:
        app.call_from_thread(app.report_error, e, "Upload")
        return
    url = res.get("short_url") or res.get("url") or ""
    name = res.get("original_filename") or os.path.basename(expanded)
    is_image = (res.get("extension") or "").lower() in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "svg")
    md = f"![{name}]({url})" if is_image else f"[{name}|attachment]({url})"
    copy_to_clipboard(md)
    app.call_from_thread(app.notify, f"Uploaded. Markdown copied: {md}")
