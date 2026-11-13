"""Review queue: list helpers for the main screen's Review tab and the item screen."""

from __future__ import annotations

import html
import re

from rich.console import Group
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Header, Static

from ..api import ApiError
from ..editor import edit_markdown
from ..render import plain_text, render_cooked
from ..util import local_time, time_ago, truncate
from ..widgets.post_stream import PostStream
from .composer import ConfirmModal, MenuModal
from .smart_footer import SmartFooter

TYPE_LABELS = {
    "ReviewableFlaggedPost": "flagged",
    "ReviewableQueuedPost": "queued",
    "ReviewableUser": "new user",
    "ReviewablePost": "post",
    "ReviewableChatMessage": "chat",
}

# These open a penalty or deletion form on the website; we don't guess their parameters.
WEB_ONLY = ("suspend", "silence", "delete_user", "delete_and_block")


class ReviewData:
    """Index a /review.json response so items can find their users, topics, scores and actions."""

    def __init__(self, d: dict) -> None:
        self.items: list[dict] = d.get("reviewables") or []
        self.users = {u["id"]: u for u in d.get("users") or []}
        self.topics = {t["id"]: t for t in d.get("topics") or []}
        self.scores = {s["id"]: s for s in d.get("reviewable_scores") or []}
        self.score_types = {s["id"]: s for s in d.get("score_types") or []}
        self.bundles = {b["id"]: b for b in d.get("bundled_actions") or []}
        self.actions = {a["id"]: a for a in d.get("actions") or d.get("reviewable_actions") or []}
        self.notes = {n["id"]: n for n in d.get("reviewable_notes") or []}

    def notes_for(self, item: dict) -> list[dict]:
        return [self.notes[i] for i in item.get("reviewable_note_ids") or [] if i in self.notes]

    def author(self, item: dict) -> str:
        uid = item.get("target_created_by_id") or item.get("created_by_id")
        return (self.users.get(uid) or {}).get("username") or ""

    def reasons(self, item: dict) -> list[str]:
        out: list[str] = []
        for sid in item.get("reviewable_score_ids") or []:
            s = self.scores.get(sid) or {}
            st = self.score_types.get(s.get("score_type_id")) or {}
            label = st.get("title") or s.get("reason_type") or "flag"
            if label not in out:
                out.append(label)
        return out

    def title(self, item: dict) -> str:
        t = self.topics.get(item.get("topic_id")) or {}
        if t.get("title"):
            return t["title"]
        payload = item.get("payload") or {}
        if payload.get("title"):
            return payload["title"]
        return plain_text(item.get("cooked") or "")[:80] or item.get("raw", "")[:80]

    def actions_for(self, item: dict) -> list[dict]:
        out: list[dict] = []
        for bid in item.get("bundled_action_ids") or []:
            b = self.bundles.get(bid) or {}
            acts = b.get("actions")
            if acts and isinstance(acts[0], dict):
                out.extend(acts)
            else:
                out.extend(self.actions[a] for a in b.get("action_ids") or acts or [] if a in self.actions)
        return out


class ReviewItemScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "back", "Back"),
        Binding("enter,a", "act", "Act"),
        Binding("o", "open", "Browser"),
        Binding("t", "topic", "Topic"),
        Binding("n", "add_note", "Add note"),
        Binding("D", "delete_note", "Delete note"),
    ]

    def __init__(self, item: dict, data: ReviewData) -> None:
        super().__init__()
        self._item = item
        self._data = data

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="review-head"):
            yield Static(id="review-body")
        yield PostStream(self.app.site, self.app.client.base_url, int(self._item.get("topic_id") or 0), id="posts")
        yield SmartFooter()

    def on_mount(self) -> None:
        it = self._item
        self.sub_title = f"review · {TYPE_LABELS.get(it.get('type', ''), it.get('type', ''))}"
        self._show()
        self.query_one("#posts").focus()

    def _posts(self) -> list[dict]:
        """The reviewed post, then each note, shaped like topic posts so PostStream can show them."""
        it, d = self._item, self._data
        cooked = it.get("cooked") or ""
        if not cooked:
            raw = it.get("raw") or (it.get("payload") or {}).get("raw") or "(no content)"
            cooked = "<p>" + html.escape(raw).replace("\n", "<br>") + "</p>"
        posts = [{
            "id": -1,
            "post_number": 0,
            "username": d.author(it) or "?",
            "cooked": cooked,
            "created_at": it.get("target_created_at") or it.get("created_at"),
        }]
        for i, n in enumerate(d.notes_for(it), 1):
            posts.append({
                "id": n["id"],
                "post_number": i,
                "username": (n.get("user") or {}).get("username", "?"),
                "cooked": n.get("cooked") or "",
                "created_at": n.get("created_at"),
                "note": n,
            })
        return posts

    def _show(self, focus_id: int | None = None) -> None:
        it, d = self._item, self._data
        head = Text()
        head.append(d.title(it), style="bold")
        head.append(f"\nby @{d.author(it) or '?'} · {time_ago(it.get('created_at'))} ago", style="dim")
        head.append(f" · score {it.get('score', 0):g}", style="dim")
        parts: list = [head, Text("")]
        for sid in it.get("reviewable_score_ids") or []:
            s = d.scores.get(sid) or {}
            st = d.score_types.get(s.get("score_type_id")) or {}
            who = "system" if s.get("user_id", 0) < 0 else "@" + ((d.users.get(s.get("user_id")) or {}).get("username") or "?")
            line = Text(f"⚑ {st.get('title') or s.get('reason_type') or 'flag'}", style="bold #ff875f")
            line.append(f" by {who} · {local_time(s.get('created_at'))}", style="dim")
            parts.append(line)
            if s.get("reason"):
                parts.append(render_cooked(s["reason"], self.app.site, self.app.client.base_url).body)
            stats = s.get("agree_stats") or {}
            if stats:
                parts.append(Text(
                    f"  flagger history: {stats.get('agreed', 0)} agreed, {stats.get('disagreed', 0)} disagreed, {stats.get('ignored', 0)} ignored",
                    style="dim",
                ))
        acts = d.actions_for(it)
        n = len(d.notes_for(it))
        status = f"{len(acts)} action{'s' if len(acts) != 1 else ''} available · enter to act" if acts else "Already reviewed."
        status += f" · {n} note{'s' if n != 1 else ''} below" if n else " · no notes, n to add one"
        parts.append(Text(status, style="dim"))
        self.query_one("#review-body", Static).update(Group(*parts))
        posts = self._posts()
        cur = next((p["post_number"] for p in posts if p["id"] == focus_id), None) if focus_id else None
        self.query_one("#posts", PostStream).set_posts(posts, cursor_number=cur)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_open(self) -> None:
        url = self._item.get("target_url") or f"{self.app.client.base_url}/review/{self._item['id']}"
        self.app.open_url(url)

    def action_topic(self) -> None:
        from .thread import ThreadScreen

        tid = self._item.get("topic_id")
        if tid:
            t = self._data.topics.get(tid) or {"id": tid}
            m = re.search(r"/t/[^/]+/\d+/(\d+)", self._item.get("target_url") or "")
            self.app.push_screen(ThreadScreen(t, target_post=int(m.group(1)) if m else None))

    def action_act(self) -> None:
        acts = self._data.actions_for(self._item)
        if not acts:
            self.app.notify("No actions available (already reviewed?).")
            return
        items: list[tuple[str, str]] = []
        for a in acts:
            label = a.get("label") or a["id"].replace("_", " ")
            if any(w in a["id"] for w in WEB_ONLY) or a.get("client_action"):
                label += "  (opens website)"
            items.append((a["id"], label))

        def picked(aid: str | None) -> None:
            if not aid:
                return
            a = next(x for x in acts if x["id"] == aid)
            if any(w in aid for w in WEB_ONLY) or a.get("client_action"):
                self.action_open()
                return
            msg = a.get("confirm_message") or f"{a.get('label') or aid}?"

            def done(ok: bool | None) -> None:
                if ok:
                    self._perform(aid)

            self.app.push_screen(ConfirmModal(msg), done)

        self.app.push_screen(MenuModal("Review action", items), picked)

    @work(thread=True, group="review-act")
    def _perform(self, action: str) -> None:
        try:
            self.app.client.perform_review(int(self._item["id"]), action, int(self._item.get("version") or 0))
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Review")
            return
        self.app.call_from_thread(self.app.notify, "Done.")
        self.app.call_from_thread(self.app.pop_screen)

    # --- notes ---

    def action_add_note(self) -> None:
        key = f"review:{self._item['id']}"
        hint = "<!-- review note, staff only; save & quit to add -->\n\n"
        prev = self.app.unsent.pop(key, None)
        with self.app.suspend():
            content = edit_markdown(hint + prev if prev else hint)
        if content is None:
            return
        from .thread import strip_template

        body = strip_template(content).strip()
        if not body:
            self.app.notify("Empty note, not added.", severity="warning")
            return
        self._add_note(body)

    @work(thread=True, group="review-note")
    def _add_note(self, body: str) -> None:
        rid = int(self._item["id"])
        try:
            note = self.app.client.add_review_note(rid, body)
        except ApiError as e:
            self.app.unsent[f"review:{rid}"] = body
            self.app.call_from_thread(
                self.app.notify,
                f"Note failed: {e}. Your text is kept; press n again to reopen it.",
                severity="error",
                timeout=10,
            )
            return
        self.app.call_from_thread(self._note_added, note)

    def _note_added(self, note: dict) -> None:
        self._data.notes[note["id"]] = note
        self._item["reviewable_note_ids"] = [*(self._item.get("reviewable_note_ids") or []), note["id"]]
        self._show(focus_id=note["id"])
        self.app.notify("Note added.")

    def action_delete_note(self) -> None:
        cur = self.query_one("#posts", PostStream).current
        n, site = (cur.post.get("note") if cur else None), self.app.site
        if not n:
            self.app.notify("Move to a note first.")
            return
        if not site.is_admin and (n.get("user") or {}).get("username") != site.username:
            self.app.notify("You can only delete your own notes.", severity="warning")
            return
        text = truncate(plain_text(n.get("cooked") or n.get("content") or ""), 60)

        def done(ok: bool | None) -> None:
            if ok:
                self._delete_note(int(n["id"]))

        self.app.push_screen(ConfirmModal(f"Delete this note?\n{text}"), done)

    @work(thread=True, group="review-note")
    def _delete_note(self, nid: int) -> None:
        try:
            self.app.client.delete_review_note(int(self._item["id"]), nid)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Delete note")
            return
        self.app.call_from_thread(self._note_deleted, nid)

    def _note_deleted(self, nid: int) -> None:
        old = list(self._item.get("reviewable_note_ids") or [])
        pos = old.index(nid) if nid in old else 0
        ids = [i for i in old if i != nid]
        self._data.notes.pop(nid, None)
        self._item["reviewable_note_ids"] = ids
        self._show(focus_id=ids[min(pos, len(ids) - 1)] if ids else None)
        self.app.notify("Note deleted.")
