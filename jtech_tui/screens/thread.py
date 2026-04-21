from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timezone
from html.parser import HTMLParser

from textual import on, work
from textual.message import Message
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import (
    Header,
    ListItem,
    ListView,
    LoadingIndicator,
    Markdown,
    Static,
)

from .smart_footer import SmartFooter

from ..api import Unauthorized
from ..editor import edit_markdown
from .composer import (
    ConfirmModal,
    CopyMenuModal,
    FilePickerModal,
    ReactionModal,
    ReactorsModal,
)
from .user_profile import UserProfileScreen


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("br", "p", "div", "li"):
            self.parts.append("\n")


def _strip_html(s: str) -> str:
    p = _HTMLTextExtractor()
    try:
        p.feed(s)
    except Exception:  # noqa: BLE001
        return s
    return re.sub(r"\n{3,}", "\n\n", "".join(p.parts))


_REACTION_EMOJI = {
    "heart": "❤️",
    "+1": "👍",
    "-1": "👎",
    "laughing": "😆",
    "open_mouth": "😮",
    "cry": "😢",
    "clap": "👏",
    "hugs": "🤗",
    "confetti_ball": "🎉",
    "thinking": "🤔",
    "rocket": "🚀",
    "eyes": "👀",
}


def _reaction_summary(post: dict) -> str:
    reactions = post.get("reactions") or []
    if reactions:
        parts = []
        for r in reactions:
            rid = r.get("id") or ""
            count = r.get("count") or 0
            if not count:
                continue
            emoji = _REACTION_EMOJI.get(rid, f":{rid}:")
            parts.append(f"{emoji} {count}")
        return "  ".join(parts)
    actions = post.get("actions_summary") or []
    if isinstance(actions, list):
        for a in actions:
            if a.get("id") == 2 and a.get("count"):
                return f"❤ {a['count']}"
    return ""


_QUOTE_RE = re.compile(r"\[quote=([^\]]*)\]([\s\S]*?)\[/quote\]")


def _render_quotes(raw: str) -> str:
    """Convert Discourse `[quote=...]...[/quote]` blocks to markdown blockquotes."""
    def repl(m: re.Match[str]) -> str:
        attrs = m.group(1)
        body = m.group(2).strip()
        user_match = re.search(r"\"?([\w.\-]+)", attrs)
        user = user_match.group(1) if user_match else ""
        quoted = "\n".join(f"> {line}" if line else ">" for line in body.splitlines())
        header = f"> **@{user} wrote:**" if user else "> **Quoted:**"
        return f"{header}\n>\n{quoted}\n"

    prev = None
    out = raw
    while out != prev:
        prev = out
        out = _QUOTE_RE.sub(repl, out)
    return out


_COLLAPSE_THRESHOLD = 40


def _code_blocks(raw: str) -> list[str]:
    """Return fenced code block contents from markdown source."""
    out: list[str] = []
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            buf: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith(fence):
                buf.append(lines[i])
                i += 1
            out.append("\n".join(buf))
        i += 1
    return out


def _post_markdown(
    post: dict,
    *,
    reply_to_username: str | None = None,
    collapsed: bool = False,
) -> str:
    body = post.get("raw") or _strip_html(post.get("cooked", ""))
    body = _render_quotes(body)
    who = post.get("username") or "?"
    when = _time_ago(post.get("created_at", ""))
    header = f"**@{who}** · {when}"
    pn = post.get("post_number")
    if isinstance(pn, int):
        header = f"{header}   —   **#{pn}**"
    summary = _reaction_summary(post)
    if summary:
        header = f"{header}   —   {summary}"
    breadcrumb = ""
    rpn = post.get("reply_to_post_number")
    if isinstance(rpn, int) and rpn > 0:
        target = reply_to_username or (post.get("reply_to_user") or {}).get("username") or ""
        who_bit = f"@{target} " if target else ""
        breadcrumb = f"> ↱ replying to {who_bit}#{rpn}\n\n"
    if collapsed:
        lines = body.splitlines()
        preview = "\n".join(lines[:6])
        body = f"{preview}\n\n*…collapsed ({len(lines)} lines). Press `x` to expand.*"
    return f"{header}\n\n{breadcrumb}{body}"


def _time_ago(s: str) -> str:
    """Convert an ISO timestamp to a short relative string like 3h, 2d, 1mo."""
    if not s:
        return ""
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    secs = int((datetime.now(timezone.utc) - t).total_seconds())
    if secs < 60:
        return "now"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    days = secs // 86400
    if days < 7:
        return f"{days}d"
    if days < 30:
        return f"{days // 7}w"
    if days < 365:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort clipboard set across Termux/macOS/Linux."""
    for cmd in (
        ["termux-clipboard-set"],
        ["pbcopy"],
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "-ib"],
    ):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True, timeout=5)
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


def _thread_markdown(thread: dict) -> str:
    posts = ((thread.get("post_stream") or {}).get("posts") or [])
    title = thread.get("title") or thread.get("fancy_title") or ""
    sep = "\n\n---\n\n"
    body = sep.join(_post_markdown(p) for p in posts)
    return f"# {title}\n\n{body}"


class PostItem(ListItem):
    """A single post inside the thread view."""

    def __init__(
        self,
        post: dict,
        *,
        reply_to_username: str | None = None,
        collapsed: bool = False,
    ) -> None:
        super().__init__()
        self.post = post
        self.reply_to_username = reply_to_username
        self.collapsed = collapsed

    def compose(self) -> ComposeResult:
        yield Markdown(self._md())

    def _md(self) -> str:
        return _post_markdown(
            self.post,
            reply_to_username=self.reply_to_username,
            collapsed=self.collapsed,
        )

    def refresh_body(self) -> None:
        try:
            self.query_one(Markdown).update(self._md())
        except Exception:  # noqa: BLE001
            pass

    def toggle_collapse(self) -> None:
        self.collapsed = not self.collapsed
        self.refresh_body()


class PostsList(ListView):
    """ListView with vim-style bindings and scroll-within-post for long posts.

    Emits ``NeedMore`` when the cursor tries to move past the first or last
    loaded post so the parent screen can lazy-load the next chunk.
    """

    class NeedMore(Message):
        """Emitted when the user navigates past the end/start of loaded posts."""
        def __init__(self, posts_list: "PostsList", direction: str) -> None:
            super().__init__()
            self.posts_list = posts_list
            self.direction = direction  # "above" | "below"

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "goto_top", "Top", show=False),
        Binding("G", "goto_bottom", "Bottom", show=False),
    ]

    def action_goto_top(self) -> None:
        if len(self.children):
            self.index = 0

    def action_goto_bottom(self) -> None:
        n = len(self.children)
        if n:
            self.index = n - 1

    def _scroll_step(self) -> int:
        return max(1, self.size.height - 2)

    def action_cursor_down(self) -> None:
        highlighted = self.highlighted_child
        if highlighted is not None:
            region = highlighted.virtual_region
            if region.y + region.height > self.scroll_y + self.size.height:
                self.scroll_relative(y=self._scroll_step(), animate=False)
                return
        n = len(self._nodes)
        if n and isinstance(self.index, int) and self.index >= n - 1:
            self.post_message(self.NeedMore(self, "below"))
            return
        super().action_cursor_down()

    def action_cursor_up(self) -> None:
        # Clamp a stale index before anything else (can happen after merge/remove).
        n = len(self._nodes)
        if isinstance(self.index, int) and n > 0 and self.index >= n:
            self.index = n - 1
        highlighted = self.highlighted_child
        if highlighted is not None:
            region = highlighted.virtual_region
            if region.y < self.scroll_y:
                self.scroll_relative(y=-self._scroll_step(), animate=False)
                return
        if self.index == 0:
            self.post_message(self.NeedMore(self, "above"))
            return
        super().action_cursor_up()


class ThreadScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back"),
        Binding("r", "reply", "Reply"),
        Binding("Q", "quote_reply", "Quote"),
        Binding("y", "yank", "Copy"),
        Binding("u", "user_profile", "Profile"),
        Binding("E", "edit_post", "Edit"),
        Binding("D", "delete_post", "Delete"),
        Binding("a", "toggle_auto_refresh", "Auto"),
        Binding("U", "upload", "Upload"),
        Binding("e", "open_in_editor", "Editor"),
        Binding("+", "react", "React"),
        Binding("ctrl+r", "react", "React", show=False),
        Binding("R", "reload", "Reload"),
        Binding("p", "jump_to_parent", "Jump→reply"),
        Binding("l", "show_reactors", "Reactors"),
        Binding("x", "toggle_collapse", "Collapse"),
        Binding("Y", "copy_menu", "Copy…"),
        Binding("g", "goto_top", "Top"),
        Binding("G", "goto_bottom", "Bottom"),
    ]

    def __init__(self, topic: dict) -> None:
        super().__init__()
        self._topic = topic
        self._topic_id = int(topic.get("id", 0))
        self._thread: dict | None = None
        self._auto_refresh = None  # Timer handle
        self._stream: list[int] = []       # all post_ids in order (from server)
        self._loaded_ids: set[int] = set() # post_ids currently in the list widget
        self._loading_above = False
        self._loading_below = False

    def on_unmount(self) -> None:
        if self._auto_refresh is not None:
            self._auto_refresh.stop()
            self._auto_refresh = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._topic.get("title", ""), id="thread-header")
        yield LoadingIndicator(id="loader")
        yield PostsList(id="posts")
        yield SmartFooter()

    def on_mount(self) -> None:
        self.query_one("#posts", PostsList).display = False
        self._load()

    def _load(self) -> None:
        self.query_one("#loader", LoadingIndicator).display = True
        self.query_one("#posts", PostsList).display = False
        self._fetch()

    @work(thread=True, exclusive=True, group="thread")
    def _fetch(self) -> None:
        # Open the thread near the last-read post so we land in the right place.
        last_read = self._topic.get("last_read_post_number")
        near_post = last_read if isinstance(last_read, int) and last_read > 1 else None
        try:
            thread = self.app.client.thread(self._topic_id, near_post=near_post)
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Load failed: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self._display_thread, thread)
        # Posts beyond the initial chunk are loaded lazily on scroll.

    def _display_thread(self, thread: dict) -> None:
        self._thread = thread
        # Record the full ordered list of post_ids so we can lazy-load chunks.
        self._stream = [
            pid for pid in ((thread.get("post_stream") or {}).get("stream") or [])
            if isinstance(pid, int)
        ]
        self._loaded_ids.clear()
        self.query_one("#loader", LoadingIndicator).display = False
        posts_list = self.query_one("#posts", PostsList)
        posts_list.display = True
        posts_list.clear()
        posts = ((thread.get("post_stream") or {}).get("posts") or [])
        by_number: dict[int, dict] = {}
        for p in posts:
            pn = p.get("post_number")
            if isinstance(pn, int):
                by_number[pn] = p
        for p in posts:
            rpn = p.get("reply_to_post_number")
            parent_user = None
            if isinstance(rpn, int):
                parent = by_number.get(rpn)
                if parent:
                    parent_user = parent.get("username")
            raw = p.get("raw") or _strip_html(p.get("cooked", ""))
            long_post = len(raw.splitlines()) > _COLLAPSE_THRESHOLD
            item = PostItem(p, reply_to_username=parent_user, collapsed=long_post)
            posts_list.append(item)
            pid = p.get("id")
            if isinstance(pid, int):
                self._loaded_ids.add(pid)
        # Resume behavior: open at first unread if there's unread content,
        # otherwise at the last post we've seen, mirroring Discourse web.
        # Never-opened threads (last_read == 0/None) start at the top.
        start_idx = 0
        last_read = thread.get("last_read_post_number") or self._topic.get(
            "last_read_post_number"
        )
        if isinstance(last_read, int) and last_read > 0:
            target_pn = last_read
            for p in posts:
                pn = p.get("post_number")
                if isinstance(pn, int) and pn > last_read:
                    target_pn = pn
                    break
            for i, p in enumerate(posts):
                if p.get("post_number") == target_pn:
                    start_idx = i
                    break
        if posts:
            self.call_after_refresh(self._finish_initial_load, start_idx)

    def _finish_initial_load(self, start_idx: int) -> None:
        posts_list = self.query_one("#posts", PostsList)
        if posts_list.children and 0 <= start_idx < len(posts_list.children):
            posts_list.index = start_idx
        posts_list.focus()
        # Markdown widgets measure themselves lazily; virtual_region.height is 0
        # until after layout settles.  Retry each frame until it's ready.
        if start_idx > 0:
            self.call_after_refresh(self._do_initial_scroll, start_idx, 0)
        pns = [
            item.post.get("post_number")
            for item in posts_list.children
            if isinstance(item, PostItem) and isinstance(item.post.get("post_number"), int)
        ]
        if pns:
            self._track_read(pns)

    def _do_initial_scroll(self, start_idx: int, attempts: int) -> None:
        try:
            posts_list = self.query_one("#posts", PostsList)
        except Exception:  # noqa: BLE001
            return
        n = len(posts_list.children)
        if not n or start_idx >= n:
            return
        item = posts_list.children[start_idx]
        if item.virtual_region.height > 0:
            item.scroll_visible(animate=False)
        elif attempts < 20:
            self.call_after_refresh(self._do_initial_scroll, start_idx, attempts + 1)

    @work(thread=True, exclusive=False, group="read-track")
    def _track_read(self, post_numbers: list[int]) -> None:
        """Fire-and-forget: tell the server these posts were read."""
        try:
            self.app.client.mark_read(self._topic_id, post_numbers)
        except Exception:  # noqa: BLE001
            pass

    # --- lazy post loading ---

    def _ids_to_load_below(self, n: int = 20) -> list[int]:
        if not self._loaded_ids:
            return self._stream[:n] if self._stream else []
        last_pos = -1
        for i, pid in enumerate(self._stream):
            if pid in self._loaded_ids:
                last_pos = i
        if last_pos < 0 or last_pos >= len(self._stream) - 1:
            return []
        end = min(last_pos + 1 + n, len(self._stream))
        return [pid for pid in self._stream[last_pos + 1: end] if pid not in self._loaded_ids]

    def _ids_to_load_above(self, n: int = 20) -> list[int]:
        if not self._loaded_ids:
            return []
        first_pos = len(self._stream)
        for i, pid in enumerate(self._stream):
            if pid in self._loaded_ids:
                first_pos = i
                break
        if first_pos == 0:
            return []
        start = max(0, first_pos - n)
        return [pid for pid in self._stream[start: first_pos] if pid not in self._loaded_ids]

    @on(PostsList.NeedMore)
    def _on_need_more(self, event: PostsList.NeedMore) -> None:
        if event.direction == "below":
            if not self._loading_below and self._ids_to_load_below():
                self._loading_below = True
                self._load_more_below()
        elif event.direction == "above":
            if not self._loading_above and self._ids_to_load_above():
                self._loading_above = True
                self._load_more_above()

    @work(thread=True, exclusive=True, group="thread-below")
    def _load_more_below(self) -> None:
        ids = self._ids_to_load_below()
        if not ids:
            self._loading_below = False
            return
        try:
            posts = self.app.client.thread_fill_missing(self._topic_id, ids)
        except Exception:  # noqa: BLE001
            self._loading_below = False
            return
        if posts:
            self.app.call_from_thread(self._append_posts_below, posts)
        else:
            self._loading_below = False

    def _append_posts_below(self, posts: list[dict]) -> None:
        self._insert_posts(posts)
        self._loading_below = False
        pns = [p.get("post_number") for p in posts if isinstance(p.get("post_number"), int)]
        if pns:
            self._track_read(pns)

    @work(thread=True, exclusive=True, group="thread-above")
    def _load_more_above(self) -> None:
        ids = self._ids_to_load_above()
        if not ids:
            self._loading_above = False
            return
        try:
            posts = self.app.client.thread_fill_missing(self._topic_id, ids)
        except Exception:  # noqa: BLE001
            self._loading_above = False
            return
        if posts:
            self.app.call_from_thread(self._prepend_posts_above, posts)
        else:
            self._loading_above = False

    def _prepend_posts_above(self, posts: list[dict]) -> None:
        posts_list = self.query_one("#posts", PostsList)
        saved_idx = posts_list.index
        n_inserted = self._insert_posts(posts)
        # All inserted posts are above the previous first item, so shift the cursor.
        if isinstance(saved_idx, int) and n_inserted > 0:
            posts_list.index = saved_idx + n_inserted
        self._loading_above = False
        pns = [p.get("post_number") for p in posts if isinstance(p.get("post_number"), int)]
        if pns:
            self._track_read(pns)

    def _insert_posts(self, posts: list[dict]) -> int:
        """Insert posts into the list at correct post_number positions.

        Returns the number of new items actually inserted (skips duplicates).
        """
        posts_list = self.query_one("#posts", PostsList)
        sorted_posts = sorted(posts, key=lambda p: p.get("post_number") or 0)
        # Build a by_number map covering both existing and incoming posts.
        by_number: dict[int, dict] = {}
        for item in posts_list.children:
            if isinstance(item, PostItem):
                pn = item.post.get("post_number")
                if isinstance(pn, int):
                    by_number[pn] = item.post
        for p in sorted_posts:
            pn = p.get("post_number")
            if isinstance(pn, int):
                by_number[pn] = p
        existing_ordered = [c for c in posts_list.children if isinstance(c, PostItem)]
        ei = 0
        n_inserted = 0
        for p in sorted_posts:
            pid = p.get("id")
            if isinstance(pid, int) and pid in self._loaded_ids:
                continue
            rpn = p.get("reply_to_post_number")
            parent_user = None
            if isinstance(rpn, int):
                parent = by_number.get(rpn)
                if parent:
                    parent_user = parent.get("username")
            raw = p.get("raw") or _strip_html(p.get("cooked", ""))
            collapsed = len(raw.splitlines()) > _COLLAPSE_THRESHOLD
            target_pn = p.get("post_number") or 0
            while ei < len(existing_ordered):
                cpn = existing_ordered[ei].post.get("post_number") or 0
                if cpn > target_pn:
                    break
                ei += 1
            item = PostItem(p, reply_to_username=parent_user, collapsed=collapsed)
            try:
                if ei < len(existing_ordered):
                    posts_list.mount(item, before=existing_ordered[ei])
                else:
                    posts_list.append(item)
            except Exception:  # noqa: BLE001
                try:
                    posts_list.append(item)
                except Exception:  # noqa: BLE001
                    continue
            if isinstance(pid, int):
                self._loaded_ids.add(pid)
            existing_ordered.insert(ei, item)
            ei += 1
            n_inserted += 1
        return n_inserted

    # --- actions ---
    def action_back(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        self._load()

    def action_open_in_editor(self) -> None:
        if not self._thread:
            return
        posts_list = self.query_one("#posts", PostsList)
        title = self._thread.get("title") or self._thread.get("fancy_title") or ""
        all_posts = [item.post for item in posts_list.children if isinstance(item, PostItem)]
        sep = "\n\n---\n\n"
        body = sep.join(_post_markdown(p) for p in all_posts)
        content = f"# {title}\n\n{body}"
        with self.app.suspend():
            edit_markdown(content, read_only=True)
        self.refresh()

    def action_reply(self) -> None:
        if not self._thread:
            return
        # If a post is highlighted, reply threads under it; otherwise reply to topic.
        posts_list = self.query_one("#posts", PostsList)
        item = posts_list.highlighted_child
        reply_to_post_number: int | None = None
        header_hint = ""
        if isinstance(item, PostItem):
            pn = item.post.get("post_number")
            if isinstance(pn, int) and pn > 1:
                reply_to_post_number = pn
                who = item.post.get("username", "")
                header_hint = f"<!-- replying to @{who} (post #{pn}); save & quit to send -->\n\n"
        tmpl = header_hint or "<!-- write your reply in markdown; save & quit to post -->\n\n"
        with self.app.suspend():
            content = edit_markdown(tmpl)
        self.refresh()
        if not content:
            return
        body = _strip_template(content).strip()
        if not body:
            self.app.notify("Empty reply — not posted.", severity="warning")
            return
        self._submit_reply(body, reply_to_post_number)

    def _highlighted_post(self) -> dict | None:
        posts_list = self.query_one("#posts", PostsList)
        item = posts_list.highlighted_child
        if isinstance(item, PostItem):
            return item.post
        return None

    def action_yank(self) -> None:
        post = self._highlighted_post()
        if not post:
            self.app.notify("Select a post first.", severity="warning")
            return
        body = post.get("raw") or _strip_html(post.get("cooked", ""))
        if _copy_to_clipboard(body):
            self.app.notify("Post copied to clipboard.", severity="information")
        else:
            self.app.notify(
                "No clipboard tool found (install termux-api / xclip / pbcopy).",
                severity="warning",
            )

    def action_goto_top(self) -> None:
        posts_list = self.query_one("#posts", PostsList)
        if posts_list.children:
            posts_list.index = 0
            posts_list.children[0].scroll_visible()

    def action_goto_bottom(self) -> None:
        posts_list = self.query_one("#posts", PostsList)
        n = len(posts_list.children)
        if n:
            posts_list.index = n - 1
            posts_list.children[n - 1].scroll_visible()

    def action_toggle_collapse(self) -> None:
        posts_list = self.query_one("#posts", PostsList)
        item = posts_list.highlighted_child
        if isinstance(item, PostItem):
            item.toggle_collapse()

    def action_jump_to_parent(self) -> None:
        post = self._highlighted_post()
        if not post:
            self.app.notify("Select a post first.", severity="warning")
            return
        rpn = post.get("reply_to_post_number")
        if not isinstance(rpn, int) or rpn <= 0:
            self.app.notify("This post isn't a reply.", severity="information")
            return
        posts_list = self.query_one("#posts", PostsList)
        for i, child in enumerate(posts_list.children):
            if isinstance(child, PostItem) and child.post.get("post_number") == rpn:
                posts_list.index = i
                child.scroll_visible()
                return
        self.app.notify(f"Post #{rpn} isn't loaded in this view.", severity="warning")

    def action_show_reactors(self) -> None:
        post = self._highlighted_post()
        if not post:
            self.app.notify("Select a post first.", severity="warning")
            return
        pid = post.get("id")
        if not isinstance(pid, int):
            return
        self._fetch_reactors(pid, post)

    @work(thread=True, exclusive=True, group="reactors")
    def _fetch_reactors(self, post_id: int, post: dict) -> None:
        groups: list[tuple[str, list[str]]] = []
        for r in post.get("reactions") or []:
            rid = r.get("id") or ""
            if not rid:
                continue
            users = self.app.client.reaction_users(post_id, rid)
            groups.append((rid, users))
        if not groups:
            for a in post.get("actions_summary") or []:
                if a.get("id") == 2 and a.get("count"):
                    users = self.app.client.post_action_users(post_id, 2)
                    groups.append(("+1", users))
                    break
        self.app.call_from_thread(self._open_reactors_modal, groups)

    def _open_reactors_modal(self, groups: list[tuple[str, list[str]]]) -> None:
        if not groups:
            self.app.notify("No reactions on this post.", severity="information")
            return
        self.app.push_screen(ReactorsModal(groups))

    def action_copy_menu(self) -> None:
        post = self._highlighted_post()
        if not post:
            self.app.notify("Select a post first.", severity="warning")
            return
        raw = post.get("raw") or _strip_html(post.get("cooked", ""))
        blocks = _code_blocks(raw)
        if not blocks:
            self._copy_post_link(post)
            return

        def _done(choice: tuple[str, int] | None) -> None:
            if not choice:
                return
            kind, idx = choice
            if kind == "link":
                self._copy_post_link(post)
            elif kind == "code" and 0 <= idx < len(blocks):
                if _copy_to_clipboard(blocks[idx]):
                    self.app.notify(
                        f"Code block #{idx + 1} copied.", severity="information"
                    )
                else:
                    self.app.notify(
                        "No clipboard tool found.", severity="warning"
                    )

        self.app.push_screen(CopyMenuModal(blocks), _done)

    def _copy_post_link(self, post: dict) -> None:
        base = (self.app.client.base_url or "").rstrip("/")
        slug = (self._thread or {}).get("slug") or self._topic.get("slug") or ""
        tid = self._topic_id
        pn = post.get("post_number") or 1
        if slug:
            url = f"{base}/t/{slug}/{tid}/{pn}"
        else:
            url = f"{base}/t/{tid}/{pn}"
        if _copy_to_clipboard(url):
            self.app.notify(f"Copied: {url}", severity="information")
        else:
            self.app.notify(
                "No clipboard tool found (install termux-api / xclip / pbcopy).",
                severity="warning",
            )

    def action_quote_reply(self) -> None:
        if not self._thread:
            return
        post = self._highlighted_post()
        if not post:
            self.app.notify("Select a post first.", severity="warning")
            return
        pn = post.get("post_number")
        user = post.get("username", "")
        body = post.get("raw") or _strip_html(post.get("cooked", ""))
        tid = post.get("topic_id") or self._topic_id
        quote_block = f'[quote="{user}, post:{pn}, topic:{tid}"]\n{body.strip()}\n[/quote]\n\n'
        tmpl = f"<!-- quoting @{user} (post #{pn}); save & quit to post -->\n\n{quote_block}"
        with self.app.suspend():
            content = edit_markdown(tmpl)
        self.refresh()
        if not content:
            return
        stripped = _strip_template(content).strip()
        if not stripped:
            self.app.notify("Empty reply — not posted.", severity="warning")
            return
        reply_to = pn if isinstance(pn, int) and pn > 1 else None
        self._submit_reply(stripped, reply_to)

    # --- profile / edit / delete / auto / upload ---
    def action_user_profile(self) -> None:
        post = self._highlighted_post()
        if not post:
            self.app.notify("Select a post first.", severity="warning")
            return
        username = post.get("username")
        if not username:
            return
        self.app.push_screen(UserProfileScreen(username))

    def action_edit_post(self) -> None:
        post = self._highlighted_post()
        if not post:
            self.app.notify("Select a post first.", severity="warning")
            return
        current = self.app.client._username or self.app.cfg.username or ""
        if not current or post.get("username") != current:
            self.app.notify("You can only edit your own posts.", severity="warning")
            return
        raw = post.get("raw") or self.app.client.post_raw(int(post.get("id") or 0))
        if not raw:
            raw = _strip_html(post.get("cooked", ""))
        tmpl = (
            f"<!-- editing your post #{post.get('post_number')}; "
            f"save & quit to update -->\n\n{raw}"
        )
        with self.app.suspend():
            content = edit_markdown(tmpl)
        self.refresh()
        if not content:
            return
        stripped = _strip_template(content).strip()
        if not stripped:
            self.app.notify("Empty content — not updated.", severity="warning")
            return
        pid = post.get("id")
        if isinstance(pid, int):
            self._submit_edit(pid, stripped)

    @work(thread=True, exclusive=True, group="post")
    def _submit_edit(self, post_id: int, body: str) -> None:
        try:
            self.app.client.edit_post(post_id, body)
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Edit failed: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self.app.notify, "Post updated.", severity="information")
        self.app.call_from_thread(self._refresh_in_place)

    def action_delete_post(self) -> None:
        post = self._highlighted_post()
        if not post:
            self.app.notify("Select a post first.", severity="warning")
            return
        current = self.app.client._username or self.app.cfg.username or ""
        if not current or post.get("username") != current:
            self.app.notify("You can only delete your own posts.", severity="warning")
            return
        pid = post.get("id")
        pn = post.get("post_number")
        if not isinstance(pid, int):
            return

        def _confirm(ok: bool | None) -> None:
            if ok:
                self._submit_delete(pid)

        self.app.push_screen(ConfirmModal(f"Delete your post #{pn}?"), _confirm)

    @work(thread=True, exclusive=True, group="post")
    def _submit_delete(self, post_id: int) -> None:
        try:
            self.app.client.delete_post(post_id)
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Delete failed: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self.app.notify, "Post deleted.", severity="information")
        self.app.call_from_thread(self._refresh_in_place)

    def action_toggle_auto_refresh(self) -> None:
        if self._auto_refresh is not None:
            self._auto_refresh.stop()
            self._auto_refresh = None
            self.app.notify("Auto-refresh off.", severity="information")
            return
        self._auto_refresh = self.set_interval(30.0, self._poll_new_posts)
        self.app.notify("Auto-refresh on (30s).", severity="information")

    @work(thread=True, exclusive=True, group="poll")
    def _poll_new_posts(self) -> None:
        try:
            fresh = self.app.client.thread(self._topic_id)
        except Exception:  # noqa: BLE001
            return
        self.app.call_from_thread(self._merge_new_posts, fresh)

    def _merge_new_posts(self, fresh: dict, *, notify: bool = True) -> None:
        """Merge a fresh thread response into the current list without scrolling.

        Inserts new posts at their correct ``post_number`` position and updates
        edited ones in place. Only removes a post if its id is absent from the
        server's full stream (meaning it was truly deleted); posts that are
        simply not in this partial response are left alone.
        """
        # Full set of post_ids the server knows about (the whole stream).
        fresh_stream_set: set[int] = set(
            pid for pid in ((fresh.get("post_stream") or {}).get("stream") or [])
            if isinstance(pid, int)
        )
        if fresh_stream_set:
            self._stream = [
                pid for pid in ((fresh.get("post_stream") or {}).get("stream") or [])
                if isinstance(pid, int)
            ]
        posts_list = self.query_one("#posts", PostsList)
        existing: dict[int, PostItem] = {}
        for item in posts_list.children:
            if isinstance(item, PostItem):
                pid = item.post.get("id")
                if isinstance(pid, int):
                    existing[pid] = item
        fresh_posts = ((fresh.get("post_stream") or {}).get("posts") or [])
        by_number: dict[int, dict] = {}
        for p in fresh_posts:
            pn = p.get("post_number")
            if isinstance(pn, int):
                by_number[pn] = p
        fresh_ids: set[int] = set()
        to_insert: list[tuple[int, PostItem]] = []
        updated = 0
        for p in fresh_posts:
            pid = p.get("id")
            if not isinstance(pid, int):
                continue
            fresh_ids.add(pid)
            rpn = p.get("reply_to_post_number")
            parent_user = None
            if isinstance(rpn, int):
                parent = by_number.get(rpn)
                if parent:
                    parent_user = parent.get("username")
            if pid in existing:
                item = existing[pid]
                if (
                    p.get("updated_at") != item.post.get("updated_at")
                    or p.get("cooked") != item.post.get("cooked")
                    or p.get("raw") != item.post.get("raw")
                ):
                    item.post = p
                    item.reply_to_username = parent_user
                    item.refresh_body()
                    updated += 1
                else:
                    item.post = p
                    item.reply_to_username = parent_user
            else:
                raw = p.get("raw") or _strip_html(p.get("cooked", ""))
                collapsed = len(raw.splitlines()) > _COLLAPSE_THRESHOLD
                new_item = PostItem(
                    p, reply_to_username=parent_user, collapsed=collapsed
                )
                to_insert.append((p.get("post_number") or 0, new_item))
        appended = 0
        if to_insert:
            to_insert.sort(key=lambda t: t[0])
            existing_ordered = [
                c for c in posts_list.children if isinstance(c, PostItem)
            ]
            ei = 0
            for target_pn, new_item in to_insert:
                while ei < len(existing_ordered):
                    cpn = existing_ordered[ei].post.get("post_number") or 0
                    if cpn > target_pn:
                        break
                    ei += 1
                try:
                    if ei < len(existing_ordered):
                        posts_list.mount(new_item, before=existing_ordered[ei])
                    else:
                        posts_list.append(new_item)
                except Exception:  # noqa: BLE001
                    try:
                        posts_list.append(new_item)
                    except Exception:  # noqa: BLE001
                        continue
                pid_new = new_item.post.get("id")
                if isinstance(pid_new, int):
                    self._loaded_ids.add(pid_new)
                appended += 1
        removed = 0
        for pid, item in list(existing.items()):
            # Only remove if the post is gone from the server stream entirely.
            if pid not in fresh_ids and pid not in fresh_stream_set:
                try:
                    item.remove()
                except Exception:  # noqa: BLE001
                    continue
                self._loaded_ids.discard(pid)
                removed += 1
        self._thread = fresh
        if notify and (appended or updated or removed):
            parts: list[str] = []
            if appended:
                parts.append(f"+{appended}")
            if updated:
                parts.append(f"~{updated}")
            if removed:
                parts.append(f"-{removed}")
            self.app.notify(f"Thread updated ({' '.join(parts)}).", severity="information")

    @work(thread=True, exclusive=True, group="thread")
    def _refresh_in_place(self) -> None:
        """Refresh the thread without clearing the list or scrolling."""
        try:
            fresh = self.app.client.thread(self._topic_id)
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Refresh failed: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self._merge_new_posts, fresh, notify=False)

    def action_upload(self) -> None:
        def _done(path: str | None) -> None:
            if path:
                self._do_upload(path)
        self.app.push_screen(FilePickerModal("Upload to forum"), _done)

    @work(thread=True, exclusive=True, group="upload")
    def _do_upload(self, path: str) -> None:
        import os
        expanded = os.path.expanduser(path)
        if not os.path.isfile(expanded):
            self.app.call_from_thread(
                self.app.notify, f"No such file: {expanded}", severity="error"
            )
            return
        try:
            result = self.app.client.upload(expanded)
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Upload failed: {e}", severity="error"
            )
            return
        url = result.get("short_url") or result.get("url") or ""
        name = result.get("original_filename") or os.path.basename(expanded)
        is_image = (result.get("extension") or "").lower() in (
            "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"
        )
        md = f"![{name}]({url})" if is_image else f"[{name}]({url})"
        _copy_to_clipboard(md)
        self.app.call_from_thread(
            self.app.notify,
            f"Uploaded. Markdown copied: {md}",
            severity="information",
        )

    @work(thread=True, exclusive=True, group="post")
    def _submit_reply(self, body: str, reply_to_post_number: int | None) -> None:
        try:
            self.app.client.reply(self._topic_id, body, reply_to_post_number)
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Reply failed: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self.app.notify, "Reply posted.", severity="information")
        self.app.call_from_thread(self._refresh_after_reply)

    def _refresh_after_reply(self) -> None:
        """After posting a reply: update stream, load the new post, jump to end."""
        self._do_post_reply_refresh()

    @work(thread=True, exclusive=True, group="post-reply-refresh")
    def _do_post_reply_refresh(self) -> None:
        try:
            fresh = self.app.client.thread(self._topic_id)
        except Exception:  # noqa: BLE001
            return
        # Get new stream so we can detect and fetch the reply we just posted.
        new_stream = [
            pid for pid in ((fresh.get("post_stream") or {}).get("stream") or [])
            if isinstance(pid, int)
        ]
        extra_posts: list[dict] = []
        if new_stream:
            loaded_set = set(self._loaded_ids)
            last_pos = -1
            for i, pid in enumerate(new_stream):
                if pid in loaded_set:
                    last_pos = i
            if last_pos < len(new_stream) - 1:
                ids = [
                    pid for pid in new_stream[last_pos + 1: last_pos + 21]
                    if pid not in loaded_set
                ]
                if ids:
                    try:
                        extra_posts = self.app.client.thread_fill_missing(
                            self._topic_id, ids
                        )
                    except Exception:  # noqa: BLE001
                        pass
        self.app.call_from_thread(self._apply_post_reply_refresh, fresh, extra_posts)

    def _apply_post_reply_refresh(self, fresh: dict, extra_posts: list[dict]) -> None:
        self._merge_new_posts(fresh, notify=False)
        if extra_posts:
            self._insert_posts(extra_posts)
        def _tail() -> None:
            try:
                ps = self.query_one("#posts", PostsList)
                if len(ps.children):
                    ps.index = len(ps.children) - 1
            except Exception:  # noqa: BLE001
                pass
        self.call_after_refresh(_tail)

    # --- reactions ---
    def action_react(self) -> None:
        post_id = self._highlighted_post_id()
        if not post_id:
            self.app.notify("Select a post first.", severity="warning")
            return
        self._open_react_modal(post_id)

    @work(thread=True, exclusive=True, group="react-opts")
    def _open_react_modal(self, post_id: int) -> None:
        try:
            supported = self.app.client.supported_reactions()
        except Exception:  # noqa: BLE001
            supported = []
        self.app.call_from_thread(self._show_react_modal, post_id, supported)

    def _show_react_modal(self, post_id: int, supported: list[str]) -> None:
        def _done(reaction: str | None) -> None:
            if reaction:
                self._toggle_reaction(post_id, reaction)

        self.app.push_screen(ReactionModal(supported_ids=supported), _done)

    @on(ListView.Selected, "#posts")
    def _on_post_selected(self, event: ListView.Selected) -> None:
        # Enter on a post opens the reaction picker.
        self.action_react()

    def _highlighted_post_id(self) -> int | None:
        if not self._thread:
            return None
        posts_list = self.query_one("#posts", PostsList)
        item = posts_list.highlighted_child
        if isinstance(item, PostItem):
            pid = item.post.get("id")
            try:
                return int(pid) if pid is not None else None
            except (TypeError, ValueError):
                return None
        return None

    @work(thread=True, exclusive=True, group="react")
    def _toggle_reaction(self, post_id: int, reaction_id: str) -> None:
        try:
            self.app.client.toggle_reaction(post_id, reaction_id)
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Reaction failed: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self._apply_reaction_locally, post_id, reaction_id)
        self.app.call_from_thread(
            self.app.notify, f"Toggled {reaction_id}.", severity="information"
        )

    def _apply_reaction_locally(self, post_id: int, reaction_id: str) -> None:
        """Optimistically update the in-memory post so the count changes without a reload."""
        posts_list = self.query_one("#posts", PostsList)
        for item in posts_list.children:
            if not isinstance(item, PostItem):
                continue
            if item.post.get("id") != post_id:
                continue
            post = item.post
            if reaction_id in ("+1", "heart", "like", "thumbsup", "thumbs_up"):
                actions = list(post.get("actions_summary") or [])
                entry = next((a for a in actions if a.get("id") == 2), None)
                if entry is None:
                    actions.append({"id": 2, "count": 1, "can_act": False})
                else:
                    current = entry.get("count") or 0
                    if entry.get("can_act", True):
                        entry["count"] = current + 1
                        entry["can_act"] = False
                    else:
                        entry["count"] = max(0, current - 1)
                        entry["can_act"] = True
                post["actions_summary"] = actions
            else:
                reactions = list(post.get("reactions") or [])
                entry = next((r for r in reactions if r.get("id") == reaction_id), None)
                if entry is None:
                    reactions.append({"id": reaction_id, "count": 1})
                else:
                    entry["count"] = (entry.get("count") or 0) + 1
                post["reactions"] = reactions
            item.refresh_body()
            break


def _strip_template(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.strip().startswith("<!--") and line.strip().endswith("-->"):
            continue
        lines.append(line)
    return "\n".join(lines)
