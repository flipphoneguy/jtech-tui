"""Review queue: list helpers for the main screen's Review tab and the item screen."""

from __future__ import annotations

from rich.console import Group
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Header, Static

from ..api import ApiError
from ..render import plain_text, render_cooked
from ..util import local_time, time_ago
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
    ]

    def __init__(self, item: dict, data: ReviewData) -> None:
        super().__init__()
        self._item = item
        self._data = data

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="profile-scroll"):
            yield Static(id="review-body")
        yield SmartFooter()

    def on_mount(self) -> None:
        it, d = self._item, self._data
        self.sub_title = f"review · {TYPE_LABELS.get(it.get('type', ''), it.get('type', ''))}"
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
        parts += [Text(""), Text("Content", style="bold underline")]
        cooked = it.get("cooked") or ""
        if cooked:
            parts.append(render_cooked(cooked, self.app.site, self.app.client.base_url).body)
        else:
            parts.append(Text(it.get("raw") or (it.get("payload") or {}).get("raw") or "(no content)"))
        acts = d.actions_for(it)
        parts += [Text(""), Text(
            f"{len(acts)} action{'s' if len(acts) != 1 else ''} available · enter to act" if acts else "Already reviewed.",
            style="dim",
        )]
        self.query_one("#review-body", Static).update(Group(*parts))
        self.query_one("#profile-scroll").focus()

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
            self.app.push_screen(ThreadScreen(t))

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
