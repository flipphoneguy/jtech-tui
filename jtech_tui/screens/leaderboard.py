from __future__ import annotations

from rich.text import Text

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Header, LoadingIndicator, Static

from ..api import Unauthorized
from .smart_footer import SmartFooter
from .user_profile import UserProfileScreen


PERIODS = ("all", "yearly", "quarterly", "monthly", "weekly", "daily")


class LeaderboardScreen(Screen):
    """Gamification-plugin leaderboard at /leaderboard/<id>.json.

    Columns: position, @username, total score. Period default is ``all``; use
    ``[`` / ``]`` to cycle (unless the board sets ``period_filter_disabled``).
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back"),
        Binding("R", "reload", "Reload"),
        Binding("]", "next_period", "Period →"),
        Binding("[", "prev_period", "Period ←"),
    ]

    def __init__(self, leaderboard_id: int | None = None, period: str = "all") -> None:
        super().__init__()
        self._period = period if period in PERIODS else "all"
        self._leaderboard_id = leaderboard_id
        self._period_filter_disabled = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Leaderboard…", id="thread-header")
        yield LoadingIndicator(id="loader")
        yield DataTable(id="tbl-lb", cursor_type="row", zebra_stripes=True)
        yield SmartFooter()

    def on_mount(self) -> None:
        t = self.query_one("#tbl-lb", DataTable)
        t.add_columns("#", "User", "Score")
        t.display = False
        self._resolve_id_and_fetch()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        self._fetch_current()

    def action_next_period(self) -> None:
        if self._period_filter_disabled:
            self.app.notify("This board has periods disabled.", severity="warning")
            return
        idx = PERIODS.index(self._period)
        self._period = PERIODS[(idx + 1) % len(PERIODS)]
        self._fetch_current()

    def action_prev_period(self) -> None:
        if self._period_filter_disabled:
            self.app.notify("This board has periods disabled.", severity="warning")
            return
        idx = PERIODS.index(self._period)
        self._period = PERIODS[(idx - 1) % len(PERIODS)]
        self._fetch_current()

    def _header_text(self, name: str) -> str:
        if self._period_filter_disabled:
            return name
        # Escape brackets so Rich/Textual doesn't interpret [/] as a markup tag.
        return f"{name} · {self._period}   (\\[/\\] to change period)"

    @work(thread=True, exclusive=True, group="lb-resolve")
    def _resolve_id_and_fetch(self) -> None:
        self.app.call_from_thread(self._show_loading)
        if self._leaderboard_id is None:
            self._leaderboard_id = self.app.client.default_leaderboard_id()
        if self._leaderboard_id is None:
            self.app.call_from_thread(
                self.app.notify,
                "No leaderboard configured on this site.",
                severity="error",
            )
            self.app.call_from_thread(self._hide_loading)
            return
        self._fetch_current()

    @work(thread=True, exclusive=True, group="lb")
    def _fetch_current(self) -> None:
        bid = self._leaderboard_id
        if bid is None:
            return
        self.app.call_from_thread(self._show_loading)
        try:
            data = self.app.client.leaderboard(bid, period=self._period)
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Load failed: {e}", severity="error"
            )
            self.app.call_from_thread(self._hide_loading)
            return
        self.app.call_from_thread(self._populate, data)

    def _show_loading(self) -> None:
        self.query_one("#loader", LoadingIndicator).display = True
        self.query_one("#tbl-lb", DataTable).display = False

    def _hide_loading(self) -> None:
        self.query_one("#loader", LoadingIndicator).display = False
        self.query_one("#tbl-lb", DataTable).display = True

    def _populate(self, data: dict) -> None:
        meta = data.get("leaderboard") or {}
        name = meta.get("name") or "Leaderboard"
        self._period_filter_disabled = bool(meta.get("period_filter_disabled"))
        self.query_one("#thread-header", Static).update(self._header_text(name))
        t = self.query_one("#tbl-lb", DataTable)
        t.clear()

        # Pinned "you" row at the top.
        personal = (data.get("personal") or {}).get("user") or {}
        you_name = personal.get("username") or ""
        if you_name:
            you_pos = str(personal.get("position") or "?")
            you_score = str(personal.get("total_score") or 0)
            t.add_row(
                Text(you_pos, style="bold cyan"),
                Text(f"@{you_name}  ★", style="bold cyan"),
                Text(you_score, style="bold cyan"),
                key=f"you:{you_name}",
            )

        users = data.get("users") or []
        seen: set[str] = set()
        fallback_rank = 0
        for u in users:
            uname = u.get("username") or ""
            if not uname or uname in seen:
                continue
            seen.add(uname)
            fallback_rank += 1
            pos = u.get("position")
            if not isinstance(pos, int):
                pos = fallback_rank
            score = u.get("total_score")
            if score is None:
                score = u.get("score", 0)
            t.add_row(str(pos), f"@{uname}", str(score), key=f"u:{uname}")

        self._hide_loading()
        self.call_after_refresh(t.focus)

    @on(DataTable.RowSelected)
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        key_obj = event.row_key
        key = key_obj.value if hasattr(key_obj, "value") else str(key_obj)
        if not key:
            return
        if key.startswith("u:"):
            uname = key[2:]
            self.app.push_screen(UserProfileScreen(uname))
        elif key.startswith("you:"):
            uname = key[4:]
            self.app.push_screen(UserProfileScreen(uname))
