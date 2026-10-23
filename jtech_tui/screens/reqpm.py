"""REQ-PM (jtech-tools contact exchange): one user's exchange with you, or your whole inbox."""

from __future__ import annotations

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Header, LoadingIndicator, Static

from ..api import ApiError
from ..util import copy_to_clipboard, local_time, time_ago, truncate
from ..widgets import VimDataTable
from .composer import ChecklistModal, ConfirmModal
from .smart_footer import SmartFooter

KIND_LABELS = {
    "phone": "📞 Phone",
    "sms": "💬 Text",
    "whatsapp": "🟢 WhatsApp",
    "email": "✉ Email",
    "website": "🌐 Website",
    "telegram": "✈ Telegram",
    "signal": "🔵 Signal",
    "discord": "🎮 Discord",
}
REQUEST_KINDS = list(KIND_LABELS)

INBOX_SECTIONS = ("incoming", "received", "sent", "outgoing", "card")
INBOX_NAMES = {
    "incoming": "Requests",
    "received": "Contacts",
    "sent": "Shared with",
    "outgoing": "My requests",
    "card": "My card",
}
BLOCKED = {
    "pending": "you already have a request waiting",
    "cooldown": "you asked recently; try again later",
}


def method_label(m: dict) -> str:
    if m.get("kind") == "custom":
        return f"{m.get('emoji') or '•'} {m.get('label') or 'Other'}"
    return KIND_LABELS.get(m.get("kind", ""), m.get("kind", "?"))


class ReqpmScreen(Screen):
    """``username`` set: your exchange with that user. ``None``: your inbox."""

    BINDINGS = [
        Binding("escape,q", "back", "Back"),
        Binding("r", "request", "Request details"),
        Binding("s", "share", "Send my details"),
        Binding("x", "cancel", "Cancel request"),
        Binding("d", "decline", "Decline"),
        Binding("R", "reload", "Reload"),
    ]

    def __init__(self, username: str | None) -> None:
        super().__init__()
        self._username = username
        self._rel: dict = {}
        self._inbox: dict = {}
        self._card: list[dict] = []
        self._section = "incoming"

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if self._username is None:
            # In the inbox you answer requests; asking and cancelling happen on a user's page.
            return action not in ("request", "cancel")
        rel = self._rel
        if action == "request":
            return bool(rel.get("can_request"))
        if action == "share":
            return bool(rel.get("can_share"))
        if action == "cancel":
            return bool((rel.get("outgoing_request") or {}).get("can_cancel"))
        if action == "decline":
            return bool(rel.get("incoming_request"))
        return True

    def compose(self) -> ComposeResult:
        yield Header()
        yield LoadingIndicator(id="loader")
        yield Static(id="reqpm-head")
        yield VimDataTable(id="tbl-reqpm", cursor_type="row", zebra_stripes=True)
        yield SmartFooter()

    def on_mount(self) -> None:
        self.sub_title = f"REQ-PM · @{self._username}" if self._username else "REQ-PM"
        if self._username is None:
            self.query_one("#tbl-reqpm", VimDataTable).sideways = self._switch
        self._fetch()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        self._fetch()

    @work(thread=True, exclusive=True, group="reqpm")
    def _fetch(self) -> None:
        client = self.app.client
        try:
            card = client.reqpm_card().get("methods") or []
            if self._username:
                rel = client.reqpm_relationship(self._username)
                self.app.call_from_thread(self._show_rel, rel, card)
            else:
                inbox = client.reqpm_inbox()
                self.app.call_from_thread(self._show_inbox, inbox, card)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "REQ-PM")

    # --- one user ---

    def _show_rel(self, rel: dict, card: list[dict]) -> None:
        self._rel = rel
        self._card = card
        self.query_one("#loader").display = False
        t = Text()
        t.append(f"@{self._username}", style="bold")
        inc = rel.get("incoming_request")
        out = rel.get("outgoing_request")
        if inc:
            t.append(f"\n📨 @{self._username} asked for your details {time_ago(inc.get('created_at'))} ago", style="bold yellow")
            t.append("  (s send · d decline)", style="dim")
        if out:
            state = out.get("state", "")
            t.append(f"\nYour request: {state}", style="cyan")
            t.append(f" · {time_ago(out.get('created_at'))} ago", style="dim")
        if not rel.get("can_request") and rel.get("request_blocked"):
            reason = BLOCKED.get(rel["request_blocked"], str(rel["request_blocked"]).replace("_", " "))
            if rel.get("retry_at"):
                reason += f" (after {local_time(rel['retry_at'])})"
            t.append(f"\nCan't request: {reason}", style="dim")
        shared = set(rel.get("my_shared_method_ids") or [])
        if shared:
            t.append(f"\nYou've shared {len(shared)} of your details with them.", style="dim")
        self.query_one("#reqpm-head", Static).update(t)
        table = self.query_one("#tbl-reqpm", VimDataTable)
        table.clear(columns=True)
        table.add_columns("Their details", "Value", "Note")
        for i, m in enumerate(rel.get("their_methods") or []):
            table.add_row(method_label(m), m.get("value") or "", Text(m.get("note") or "", style="dim"), key=f"m:{i}")
        if not table.row_count:
            table.add_row(Text("They haven't shared anything with you.", style="dim"), "", "", key="none")
        self.refresh_bindings()
        table.focus()

    # --- inbox ---

    def _switch(self, step: int) -> None:
        i = INBOX_SECTIONS.index(self._section)
        self._section = INBOX_SECTIONS[(i + step) % len(INBOX_SECTIONS)]
        self._fill_inbox()

    def _show_inbox(self, inbox: dict, card: list[dict]) -> None:
        self._inbox = inbox
        self._card = card
        self.query_one("#loader").display = False
        self._fill_inbox()
        self.query_one("#tbl-reqpm").focus()

    def _fill_inbox(self) -> None:
        sec = self._section
        head = Text(" ")
        for s in INBOX_SECTIONS:
            n = len(self._inbox.get(s) or []) if s != "card" else len(self._card)
            style = "bold reverse" if s == sec else "dim"
            badge = f" ●{n}" if s == "incoming" and n else ""
            head.append(f" {INBOX_NAMES[s]}{badge} ", style=style)
            head.append(" ")
        head.append("  ← → switch", style="dim")
        self.query_one("#reqpm-head", Static).update(head)
        table = self.query_one("#tbl-reqpm", VimDataTable)
        table.clear(columns=True)
        rows = self._inbox.get(sec) or []
        if sec == "incoming":
            table.add_columns("From", "Wants", "When")
            for i, r in enumerate(rows):
                wants = ", ".join(KIND_LABELS.get(k, k) for k in r.get("wanted_kinds") or []) or "anything"
                table.add_row(f"@{r['user']['username']}", truncate(wants, 50), time_ago(r.get("created_at")), key=f"i:{i}")
        elif sec == "received":
            table.add_columns("From", "Detail", "Value")
            for i, r in enumerate(rows):
                for j, m in enumerate(r.get("methods") or []):
                    table.add_row(f"@{r['user']['username']}" if j == 0 else "", method_label(m), m.get("value") or "", key=f"c:{i}:{j}")
        elif sec == "sent":
            names = {m["id"]: method_label(m) for m in self._card}
            table.add_columns("To", "What", "When")
            for i, r in enumerate(rows):
                what = ", ".join(names.get(mid, "?") for mid in r.get("method_ids") or [])
                table.add_row(f"@{r['user']['username']}", truncate(what, 50), time_ago(r.get("shared_at")), key=f"s:{i}")
        elif sec == "outgoing":
            table.add_columns("To", "State", "When")
            for i, r in enumerate(rows):
                table.add_row(f"@{r['user']['username']}", r.get("state", ""), time_ago(r.get("created_at")), key=f"o:{i}")
        else:
            table.add_columns("Detail", "Value", "Shared by default")
            for i, m in enumerate(self._card):
                table.add_row(method_label(m), m.get("value") or "", "yes" if m.get("share_by_default") else "", key=f"k:{i}")
        if not table.row_count:
            table.add_row(*([""] * (len(table.columns) - 1)), Text("Nothing here.", style="dim"), key="none")

    def _selected_key(self) -> str:
        table = self.query_one("#tbl-reqpm", DataTable)
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value or ""
        except Exception:  # noqa: BLE001
            return ""

    @on(DataTable.RowSelected, "#tbl-reqpm")
    def _row(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value or ""
        if key.startswith("m:"):
            m = (self._rel.get("their_methods") or [])[int(key[2:])]
            self._copy(m)
        elif key.startswith("c:"):
            _, i, j = key.split(":")
            m = (self._inbox.get("received") or [])[int(i)]["methods"][int(j)]
            self._copy(m)
        elif key.startswith(("i:", "s:", "o:")):
            rows = self._inbox.get({"i": "incoming", "s": "sent", "o": "outgoing"}[key[0]]) or []
            name = rows[int(key[2:])]["user"]["username"]
            self.app.push_screen(ReqpmScreen(name))

    def _copy(self, m: dict) -> None:
        value = m.get("value") or ""
        if value and copy_to_clipboard(value):
            self.app.notify(f"Copied {method_label(m)}.")

    # --- actions ---

    def _target(self) -> str | None:
        if self._username:
            return self._username
        key = self._selected_key()
        if key.startswith("i:"):
            return (self._inbox.get("incoming") or [])[int(key[2:])]["user"]["username"]
        return None

    def action_request(self) -> None:
        if not self._username or not self.check_action("request", ()):
            return
        items = [(k, label, False) for k, label in KIND_LABELS.items()]

        def done(kinds: list[str] | None) -> None:
            if kinds is not None:
                self._act("Request sent", lambda: self.app.client.reqpm_request(self._username, kinds or None))

        self.app.push_screen(ChecklistModal(f"Ask @{self._username} for (none ticked = anything):", items), done)

    def action_share(self) -> None:
        name = self._target()
        if not name:
            self.app.notify("Pick a request first.")
            return
        if not self._card:
            self.app.notify("Your card is empty. Add details on the website first.", severity="warning")
            return
        already = set(self._rel.get("my_shared_method_ids") or []) if self._username else set()
        wanted: set[str] = set()
        if self._username:
            wanted = set((self._rel.get("incoming_request") or {}).get("wanted_kinds") or [])
        else:
            key = self._selected_key()
            wanted = set((self._inbox.get("incoming") or [])[int(key[2:])].get("wanted_kinds") or [])
        items = [
            (str(m["id"]), f"{method_label(m)}: {m.get('value') or ''}",
             m["id"] in already or (m.get("kind") in wanted) or (not wanted and bool(m.get("share_by_default"))))
            for m in self._card
            if not m.get("unreadable")
        ]

        def done(ids: list[str] | None) -> None:
            if ids:
                self._act(f"Sent to @{name}", lambda: self.app.client.reqpm_share(name, [int(i) for i in ids]))

        self.app.push_screen(ChecklistModal(f"Send @{name} these details:", items), done)

    def action_cancel(self) -> None:
        req = (self._rel.get("outgoing_request") or {}) if self._username else {}
        if not req.get("can_cancel"):
            return
        self.app.push_screen(
            ConfirmModal(f"Cancel your request to @{self._username}?"),
            lambda ok: ok and self._act("Request cancelled", lambda: self.app.client.reqpm_cancel(int(req["id"]))),
        )

    def action_decline(self) -> None:
        if self._username:
            req = self._rel.get("incoming_request") or {}
            name = self._username
        else:
            key = self._selected_key()
            if not key.startswith("i:"):
                self.app.notify("Pick a request first.")
                return
            row = (self._inbox.get("incoming") or [])[int(key[2:])]
            req, name = row, row["user"]["username"]
        if not req.get("id"):
            return
        self.app.push_screen(
            ConfirmModal(f"Decline @{name}'s request? They won't be told."),
            lambda ok: ok and self._act("Declined", lambda: self.app.client.reqpm_decline(int(req["id"]))),
        )

    def _act(self, label: str, fn) -> None:
        self._act_worker(label, fn)

    @work(thread=True, group="reqpm-act")
    def _act_worker(self, label: str, fn) -> None:
        try:
            fn()
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "REQ-PM")
            return
        self.app.call_from_thread(self.app.notify, f"{label}.")
        self._fetch()
