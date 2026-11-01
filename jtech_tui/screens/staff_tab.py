"""The mods-only Staff tab on the main screen: Review · Notes · Mod inbox · Log."""

from __future__ import annotations

from rich.text import Text
from textual import work
from textual.widgets import DataTable, Static, TabbedContent

from ..api import ApiError
from ..render import plain_text
from ..util import local_time, time_ago, truncate
from .composer import TextModal
from .review import TYPE_LABELS, ReviewData, ReviewItemScreen

SECTIONS = ("review", "notes", "inbox", "log")
SECTION_NAMES = {"review": "Review", "notes": "Notes", "inbox": "Mod inbox", "log": "Log"}
COLUMNS = {
    "review": ("Type", "Reason", "Author", "What", "Age"),
    "notes": ("●", "Kind", "Who", "What", "Age"),
    "inbox": ("●", "Subject", "Posts", "Last"),
    "log": ("Who", "Action", "Target", "When"),
}


class StaffTabMixin:
    """Mixed into MainScreen. Uses its _title_width, _restore_cursor, _topic_cache and table helpers."""

    _staff_section = "review"
    _staff_counts: dict[str, int] = {}
    _staff_data: dict[str, object] = {}

    def _sync_staff_tabs(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        if self.app.site.is_staff:
            tabs.show_tab("tab-staff")
            self._staff_counts = dict(self._staff_counts)
            self._load_staff_counts()
        else:
            tabs.hide_tab("tab-staff")

    def on_site_ready(self) -> None:
        self._sync_staff_tabs()

    # --- counts / badge ---

    @work(thread=True, exclusive=True, group="staff-count")
    def _load_staff_counts(self) -> None:
        client = self.app.client
        counts: dict[str, int] = {}
        try:
            counts["review"] = client.review_count() if self.app.site.can_review else 0
        except ApiError:
            pass
        try:
            counts["notes"] = sum(1 for n in client.notes_feed() if n.get("unread"))
        except ApiError:
            pass
        self.app.call_from_thread(self._set_staff_counts, counts)

    def _set_staff_counts(self, counts: dict[str, int]) -> None:
        self._staff_counts = {**self._staff_counts, **counts}
        total = self._staff_counts.get("review", 0) + self._staff_counts.get("notes", 0)
        self._set_tab_badge("tab-staff", "Staff", total)
        self._render_sections()

    def _set_tab_badge(self, tab_id: str, label: str, count: int, mark: str = "●") -> None:
        try:
            from textual.widgets._tabbed_content import ContentTab

            tab = self.query_one(f"#--content-tab-{tab_id}", ContentTab)
        except Exception:  # noqa: BLE001
            return
        tab.label = Text.assemble(f"{label} ", (mark, "bold yellow"), f"{' ' if mark == '●' else ''}{count}") if count else label

    def _render_sections(self) -> None:
        t = Text(" ")
        for s in SECTIONS:
            label = SECTION_NAMES[s]
            n = self._staff_counts.get(s, 0)
            style = "bold reverse" if s == self._staff_section else "dim"
            t.append(f" {label}{f' ●{n}' if n else ''} ", style=style)
            t.append("  ")
        t.append("  ← → switch", style="dim")
        try:
            self.query_one("#staff-sections", Static).update(t)
        except Exception:  # noqa: BLE001
            pass

    # --- switching ---

    def action_staff_next(self) -> None:
        self._staff_switch(1)

    def action_staff_prev(self) -> None:
        self._staff_switch(-1)

    def _staff_switch(self, step: int) -> None:
        if self._current_tab_id() != "tab-staff":
            return
        i = SECTIONS.index(self._staff_section)
        self._staff_section = SECTIONS[(i + step) % len(SECTIONS)]
        self._render_sections()
        self._load_staff()

    def _open_mod_inbox(self) -> None:
        self._staff_section = "inbox"
        tabs = self.query_one("#tabs", TabbedContent)
        if tabs.active == "tab-staff":
            self._load_staff()
        else:
            tabs.active = "tab-staff"
        self.call_after_refresh(self.query_one("#tbl-staff", DataTable).focus)

    def _load_staff(self) -> None:
        self._render_sections()
        sec = self._staff_section
        t = self.query_one("#tbl-staff", DataTable)
        t.clear(columns=True)
        t.add_columns(*COLUMNS[sec])
        cached = self._staff_data.get(sec)
        if cached is not None:
            self._populate_staff(sec, cached)
        self._fetch_staff(sec)

    @work(thread=True, exclusive=True, group="staff")
    def _fetch_staff(self, sec: str) -> None:
        client = self.app.client
        try:
            if sec == "review":
                data: object = client.reviewables("pending")
            elif sec == "notes":
                data = client.notes_feed()
            elif sec == "inbox":
                user = self.app.site.username or self.app.cfg.username
                data = (client.group_messages(user, "moderators").get("topic_list") or {}).get("topics") or []
            else:
                data = client.staff_action_logs().get("staff_action_logs") or []
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, SECTION_NAMES[sec])
            return
        self.app.call_from_thread(self._populate_staff, sec, data)
        if sec == "notes" and any(n.get("unread") for n in data):  # type: ignore[union-attr]
            try:
                client.notes_feed_seen()
            except ApiError:
                return
            self.app.call_from_thread(self._set_staff_counts, {"notes": 0})

    def _populate_staff(self, sec: str, data) -> None:
        self._staff_data = {**self._staff_data, sec: data}
        if sec != self._staff_section:
            return
        t = self.query_one("#tbl-staff", DataTable)
        prev = t.cursor_row
        t.clear()
        getattr(self, f"_rows_{sec}")(t, data)
        if not t.row_count:
            t.add_row(*([""] * (len(COLUMNS[sec]) - 1)), Text("Nothing here.", style="dim"), key="staff:none")
        self._restore_cursor(t, prev)

    def _rows_review(self, t: DataTable, d: dict) -> None:
        rd = ReviewData(d)
        self._review = rd
        self._set_staff_counts({"review": len(rd.items)})
        w = self._title_width(right_reserved=46)
        for it in rd.items:
            t.add_row(
                Text(TYPE_LABELS.get(it.get("type", ""), it.get("type", "")), style="bold"),
                truncate(", ".join(rd.reasons(it)) or "-", 14),
                truncate("@" + (rd.author(it) or "?"), 16),
                truncate(rd.title(it), w),
                time_ago(it.get("created_at")),
                key=f"rev:{it['id']}",
            )

    def _rows_notes(self, t: DataTable, notes: list[dict]) -> None:
        w = self._title_width(right_reserved=40)
        for i, n in enumerate(notes):
            what = n.get("topic_title") or ""
            excerpt = plain_text(n.get("excerpt") or "")
            if excerpt:
                what = f"{what}: {excerpt}" if what else excerpt
            who = n.get("username") or ""
            if n.get("target_username"):
                who = f"{who}→{n['target_username']}"
            t.add_row(
                Text("●", style="bold yellow") if n.get("unread") else Text("·", style="dim"),
                truncate((n.get("kind") or "note").replace("_", " "), 12),
                truncate(who, 18),
                truncate(what, w),
                time_ago(n.get("activity_at") or n.get("created_at")),
                key=f"note:{i}",
            )

    def _rows_inbox(self, t: DataTable, topics: list[dict]) -> None:
        from .main import _posts_cell, _state_cell, _title_cell

        w = self._title_width(right_reserved=22)
        for m in topics:
            key = f"topic:{m.get('id')}"
            self._topic_cache[key] = m
            t.add_row(
                _state_cell(m),
                _title_cell(m, w),
                _posts_cell(m),
                time_ago(m.get("last_posted_at") or m.get("bumped_at")),
                key=key,
            )

    def _rows_log(self, t: DataTable, logs: list[dict]) -> None:
        w = self._title_width(right_reserved=44)
        for i, row in enumerate(logs):
            target = (row.get("target_user") or {}).get("username")
            subject = f"@{target}" if target else (row.get("subject") or row.get("details") or "")
            if row.get("topic_id") and not target:
                subject = subject or f"topic {row['topic_id']}"
            t.add_row(
                truncate("@" + ((row.get("acting_user") or {}).get("username") or "?"), 16),
                truncate((row.get("action_name") or row.get("custom_type") or "").replace("_", " "), 24),
                truncate(subject, w),
                time_ago(row.get("created_at")),
                key=f"log:{i}",
            )

    # --- opening rows ---

    def _staff_row_selected(self, key: str) -> bool:
        """Handle a Staff-tab row. Returns True if handled."""
        if key == "staff:none":
            return True
        if key.startswith("rev:"):
            rd = getattr(self, "_review", None)
            item = next((i for i in rd.items if f"rev:{i['id']}" == key), None) if rd else None
            if item:
                self.app.push_screen(ReviewItemScreen(item, rd))
            return True
        if key.startswith("note:"):
            n = (self._staff_data.get("notes") or [])[int(key[5:])]
            self._open_staff_target(n.get("topic_id"), n.get("url"), n.get("topic_title"))
            return True
        if key.startswith("log:"):
            row = (self._staff_data.get("log") or [])[int(key[4:])]
            if row.get("topic_id"):
                self._open_staff_target(row["topic_id"], None, None)
            else:
                body = Text()
                for k in ("action_name", "details", "context", "previous_value", "new_value", "subject"):
                    if row.get(k):
                        body.append(f"{k.replace('_', ' ')}: ", style="bold")
                        body.append(f"{row[k]}\n")
                body.append(f"\n{local_time(row.get('created_at'))}", style="dim")
                self.app.push_screen(TextModal("Staff action", body))
            return True
        return False

    def _open_staff_target(self, topic_id, url, title) -> None:
        from .thread import ThreadScreen

        if not topic_id:
            if url:
                self.app.open_url(url if url.startswith("http") else self.app.client.base_url + url)
            return
        target = None
        if url:
            import re

            m = re.search(r"/t/[^/]+/\d+/(\d+)", url)
            if m:
                target = int(m.group(1))
        self.app.push_screen(ThreadScreen({"id": topic_id, "title": title or ""}, target_post=target))
