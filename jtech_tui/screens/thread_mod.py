"""Staff actions for the thread screen: post menu (m), topic menu (M), whispers (w) and flagging (!)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from rich.console import Group
from rich.text import Text
from textual import work

from .. import complete
from ..api import ApiError, whisper_fields
from ..editor import edit_markdown
from ..render import render_cooked
from ..util import local_time, parse_duration, parse_when, topic_id_from
from .composer import ConfirmModal, MenuModal, PromptModal, TextModal

if TYPE_CHECKING:
    from ..widgets import Entry


class Audience:
    def __init__(self) -> None:
        self.user_ids: list[int] = []
        self.group_ids: list[int] = []
        self.badge_ids: list[int] = []
        self.labels: list[str] = []
        self.errors: list[str] = []


def resolve_audience(app, text: str) -> Audience:
    """Turn ``@dov, moderators, badge:Helper`` into ids. Blocking; call from a worker."""
    out = Audience()
    groups = {g["name"].lower(): g for g in app.site.raw.get("groups") or []}
    badges: list[dict] | None = None
    for raw in text.split(","):
        tok = raw.strip()
        if not tok:
            continue
        if tok.lower().startswith("badge:"):
            name = tok[6:].strip().lower()
            if badges is None:
                try:
                    badges = app.client.badges()
                except ApiError:
                    badges = []
            hit = next((b for b in badges if b.get("name", "").lower() == name), None)
            if hit:
                out.badge_ids.append(hit["id"])
                out.labels.append(f"🏅{hit['name']}")
            else:
                out.errors.append(f"no badge '{tok[6:].strip()}'")
            continue
        bare = tok.lstrip("@")
        if not tok.startswith("@") and bare.lower() in groups:
            g = groups[bare.lower()]
            out.group_ids.append(g["id"])
            out.labels.append(g["name"])
            continue
        try:
            uid = app.client.user_id(bare)
        except ApiError:
            uid = None
        if uid:
            out.user_ids.append(uid)
            out.labels.append(f"@{bare}")
        else:
            out.errors.append(f"no user '{bare}'")
    return out


class ThreadModMixin:
    """Mixed into ThreadScreen. Relies on its _current, _data, _tid, stream, _merge, _poll."""

    # --- availability ---

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("post_menu", "topic_menu", "whisper", "check", "check_range"):
            return self.app.site.is_staff
        return True

    # --- selection ---

    _check_anchor: int | None = None

    def action_check(self) -> None:
        e = self._current()
        if e is None or e.is_small:
            return
        ids = set(self.stream.checked)
        ids ^= {e.id}
        self._check_anchor = e.id if e.id in ids else None
        self.stream.set_checked(ids)
        self._check_count(len(ids))

    def action_check_range(self) -> None:
        e = self._current()
        if e is None:
            return
        s = self.stream
        anchor = next((i for i, x in enumerate(s.entries) if x.id == self._check_anchor), None)
        lo, hi = sorted((s.cursor, s.cursor if anchor is None else anchor))
        ids = set(s.checked) | {x.id for x in s.entries[lo:hi + 1] if not x.is_small}
        self._check_anchor = e.id
        s.set_checked(ids)
        self._check_count(len(ids))

    def _check_count(self, n: int) -> None:
        if n:
            self.app.notify(f"{n} selected · m for actions · esc clears", timeout=2)

    def _checked_entries(self) -> list["Entry"]:
        return [x for x in self.stream.entries if x.id in self.stream.checked]

    def _bulk_menu(self) -> None:
        ids = sorted(self.stream.checked, key=lambda i: self._ids.index(i) if i in self._ids else 0)
        n = len(ids)
        what = f"{n} post{'s' if n != 1 else ''}"
        details = self._data.get("details") or {}
        items: list[tuple[str, str]] = []
        if details.get("can_move_posts"):
            items.append(("move", f"Move {what} to another topic…"))
        items.append(("owner", f"Change owner of {what}…"))
        items.append(("delete", f"Delete {what}"))
        e = self._current(warn=False)
        if e is not None and not e.is_small:
            items.append(("single", f"Menu for post #{e.number} only"))
        items.append(("clear", "Clear selection"))

        def done(choice: str | None) -> None:
            if choice == "move":
                self._move_ids(ids, what)
            elif choice == "owner":
                self._owner_ids(ids, what)
            elif choice == "delete":
                self._delete_ids(ids, what)
            elif choice == "single":
                self._single_post_menu()
            elif choice == "clear":
                self.stream.set_checked(set())

        self.app.push_screen(MenuModal(f"Selected · {what}", items), done)

    def _bulk_done(self) -> None:
        self.stream.set_checked(set())
        self._check_anchor = None

    def _delete_ids(self, ids: list[int], what: str) -> None:
        # The selection outlives jumps and reloads, so check by id, not by what's on screen.
        loaded = self._checked_entries()
        if (self._ids and self._ids[0] in ids) or any(x.number == 1 for x in loaded):
            self.app.notify("The first post is selected. Delete the topic from M instead.", severity="warning")
            return
        self._delete_check_worker(ids, what, [x.post for x in loaded])

    @work(thread=True, group="mod")
    def _delete_check_worker(self, ids: list[int], what: str, posts: list[dict]) -> None:
        have = {p.get("id") for p in posts}
        missing = [i for i in ids if i not in have]
        if missing:
            try:
                posts = posts + self.app.client.posts_by_ids(self._tid, missing)
            except ApiError as e:
                self.app.call_from_thread(self.app.report_error, e, "Delete")
                return
        if len(posts) < len(ids) or any(not p.get("can_delete") for p in posts):
            self.app.call_from_thread(self.app.notify, "Some selected posts can't be deleted.", severity="warning")
            return
        self.app.call_from_thread(
            self._confirm,
            f"Delete {what}?",
            lambda: self._run("Delete", lambda: self.app.client.delete_posts(ids), after="topic", on_ok=self._bulk_done),
        )

    # --- shared plumbing ---

    def _run(
        self,
        label: str,
        fn: Callable[[], object],
        after: str = "post",
        post_id: int | None = None,
        on_ok: Callable[[], None] | None = None,
    ) -> None:
        self._mod_worker(label, fn, after, post_id, on_ok)

    @work(thread=True, group="mod")
    def _mod_worker(self, label: str, fn, after: str, post_id: int | None, on_ok=None) -> None:
        self._do(label, fn, after, post_id, on_ok)

    def _do(self, label: str, fn, after: str, post_id: int | None, on_ok=None) -> None:
        """Run an API call, report it, and refresh what it changed. Call from a worker thread."""
        try:
            fn()
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, label)
            return
        self.app.call_from_thread(self.app.notify, f"{label}: done.")
        if on_ok is not None:
            self.app.call_from_thread(on_ok)
        if after == "post" and post_id:
            try:
                fresh = self.app.client.posts_by_ids(self._tid, [post_id])
            except ApiError:
                fresh = []
            if fresh:
                self.app.call_from_thread(self.stream.update_post, fresh[0])
        if after in ("topic", "post"):
            self._refresh_quiet()

    def _refresh_quiet(self) -> None:
        """Re-read the topic around the cursor and merge (worker-safe)."""
        cur = self.stream.current
        try:
            fresh = self.app.client.topic(self._tid, near_post=cur.number if cur else None, deleted=self.app.site.is_staff)
        except ApiError:
            return
        self.app.call_from_thread(self._merge, fresh)

    def _ask(
        self, title: str, then: Callable[[str], None], *, placeholder: str = "", value: str = "", hint: str = "", **suggest
    ) -> None:
        def done(v: str | None) -> None:
            if v is not None:
                then(v)

        self.app.push_screen(PromptModal(title, placeholder, value, hint, **suggest), done)

    def _confirm(self, question: str, then: Callable[[], None]) -> None:
        def done(ok: bool | None) -> None:
            if ok:
                then()

        self.app.push_screen(ConfirmModal(question), done)

    # --- post menu ---

    def action_post_menu(self) -> None:
        if self.stream.checked:
            self._bulk_menu()
        else:
            self._single_post_menu()

    def _single_post_menu(self) -> None:
        e = self._current()
        if e is None or e.is_small:
            return
        p = e.post
        d = self._data
        details = d.get("details") or {}
        items: list[tuple[str, str]] = []
        if p.get("can_edit"):
            items.append(("edit", "Edit" + ("" if p.get("yours") else " (with reason)")))
        if p.get("deleted_at"):
            if p.get("can_recover"):
                items.append(("recover", "Recover deleted post"))
        elif p.get("can_delete"):
            items.append(("delete", "Delete"))
        if self.app.site.is_staff and p.get("can_edit") and e.number > 1:
            if e.is_whisper:
                items += [
                    ("whisper_off", "Make public (no longer a whisper)"),
                    ("whisper_retarget", "Change whisper audience…"),
                    ("whisper_add", "Add someone to this whisper…"),
                    ("viewers", "Who has seen this whisper"),
                ]
            else:
                items.append(("whisper_on", "Turn into whisper…"))
        if self.app.site.is_staff and not e.is_whisper:
            if d.get("mod_topic_pinned_post_id") == e.id:
                items.append(("unpin_post", "Unpin from bottom of topic"))
            else:
                items.append(("pin_post", "Pin to bottom of topic"))
        if p.get("can_wiki"):
            items.append(("wiki", "Remove wiki" if p.get("wiki") else "Make wiki"))
        if self.app.site.is_staff:
            items.append(("owner", "Change owner…"))
        if details.get("can_move_posts"):
            items.append(("move", "Move to another topic…"))
        if p.get("can_view_edit_history") and (p.get("version") or 1) > 1:
            items.append(("history", "Edit history"))
        flags = [a for a in p.get("actions_summary") or [] if a.get("id") != 2 and a.get("count")]
        if flags or p.get("reviewable_id"):
            items.append(("flags", f"Flags on this post ({sum(a['count'] for a in flags)})"))
        items.append(("flag", "Flag…"))
        if self.app.site.is_staff and p.get("user_id"):
            items.append(("", "USER"))
            items.append(("user", f"@{p.get('username')}: info, notes, silence, suspend"))
        title = f"Post #{e.number} · @{p.get('username')}"

        def done(choice: str | None) -> None:
            if choice:
                getattr(self, f"_post_{choice}")(e)

        self.app.push_screen(MenuModal(title, items), done)

    def _post_edit(self, e: "Entry") -> None:
        self.action_edit_post()

    def _post_delete(self, e: "Entry") -> None:
        self.action_delete_post()

    def _post_recover(self, e: "Entry") -> None:
        self._run("Recover", lambda: self.app.client.recover_post(e.id), post_id=e.id)

    def _post_whisper_on(self, e: "Entry") -> None:
        prefill = f"@{e.reply_to_user}" if e.reply_to_user else ""
        self._ask(
            "Turn into whisper · to:",
            lambda text: self._convert_whisper(e, text),
            placeholder="@user, group, badge:Name (empty = staff only)",
            value=prefill,
            complete=complete.audience(self.app),
            seps=",",
        )

    def _post_whisper_retarget(self, e: "Entry") -> None:
        p = e.post
        cur = [f"@{u['username']}" for u in p.get("mod_whisper_targets") or [] if u.get("username")]
        cur += [g.get("name", "") for g in p.get("mod_whisper_target_groups") or [] if isinstance(g, dict)]
        cur += [f"badge:{b.get('name', '')}" for b in p.get("mod_whisper_target_badges") or [] if isinstance(b, dict)]
        self._ask(
            "Whisper audience · to:",
            lambda text: self._convert_whisper(e, text),
            value=", ".join(cur),
            placeholder="@user, group, badge:Name (empty = staff only)",
            complete=complete.audience(self.app),
            seps=",",
        )

    @work(thread=True, group="mod")
    def _convert_whisper(self, e: "Entry", text: str) -> None:
        aud = resolve_audience(self.app, text)
        if aud.errors:
            self.app.call_from_thread(self.app.notify, "; ".join(aud.errors), severity="error")
            return
        self._do(
            "Whisper",
            lambda: self.app.client.set_post_whisper(e.id, True, aud.user_ids, aud.group_ids, aud.badge_ids),
            "post",
            e.id,
        )

    def _post_whisper_off(self, e: "Entry") -> None:
        self._confirm(
            f"Make post #{e.number} public? Everyone who can see the topic will see it.",
            lambda: self._run("Make public", lambda: self.app.client.set_post_whisper(e.id, False), post_id=e.id),
        )

    def _post_whisper_add(self, e: "Entry") -> None:
        self._ask(
            "Add to whisper · username:",
            lambda name: name and self._run(
                "Add to whisper",
                lambda: self.app.client.add_whisper_participant(self._tid, e.id, name.lstrip("@")),
                post_id=e.id,
            ),
            complete=complete.users(self.app),
        )

    def _post_viewers(self, e: "Entry") -> None:
        self._viewers_worker(e.id)

    @work(thread=True, group="mod")
    def _viewers_worker(self, post_id: int) -> None:
        try:
            d = self.app.client.post_viewers(post_id)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Viewers")
            return
        rows = d.get("viewers") or d.get("users") or []
        t = Text()
        for v in rows:
            name = v.get("username") or v.get("name") or "?"
            when = v.get("viewed_at") or v.get("last_read_at") or ""
            t.append(f"@{name}", style="bold")
            if when:
                t.append(f"  {local_time(when)}", style="dim")
            t.append("\n")
        self.app.call_from_thread(self.app.push_screen, TextModal("Seen by", t if rows else "Nobody yet."))

    def _post_pin_post(self, e: "Entry") -> None:
        self._run(
            "Pin to bottom",
            lambda: self.app.client.update_topic_tools(self._tid, pinned_post_id=e.id),
            after="topic",
        )

    def _post_unpin_post(self, e: "Entry") -> None:
        self._run(
            "Unpin",
            lambda: self.app.client.update_topic_tools(self._tid, pinned_post_id=0),
            after="topic",
        )

    def _post_wiki(self, e: "Entry") -> None:
        on = not e.post.get("wiki")
        self._run("Wiki", lambda: self.app.client.set_wiki(e.id, on), post_id=e.id)

    def _post_owner(self, e: "Entry") -> None:
        self._owner_ids([e.id], f"post #{e.number}", post_id=e.id)

    def _owner_ids(self, ids: list[int], what: str, post_id: int | None = None) -> None:
        on_ok = None if post_id else self._bulk_done
        self._ask(
            f"Change owner of {what} · new owner username:",
            lambda name: name and self._confirm(
                f"Make @{name.lstrip('@')} the author of {what}?",
                lambda: self._run(
                    "Change owner",
                    lambda: self.app.client.change_owner(self._tid, ids, name.lstrip("@")),
                    after="post" if post_id else "topic",
                    post_id=post_id,
                    on_ok=on_ok,
                ),
            ),
            complete=complete.users(self.app),
        )

    def _post_move(self, e: "Entry") -> None:
        self._move_ids([e.id], f"post #{e.number}", single=True)

    def _move_ids(self, ids: list[int], what: str, single: bool = False) -> None:
        on_ok = None if single else self._bulk_done

        def go(target: str) -> None:
            if not target:
                return
            tid = topic_id_from(target)
            if tid:
                self._confirm(
                    f"Move {what} to topic {tid}?",
                    lambda: self._run(
                        "Move",
                        lambda: self.app.client.move_posts(self._tid, ids, destination_topic_id=tid),
                        after="topic",
                        on_ok=on_ok,
                    ),
                )
            elif target.lower().startswith("new:"):
                title = target[4:].strip()
                self._confirm(
                    f"Move {what} into a new topic “{title}”?",
                    lambda: self._run(
                        "Move", lambda: self.app.client.move_posts(
                            self._tid, ids, title=title, category_id=self._data.get("category_id")
                        ), after="topic", on_ok=on_ok,
                    ),
                )
            else:
                self.app.notify("Give a topic number/link, or new: Title", severity="warning")

        self._ask(
            f"Move {what} to:",
            go,
            placeholder="topic title, number or link, or  new: Title of new topic",
            complete=complete.topics(self.app),
            min_chars=3,
        )

    def _post_history(self, e: "Entry") -> None:
        self._history_worker(e.id, e.post.get("version") or 1)

    @work(thread=True, group="mod")
    def _history_worker(self, post_id: int, version: int) -> None:
        try:
            d = self.app.client.revision(post_id, version)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "History")
            return
        head = Text()
        head.append(f"Version {d.get('previous_revision') or version - 1} → {d.get('current_revision') or version}", style="bold")
        head.append(f"  by @{d.get('username', '?')} · {local_time(d.get('created_at'))}\n", style="dim")
        if d.get("edit_reason"):
            head.append(f"Reason: {d['edit_reason']}\n", style="italic")
        title = (d.get("title_changes") or {}).get("inline")
        body_html = (d.get("body_changes") or {}).get("inline") or ""
        parts = [head]
        if title:
            parts.append(render_cooked(title, self.app.site, self.app.client.base_url).body)
        parts.append(render_cooked(body_html, self.app.site, self.app.client.base_url).body)
        self.app.call_from_thread(self.app.push_screen, TextModal("Edit history (green added, red removed)", Group(*parts)))

    def _post_flags(self, e: "Entry") -> None:
        names = {t["id"]: t.get("name") or t.get("name_key") for t in self.app.site.flag_types}
        t = Text()
        for a in e.post.get("actions_summary") or []:
            if a.get("id") != 2 and a.get("count"):
                t.append(f"{names.get(a['id'], a['id'])}: {a['count']}\n")
        if e.post.get("reviewable_id"):
            t.append(f"\nIn the review queue (item {e.post['reviewable_id']}). Open the Review tab to act on it.", style="dim")
        self.app.push_screen(TextModal(f"Flags on post #{e.number}", t if t.plain else "No flags."))

    def _post_flag(self, e: "Entry") -> None:
        self.action_flag()

    def _post_user(self, e: "Entry") -> None:
        from .user_mod import UserModScreen

        self.app.push_screen(UserModScreen(int(e.post["user_id"]), e.post.get("username", "")))

    # --- flag (everyone) ---

    def action_flag(self) -> None:
        e = self._current()
        if e is None or e.is_small:
            return
        if e.post.get("yours"):
            self.app.notify("You can't flag your own post.", severity="warning")
            return
        can = {a["id"] for a in e.post.get("actions_summary") or [] if a.get("can_act")}
        items = [
            (str(t["id"]), t.get("name") or t.get("name_key", "").replace("_", " "))
            for t in self.app.site.flag_types
            if t["id"] in can
        ]
        if not items:
            self.app.notify("You can't flag this post (already flagged?).", severity="warning")
            return

        def picked(choice: str | None) -> None:
            if not choice:
                return
            ftype = int(choice)
            needs_msg = any(
                t["id"] == ftype and (t.get("is_custom_flag") or t.get("require_message"))
                for t in self.app.site.flag_types
            )
            if needs_msg:
                self._ask(
                    "Message (required for this flag):",
                    lambda msg: msg and self._run("Flag", lambda: self.app.client.act(e.id, ftype, msg), post_id=e.id),
                )
            else:
                self._confirm(
                    f"Flag post #{e.number} as “{dict(items)[choice]}”?",
                    lambda: self._run("Flag", lambda: self.app.client.act(e.id, ftype), post_id=e.id),
                )

        self.app.push_screen(MenuModal(f"Flag post #{e.number}", items), picked)

    # --- whisper reply ---

    def action_whisper(self) -> None:
        if not self._data:
            return
        e = self._current(warn=False)
        reply_to = e.number if e is not None and e.number > 1 and not e.is_small else None
        prefill = ""
        if e is not None and not e.post.get("yours") and not e.is_small:
            prefill = f"@{e.post.get('username')}"
        self._ask(
            "Whisper · to:",
            lambda text: self._whisper_resolve(text, reply_to),
            value=prefill,
            placeholder="@user, group, badge:Name (empty = staff only)",
            complete=complete.audience(self.app),
            seps=",",
        )

    @work(thread=True, group="mod")
    def _whisper_resolve(self, text: str, reply_to: int | None) -> None:
        aud = resolve_audience(self.app, text)
        if aud.errors:
            self.app.call_from_thread(self.app.notify, "; ".join(aud.errors), severity="error")
            return
        self.app.call_from_thread(self._whisper_compose, aud, reply_to)

    def _whisper_compose(self, aud: Audience, reply_to: int | None) -> None:
        who = ", ".join(aud.labels) or "staff only"
        hint = f"<!-- 🔒 whisper to {who}; save & quit to send -->\n\n"
        prev = self.app.unsent.pop(f"{self._tid}:w{reply_to}", None)
        with self.app.suspend():
            content = edit_markdown(hint + prev if prev else hint)
        if content is None:
            return
        from .thread import strip_template

        body = strip_template(content).strip()
        if not body:
            self.app.notify("Empty whisper, not sent.", severity="warning")
            return
        extra = whisper_fields(aud.user_ids, aud.group_ids, aud.badge_ids)
        self.app.check_mentions(body, lambda b: self._whisper_send(b, reply_to, extra))

    @work(thread=True, group="post")
    def _whisper_send(self, body: str, reply_to: int | None, extra: dict) -> None:
        try:
            post = self.app.client.reply(self._tid, body, reply_to, extra=extra)
        except ApiError as e:
            self.app.unsent[f"{self._tid}:w{reply_to}"] = body
            self.app.call_from_thread(
                self.app.notify,
                f"Whisper failed: {e}. Your text is kept; whisper again to reopen it.",
                severity="error",
                timeout=10,
            )
            return
        self.app.call_from_thread(self._replied, post)

    # --- topic menu ---

    def action_topic_menu(self) -> None:
        d = self._data
        if not d:
            return
        det = d.get("details") or {}
        items: list[tuple[str, str]] = []
        if det.get("can_close_topic"):
            items.append(("open", "Open topic") if d.get("closed") else ("close", "Close topic"))
        if det.get("can_pin_unpin_topic"):
            if d.get("pinned") or d.get("pinned_globally"):
                items.append(("unpin", "Unpin"))
            else:
                items += [("pin", "Pin in category…"), ("pin_global", "Pin globally…")]
        if det.get("can_toggle_topic_visibility"):
            items.append(("list", "List (make visible)") if d.get("visible") is False else ("unlist", "Unlist"))
        if det.get("can_archive_topic"):
            items.append(("unarchive", "Unarchive") if d.get("archived") else ("archive", "Archive"))
        if det.get("can_edit"):
            items += [("title", "Edit title…"), ("category", "Change category…"), ("tags", "Edit tags…")]
        if det.get("can_moderate_category"):
            items.append(("slow", "Slow mode…" if not d.get("slow_mode_seconds") else "Change / end slow mode…"))
        if self.app.site.is_staff:
            items += [("timer", "Set a timer…"), ("footer", "Footer message…")]
        if det.get("can_split_merge_topic"):
            items.append(("merge", "Merge into another topic…"))
        if d.get("deleted_at"):
            items.append(("recover", "Recover topic"))
        elif det.get("can_delete"):
            items.append(("delete", "Delete topic"))
        if not items:
            self.app.notify("No topic actions available.")
            return

        def done(choice: str | None) -> None:
            if choice:
                getattr(self, f"_topic_{choice}")()

        title = d.get("title") or ""
        self.app.push_screen(MenuModal(f"Topic · {title[:50]}", items), done)

    def _status(self, label: str, status: str, enabled: bool, until: str = "") -> None:
        self._run(label, lambda: self.app.client.topic_status(self._tid, status, enabled, until), after="topic")

    def _topic_close(self) -> None:
        self._status("Close", "closed", True)

    def _topic_open(self) -> None:
        self._status("Open", "closed", False)

    def _topic_archive(self) -> None:
        self._status("Archive", "archived", True)

    def _topic_unarchive(self) -> None:
        self._status("Unarchive", "archived", False)

    def _topic_unlist(self) -> None:
        self._status("Unlist", "visible", False)

    def _topic_list(self) -> None:
        self._status("List", "visible", True)

    def _topic_unpin(self) -> None:
        status = "pinned_globally" if self._data.get("pinned_globally") else "pinned"
        self._status("Unpin", status, False)

    def _pin_until(self, status: str) -> None:
        def go(v: str) -> None:
            when = parse_when(v or "1w")
            if when is None:
                self.app.notify("Use e.g. 3d, 2w, 1mo or 2026-12-01.", severity="warning")
                return
            self._status("Pin", status, True, when.isoformat())

        self._ask("Pin until:", go, placeholder="3d, 2w, 1mo, 2026-12-01 (empty = 1 week)")

    def _topic_pin(self) -> None:
        self._pin_until("pinned")

    def _topic_pin_global(self) -> None:
        self._pin_until("pinned_globally")

    def _topic_title(self) -> None:
        self._ask(
            "New title:",
            lambda t: t and self._run("Title", lambda: self.app.client.update_topic(self._tid, title=t), after="topic"),
            value=self._data.get("title") or "",
        )

    def _topic_category(self) -> None:
        site = self.app.site
        cur = self._data.get("category_id")
        items = [
            (str(cid), ("• " if cid == cur else "  ") + site.category_label(cid))
            for cid in sorted(site.categories, key=lambda c: site.category_label(c).lower())
        ]

        def done(choice: str | None) -> None:
            if choice and int(choice) != cur:
                self._run(
                    "Category", lambda: self.app.client.update_topic(self._tid, category_id=int(choice)), after="topic"
                )

        self.app.push_screen(MenuModal("Move topic to category", items), done)

    def _topic_tags(self) -> None:
        cur = [t if isinstance(t, str) else t.get("name", "") for t in self._data.get("tags") or []]
        self._ask(
            "Tags (space or comma separated):",
            lambda v: self._run(
                "Tags",
                lambda: self.app.client.update_topic(
                    self._tid, tags=[t for t in v.replace(",", " ").split() if t]
                ),
                after="topic",
            ),
            value=" ".join(cur),
            complete=complete.tags(self.app),
            seps=" ,",
        )

    def _topic_slow(self) -> None:
        def go(v: str) -> None:
            v = v.strip().lower()
            if v in ("", "0", "off"):
                self._run("Slow mode off", lambda: self.app.client.slow_mode(self._tid, 0), after="topic")
                return
            secs = parse_duration(v)
            if secs is None:
                self.app.notify("Use e.g. 10m, 1h, 1d.", severity="warning")
                return
            self._run("Slow mode", lambda: self.app.client.slow_mode(self._tid, secs), after="topic")

        self._ask("Slow mode: one post per user every…", go, placeholder="10m, 1h, 1d · off to disable")

    def _topic_timer(self) -> None:
        kinds = [
            ("close", "Close after…"),
            ("open", "Open after…"),
            ("delete", "Delete after…"),
            ("bump", "Bump after…"),
            ("delete_replies", "Delete replies older than…"),
        ]

        def picked(kind: str | None) -> None:
            if not kind:
                return

            def go(v: str) -> None:
                when = parse_when(v)
                if when is None:
                    self.app.notify("Use e.g. 12h, 3d, 2026-12-01.", severity="warning")
                    return
                self._run("Timer", lambda: self.app.client.topic_timer(self._tid, kind, when.isoformat()), after="topic")

            self._ask(f"{dict(kinds)[kind]}", go, placeholder="12h, 3d, 1w, 2026-12-01")

        self.app.push_screen(MenuModal("Topic timer", kinds), picked)

    def _topic_footer(self) -> None:
        cur = self._data.get("mod_topic_footer_message") or ""
        self._ask(
            "Footer message (empty to remove):",
            lambda v: self._run(
                "Footer message", lambda: self.app.client.update_topic_tools(self._tid, footer_message=v), after="topic"
            ),
            value=cur if isinstance(cur, str) else "",
        )

    def _topic_merge(self) -> None:
        def go(v: str) -> None:
            tid = topic_id_from(v)
            if not tid:
                self.app.notify("Give a topic number or link.", severity="warning")
                return
            self._confirm(
                f"Merge all posts of this topic into topic {tid}? This topic will be closed and emptied.",
                lambda: self._run("Merge", lambda: self.app.client.merge_topic(self._tid, tid), after="topic"),
            )

        self._ask(
            "Merge into topic:", go, placeholder="topic title, number or link", complete=complete.topics(self.app), min_chars=3
        )

    def _topic_delete(self) -> None:
        self._confirm(
            f"Delete the whole topic “{self._data.get('title', '')}”?",
            lambda: self._run("Delete topic", lambda: self.app.client.delete_topic(self._tid), after="topic"),
        )

    def _topic_recover(self) -> None:
        self._run("Recover topic", lambda: self.app.client.recover_topic(self._tid), after="topic")


def topic_banner(data: dict) -> Text | None:
    """One-line notices for staff state that isn't visible in posts (footer message, slow mode, timer)."""
    bits: list[str] = []
    if data.get("slow_mode_seconds"):
        bits.append(f"🐢 slow mode {int(data['slow_mode_seconds']) // 60}m")
    timer = data.get("topic_timer") or {}
    if timer.get("execute_at"):
        bits.append(f"⏲ {timer.get('status_type', 'timer')} at {local_time(timer['execute_at'])}")
    if data.get("pinned_until") and (data.get("pinned") or data.get("pinned_globally")):
        bits.append(f"📌 until {local_time(data['pinned_until'])}")
    footer = data.get("mod_topic_footer_message")
    if footer:
        bits.append(f"footer: {footer}")
    return Text(" · ".join(bits), style="dim") if bits else None
