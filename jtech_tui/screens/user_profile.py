from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Header, LoadingIndicator, Markdown

from .smart_footer import SmartFooter

from ..api import ApiError


def _fmt_when(s: str) -> str:
    if not s:
        return "?"
    return s.split("T")[0]


class UserProfileScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back"),
        Binding("R", "reload", "Reload"),
    ]

    def __init__(self, username: str) -> None:
        super().__init__()
        self._username = username

    def compose(self) -> ComposeResult:
        yield Header()
        yield LoadingIndicator(id="loader")
        with VerticalScroll(id="profile-scroll"):
            yield Markdown("", id="profile")
        yield SmartFooter()

    def on_mount(self) -> None:
        self.sub_title = f"@{self._username}"
        self.query_one("#profile-scroll", VerticalScroll).display = False
        self._fetch()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        self.query_one("#loader", LoadingIndicator).display = True
        self.query_one("#profile-scroll", VerticalScroll).display = False
        self._fetch()

    @work(thread=True, exclusive=True, group="profile")
    def _fetch(self) -> None:
        try:
            data = self.app.client.user(self._username)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Load")
            return
        try:
            actions = self.app.client.user_actions(self._username)
        except ApiError:
            actions = []
        self.app.call_from_thread(self._display, data, actions)

    def _display(self, data: dict, actions: list[dict]) -> None:
        user = data.get("user") or {}
        parts: list[str] = []
        name = user.get("name") or ""
        parts.append(f"# @{user.get('username', self._username)}")
        if name:
            parts.append(f"**{name}**")
        if user.get("title"):
            parts.append(f"*{user['title']}*")
        if user.get("bio_raw"):
            parts.append("")
            parts.append(user["bio_raw"])
        parts.append("")
        parts.append("## Stats")
        stats_lines = [
            f"- **Posts**: {user.get('post_count', 0)}",
            f"- **Topics**: {user.get('topic_count', 0)}",
            f"- **Likes given**: {user.get('likes_given', '?')}",
            f"- **Likes received**: {user.get('likes_received', '?')}",
            f"- **Joined**: {_fmt_when(user.get('created_at', ''))}",
            f"- **Last seen**: {_fmt_when(user.get('last_seen_at', ''))}",
        ]
        if user.get("trust_level") is not None:
            stats_lines.append(f"- **Trust level**: {user.get('trust_level')}")
        parts.extend(stats_lines)
        parts.append("")
        parts.append("## Recent activity")
        if not actions:
            parts.append("*No recent activity available.*")
        else:
            for a in actions[:25]:
                title = a.get("title") or a.get("topic_title") or "(untitled)"
                when = _fmt_when(a.get("created_at", ""))
                excerpt = (a.get("excerpt") or "").replace("\n", " ").strip()
                if len(excerpt) > 200:
                    excerpt = excerpt[:200] + "…"
                parts.append(f"### {title}")
                parts.append(f"*{when}*")
                if excerpt:
                    parts.append("")
                    parts.append(f"> {excerpt}")
                parts.append("")

        self.query_one("#loader", LoadingIndicator).display = False
        scroll = self.query_one("#profile-scroll", VerticalScroll)
        scroll.display = True
        self.query_one("#profile", Markdown).update("\n".join(parts))
