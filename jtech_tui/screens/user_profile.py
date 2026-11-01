from __future__ import annotations

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Header, LoadingIndicator, Static

from ..api import ApiError
from ..render import plain_text, render_cooked
from ..util import parse_time, parse_when, time_ago, truncate
from ..widgets import VimDataTable
from .composer import MenuModal, PromptModal, TextModal
from .smart_footer import SmartFooter

SECTIONS = ("activity", "topics", "replies", "badges", "followers", "following", "bio")
SECTION_NAMES = {
    "activity": "Activity",
    "topics": "Topics",
    "replies": "Replies",
    "badges": "Badges",
    "followers": "Followers",
    "following": "Following",
    "bio": "Bio",
}


def _num(n: int | None) -> str:
    n = int(n or 0)
    return f"{n / 1000:.1f}k" if n >= 10000 else str(n)


class UserProfileScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "back", "Back"),
        Binding("f", "follow", "Follow"),
        Binding("c", "reqpm", "REQ-PM"),
        Binding("s", "status", "Status"),
        Binding("m", "moderate", "Moderate"),
        Binding("o", "open_web", "Browser"),
        Binding("y", "copy_link", "Copy link"),
        Binding("R", "reload", "Reload"),
    ]

    def __init__(self, username: str) -> None:
        super().__init__()
        self._username = username
        self._user: dict = {}
        self._summary: dict = {}
        self._section = "activity"
        self._data: dict[str, list] = {}

    @property
    def is_me(self) -> bool:
        me = self.app.site.username or self.app.cfg.username
        return bool(me) and me.lower() == self._username.lower()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "status":
            return self.is_me
        if action == "follow":
            return not self.is_me and bool(self._user.get("can_follow"))
        if action == "moderate":
            return self.app.site.is_staff and not self.is_me
        if action == "reqpm":
            return self.is_me or bool(self._user.get("reqpm_available"))
        return True

    def compose(self) -> ComposeResult:
        yield Header()
        yield LoadingIndicator(id="loader")
        yield Static(id="profile-head")
        yield Static(id="profile-sections")
        yield VimDataTable(id="tbl-profile", cursor_type="row", zebra_stripes=True)
        with VerticalScroll(id="profile-bio"):
            yield Static(id="profile-bio-body")
        yield SmartFooter()

    def on_mount(self) -> None:
        self.sub_title = f"@{self._username}"
        t = self.query_one("#tbl-profile", VimDataTable)
        t.sideways = self._switch
        self.query_one("#profile-bio").display = False
        self._fetch()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        self._data.clear()
        self._fetch()

    def action_open_web(self) -> None:
        self.app.open_url(f"{self.app.client.base_url}/u/{self._username}")

    # --- loading ---

    @work(thread=True, exclusive=True, group="profile")
    def _fetch(self) -> None:
        client = self.app.client
        try:
            user = client.user(self._username).get("user") or {}
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Load")
            return
        try:
            summary = client.user_summary(self._username)
        except ApiError:
            summary = {}
        self.app.call_from_thread(self._show, user, summary)

    def _show(self, user: dict, summary: dict) -> None:
        self._user = user
        self._summary = summary
        self.query_one("#loader").display = False
        self._render_head()
        self._load_section()
        self.refresh_bindings()
        self.query_one("#tbl-profile").focus()

    def _render_head(self) -> None:
        u = self._user
        s = (self._summary or {}).get("user_summary") or {}
        site = self.app.site
        t = Text()
        t.append(f"@{u.get('username', self._username)}", style="bold")
        if u.get("admin"):
            t.append(" ADMIN ", style="bold #1c1c1c on #ff875f")
        elif u.get("moderator"):
            t.append(" MOD ", style="bold #1c1c1c on #5fafff")
        if u.get("name"):
            t.append(f" · {u['name']}")
        if u.get("title"):
            t.append(f" · {u['title']}", style="italic")
        status = u.get("status") or u.get("user_status") or {}
        if status.get("description"):
            t.append(" · ")
            if status.get("emoji"):
                t.append_text(site.emoji.rich(status["emoji"]))
                t.append(" ")
            t.append(status["description"])
            if status.get("ends_at"):
                t.append(f" (until {time_ago(status['ends_at']) if parse_time(status['ends_at']) else status['ends_at']})", style="dim")
        line2: list[str] = []
        if u.get("location"):
            line2.append(u["location"].strip())
        if u.get("website_name"):
            line2.append(u["website_name"])
        if u.get("created_at"):
            created = parse_time(u["created_at"])
            if created:
                line2.append(f"joined {created.strftime('%b %Y')}")
        if u.get("last_seen_at"):
            line2.append(f"seen {time_ago(u['last_seen_at'])}")
        if u.get("trust_level") is not None:
            line2.append(f"TL{u['trust_level']}")
        t.append("\n" + " · ".join(line2), style="dim")
        line3: list[str] = []
        if u.get("total_followers") is not None:
            line3.append(f"{_num(u['total_followers'])} followers")
        line3.append(f"{_num(u.get('badge_count'))} badges")
        line3.append(f"{_num(s.get('post_count', u.get('post_count')))} posts")
        line3.append(f"{_num(s.get('topic_count', u.get('topic_count')))} topics")
        if s.get("likes_received") is not None:
            line3.append(f"♥ {_num(s['likes_received'])} received")
        if s.get("solved_count"):
            line3.append(f"✓ {_num(s['solved_count'])} solutions")
        if s.get("days_visited") is not None:
            line3.append(f"{_num(s['days_visited'])} days visited")
        t.append("\n" + " · ".join(line3))
        keys: list[str] = []
        if not self.is_me and u.get("can_follow"):
            keys.append("★ following · f unfollow" if u.get("is_followed") else "f follow")
        if self.check_action("reqpm", ()):
            keys.append("c REQ-PM" + (" inbox" if self.is_me else ""))
        if self.is_me:
            keys.append("s status")
        if self.check_action("moderate", ()):
            keys.append("m moderate")
        if keys:
            t.append("\n" + " · ".join(keys), style="bold #5fafff")
        self.query_one("#profile-head", Static).update(t)

    # --- sections ---

    def _render_sections(self) -> None:
        t = Text(" ")
        for s in SECTIONS:
            style = "bold reverse" if s == self._section else "dim"
            t.append(f" {SECTION_NAMES[s]} ", style=style)
            t.append(" ")
        t.append("  ← → switch", style="dim")
        self.query_one("#profile-sections", Static).update(t)

    def _switch(self, step: int) -> None:
        i = SECTIONS.index(self._section)
        self._section = SECTIONS[(i + step) % len(SECTIONS)]
        self._load_section()

    def on_key(self, event) -> None:
        # ←/→ also switch sections while the bio (not the table) is showing.
        if self._section == "bio" and event.key in ("left", "right"):
            event.stop()
            self._switch(-1 if event.key == "left" else 1)

    def _load_section(self) -> None:
        self._render_sections()
        sec = self._section
        table = self.query_one("#tbl-profile", VimDataTable)
        bio = self.query_one("#profile-bio")
        if sec == "bio":
            table.display = False
            bio.display = True
            cooked = self._user.get("bio_cooked") or ""
            body = render_cooked(cooked, self.app.site, self.app.client.base_url).body if cooked else Text("No bio.", style="dim")
            self.query_one("#profile-bio-body", Static).update(body)
            bio.focus()
            return
        bio.display = False
        table.display = True
        table.focus()
        if sec in self._data:
            self._fill(sec, self._data[sec])
            return
        if sec in ("topics", "replies", "badges"):
            self._fill(sec, self._from_summary(sec))
            return
        table.clear(columns=True)
        table.add_column("Loading…")
        self._fetch_section(sec)

    def _from_summary(self, sec: str) -> list:
        s = self._summary or {}
        us = s.get("user_summary") or {}
        topics = {t["id"]: t for t in s.get("topics") or []}
        if sec == "topics":
            return [topics[i] for i in us.get("topic_ids") or [] if i in topics] or list(topics.values())
        if sec == "replies":
            out = []
            for r in us.get("replies") or []:
                t = topics.get(r.get("topic_id")) or {}
                out.append({**r, "title": t.get("title", "")})
            return out
        badges = {b["id"]: b for b in s.get("badges") or []}
        return [{**ub, "badge": badges.get(ub.get("badge_id"), {})} for ub in us.get("badges") or []]

    @work(thread=True, exclusive=True, group="profile-section")
    def _fetch_section(self, sec: str) -> None:
        client = self.app.client
        try:
            if sec == "activity":
                data: list = client.user_actions(self._username)
            elif sec == "followers":
                data = client.followers(self._username)
            else:
                data = client.following(self._username)
        except ApiError as e:
            self.app.call_from_thread(self._section_failed, sec, e)
            return
        self.app.call_from_thread(self._fill, sec, data)

    def _section_failed(self, sec: str, e: ApiError) -> None:
        table = self.query_one("#tbl-profile", VimDataTable)
        table.clear(columns=True)
        table.add_column(SECTION_NAMES[sec])
        msg = "Hidden by this user." if e.status in (403, 404) else f"Couldn't load: {e}"
        table.add_row(Text(msg, style="dim"), key="none")

    def _fill(self, sec: str, data: list) -> None:
        self._data[sec] = data
        if sec != self._section:
            return
        table = self.query_one("#tbl-profile", VimDataTable)
        table.clear(columns=True)
        width = max(30, (self.size.width or 80) - 20)
        if sec == "activity":
            table.add_columns(" ", "Topic", "Excerpt", "When")
            for i, a in enumerate(data):
                icon = "➕" if a.get("action_type") == 4 else "↩"
                excerpt = plain_text(a.get("excerpt") or "")
                table.add_row(
                    icon,
                    truncate(a.get("title") or "", width // 2),
                    Text(truncate(excerpt, width // 2), style="dim"),
                    time_ago(a.get("created_at")),
                    key=f"a:{i}",
                )
        elif sec == "topics":
            table.add_columns("Topic", "Posts", "♥", "Created")
            for i, t in enumerate(data):
                table.add_row(
                    truncate(t.get("title", ""), width),
                    str(t.get("posts_count", "")),
                    str(t.get("like_count", "")),
                    time_ago(t.get("created_at")),
                    key=f"t:{i}",
                )
        elif sec == "replies":
            table.add_columns("Topic", "#", "♥", "When")
            for i, r in enumerate(data):
                table.add_row(
                    truncate(r.get("title", ""), width),
                    str(r.get("post_number", "")),
                    str(r.get("like_count", "")),
                    time_ago(r.get("created_at")),
                    key=f"r:{i}",
                )
        elif sec == "badges":
            table.add_columns("Badge", "Granted", "×")
            for i, ub in enumerate(data):
                b = ub.get("badge") or {}
                table.add_row(
                    truncate(b.get("name", "?"), width),
                    time_ago(ub.get("granted_at")),
                    str(ub.get("count") or 1),
                    key=f"b:{i}",
                )
        else:
            table.add_columns("User")
            for i, u in enumerate(data):
                table.add_row(f"@{u.get('username', '?')}", key=f"u:{i}")
        if not table.row_count:
            table.add_row(*([""] * (len(table.columns) - 1)), Text("Nothing here.", style="dim"), key="none")

    @on(DataTable.RowSelected, "#tbl-profile")
    def _row(self, event: DataTable.RowSelected) -> None:
        from .thread import ThreadScreen

        key = event.row_key.value or ""
        if key == "none" or ":" not in key:
            return
        kind, idx = key.split(":", 1)
        item = self._data.get(self._section, [])[int(idx)]
        if kind == "a":
            self.app.push_screen(ThreadScreen(
                {"id": item.get("topic_id"), "title": item.get("title", "")},
                target_post=item.get("post_number"),
            ))
        elif kind == "t":
            self.app.push_screen(ThreadScreen(item, target_post=1))
        elif kind == "r":
            self.app.push_screen(ThreadScreen(
                {"id": item.get("topic_id"), "title": item.get("title", "")},
                target_post=item.get("post_number"),
            ))
        elif kind == "b":
            b = item.get("badge") or {}
            body = render_cooked(b.get("description") or "", self.app.site, self.app.client.base_url).body
            self.app.push_screen(TextModal(b.get("name", "Badge"), body))
        elif kind == "u":
            self.app.push_screen(UserProfileScreen(item.get("username", "")))

    # --- actions ---

    def action_copy_link(self) -> None:
        """The highlighted row's link, or the profile's when the row has none."""
        base = self.app.client.base_url
        url = f"{base}/u/{self._username}"
        table = self.query_one("#tbl-profile", VimDataTable)
        try:
            key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value or ""
        except Exception:  # noqa: BLE001
            key = ""
        kind, _, idx = key.partition(":")
        rows = self._data.get(self._section, [])
        item = rows[int(idx)] if idx.isdigit() and int(idx) < len(rows) else None
        if item is not None:
            if kind in ("a", "r"):
                url = f"{base}/t/{item.get('slug') or '-'}/{item.get('topic_id')}/{item.get('post_number') or 1}"
            elif kind == "t":
                url = f"{base}/t/{item.get('slug') or '-'}/{item.get('id')}"
            elif kind == "b":
                b = item.get("badge") or {}
                url = f"{base}/badges/{b.get('id')}/{b.get('slug') or '-'}"
            elif kind == "u":
                url = f"{base}/u/{item.get('username', '')}"
        self.app.copy_link(url)

    def action_follow(self) -> None:
        if not self.check_action("follow", ()):
            return
        self._follow_worker(not self._user.get("is_followed"))

    @work(thread=True, group="profile-act")
    def _follow_worker(self, follow: bool) -> None:
        try:
            if follow:
                self.app.client.follow(self._username)
            else:
                self.app.client.unfollow(self._username)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Follow")
            return
        self.app.call_from_thread(self.app.notify, f"{'Following' if follow else 'Unfollowed'} @{self._username}.")
        self._fetch()

    def action_reqpm(self) -> None:
        if not self.check_action("reqpm", ()):
            return
        from .reqpm import ReqpmScreen

        self.app.push_screen(ReqpmScreen(None if self.is_me else self._username))

    def action_moderate(self) -> None:
        if not self.check_action("moderate", ()) or not self._user.get("id"):
            return
        from .user_mod import UserModScreen

        self.app.push_screen(UserModScreen(int(self._user["id"]), self._username))

    def action_status(self) -> None:
        if not self.is_me:
            return
        cur = self._user.get("status") or {}
        items = [("set", "Set status…")]
        if cur.get("description"):
            items.append(("clear", f"Clear “{cur['description']}”"))

        def picked(choice: str | None) -> None:
            if choice == "clear":
                self._status_worker(None, "", "")
            elif choice == "set":
                self._ask_status()

        self.app.push_screen(MenuModal("Your status", items), picked)

    def _ask_status(self) -> None:
        def got_text(text: str | None) -> None:
            if not text:
                return

            def got_emoji(emoji: str | None) -> None:
                if emoji is None:
                    return

                def got_until(until: str | None) -> None:
                    if until is None:
                        return
                    ends = ""
                    if until:
                        when = parse_when(until)
                        if when is None:
                            self.app.notify("Use e.g. 30m, 4h, 1d.", severity="warning")
                            return
                        ends = when.isoformat()
                    self._status_worker(text, emoji.strip(":") or "speech_balloon", ends)

                self.app.push_screen(PromptModal("Clear after (empty = never):", "30m, 4h, 1d"), got_until)

            self.app.push_screen(PromptModal("Emoji name (empty = 💬):", "e.g. wrench, laptop, palm_tree"), got_emoji)

        self.app.push_screen(PromptModal("Status text:", "e.g. Working"), got_text)

    @work(thread=True, group="profile-act")
    def _status_worker(self, text: str | None, emoji: str, ends: str) -> None:
        try:
            if text is None:
                self.app.client.clear_status()
            else:
                self.app.client.set_status(text, emoji, ends)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Status")
            return
        self.app.call_from_thread(self.app.notify, "Status cleared." if text is None else "Status set.")
        self._fetch()
