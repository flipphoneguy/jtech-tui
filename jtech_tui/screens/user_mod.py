from __future__ import annotations

from rich.console import Group
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Header, LoadingIndicator, Static

from ..api import ApiError
from ..render import render_cooked
from ..util import local_time, parse_when, time_ago
from .composer import ConfirmModal, PromptModal
from .smart_footer import SmartFooter


class UserModScreen(Screen):
    """Staff view of one user: state, penalties, notes, and silence/suspend actions."""

    BINDINGS = [
        Binding("escape,q", "back", "Back"),
        Binding("n", "add_note", "Add note"),
        Binding("s", "silence", "Silence"),
        Binding("S", "suspend", "Suspend"),
        Binding("L", "lift", "Lift penalty"),
        Binding("p", "profile", "Profile"),
        Binding("R", "reload", "Reload"),
    ]

    def __init__(self, user_id: int, username: str) -> None:
        super().__init__()
        self._uid = user_id
        self._username = username
        self._user: dict = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield LoadingIndicator(id="loader")
        with VerticalScroll(id="profile-scroll"):
            yield Static(id="usermod-body")
        yield SmartFooter()

    def on_mount(self) -> None:
        self.sub_title = f"moderate @{self._username}"
        self._fetch()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        self._fetch()

    def action_profile(self) -> None:
        from .user_profile import UserProfileScreen

        self.app.push_screen(UserProfileScreen(self._username))

    @work(thread=True, exclusive=True, group="usermod")
    def _fetch(self) -> None:
        client = self.app.client
        try:
            user = client.admin_user(self._uid)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Load")
            return
        try:
            notes = client.user_notes(self._uid)
        except ApiError:
            notes = []
        self.app.call_from_thread(self._show, user, notes)

    def _show(self, u: dict, notes: list[dict]) -> None:
        self._user = u
        self.query_one("#loader").display = False
        t = Text()
        t.append(f"@{u.get('username', self._username)}", style="bold")
        if u.get("title"):
            t.append(f"  {u['title']}", style="italic")
        t.append(f"\nTL{u.get('trust_level', '?')}", style="bold")
        if u.get("moderator"):
            t.append(" · moderator")
        if u.get("admin"):
            t.append(" · admin")
        if not u.get("active"):
            t.append(" · not activated", style="yellow")
        if u.get("staged"):
            t.append(" · staged", style="yellow")
        t.append(f"\njoined {local_time(u.get('created_at'))} · last seen {time_ago(u.get('last_seen_at'))} ago")
        t.append(f"\n{u.get('post_count', 0)} posts · {u.get('topic_count', 0)} topics · {u.get('days_visited', 0)} days visited")
        t.append("\n\n")
        if u.get("silenced_till"):
            t.append(f"🔇 SILENCED until {local_time(u['silenced_till'])}", style="bold #ff875f")
            if u.get("silence_reason"):
                t.append(f"\n   reason: {u['silence_reason']}")
            t.append("\n")
        if u.get("suspended_till"):
            t.append(f"⛔ SUSPENDED until {local_time(u['suspended_till'])}", style="bold #ff5f5f")
            if u.get("suspend_reason"):
                t.append(f"\n   reason: {u['suspend_reason']}")
            t.append("\n")
        pc = u.get("penalty_counts") or {}
        t.append(
            f"Penalties: {pc.get('silenced', 0)} silenced, {pc.get('suspended', 0)} suspended · "
            f"flags received {u.get('flags_received_count', 0)} · given {u.get('flags_given_count', 0)} · "
            f"warnings {u.get('warnings_received_count', 0)}\n"
        )
        t.append(f"\nStaff notes ({len(notes)})", style="bold underline")
        parts: list = [t]
        if not notes:
            parts.append(Text("none", style="dim"))
        for n in notes:
            head = Text(f"@{(n.get('created_by') or {}).get('username', '?')} · {local_time(n.get('created_at'))}", style="dim")
            if n.get("post_id"):
                head.append(f" · on post {n['post_id']}", style="dim")
            body = render_cooked(n.get("cooked") or "", self.app.site, self.app.client.base_url).body
            parts += [head, body, Text("")]
        self.query_one("#usermod-body", Static).update(Group(*parts))

    # --- actions ---

    def _ask(self, title: str, then, placeholder: str = "") -> None:
        def done(v: str | None) -> None:
            if v is not None:
                then(v)

        self.app.push_screen(PromptModal(title, placeholder), done)

    def action_add_note(self) -> None:
        self._ask(
            f"Staff note on @{self._username} (staff only):",
            lambda raw: raw and self._run("Note", lambda: self.app.client.add_user_note(self._uid, raw)),
        )

    def _penalty(self, kind: str) -> None:
        verb = "Silence" if kind == "silence" else "Suspend"

        def got_until(v: str) -> None:
            when = parse_when(v)
            if when is None:
                self.app.notify("Use e.g. 1d, 1w, 1mo, forever or 2026-12-01.", severity="warning")
                return
            self._ask(f"{verb} reason (shown to the user and logged):", lambda r: r and got_reason(when, r))

        def got_reason(when, reason: str) -> None:
            self._ask(
                "Message emailed to the user (optional):",
                lambda msg: confirm(when, reason, msg),
            )

        def confirm(when, reason: str, msg: str) -> None:
            def done(ok: bool | None) -> None:
                if not ok:
                    return
                fn = self.app.client.silence_user if kind == "silence" else self.app.client.suspend_user
                self._run(verb, lambda: fn(self._uid, when.isoformat(), reason, msg))

            self.app.push_screen(
                ConfirmModal(f"{verb} @{self._username} until {local_time(when.isoformat())}?\nReason: {reason}"),
                done,
            )

        self._ask(f"{verb} @{self._username} until:", got_until, "1d, 1w, 1mo, forever, 2026-12-01")

    def action_silence(self) -> None:
        self._penalty("silence")

    def action_suspend(self) -> None:
        self._penalty("suspend")

    def action_lift(self) -> None:
        u = self._user
        if u.get("suspended_till"):
            label, fn = "Unsuspend", self.app.client.unsuspend_user
        elif u.get("silenced_till"):
            label, fn = "Unsilence", self.app.client.unsilence_user
        else:
            self.app.notify("No active penalty.")
            return

        def done(ok: bool | None) -> None:
            if ok:
                self._run(label, lambda: fn(self._uid))

        self.app.push_screen(ConfirmModal(f"{label} @{self._username}?"), done)

    def _run(self, label: str, fn) -> None:
        self._worker(label, fn)

    @work(thread=True, group="usermod-act")
    def _worker(self, label: str, fn) -> None:
        try:
            fn()
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, label)
            return
        self.app.call_from_thread(self.app.notify, f"{label}: done.")
        self._fetch()
