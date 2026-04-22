from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Header,
    Input,
    TabbedContent,
    TabPane,
    Tabs,
)

from .smart_footer import SmartFooter

from ..api import NOTIFICATION_TYPES, Unauthorized
from ..editor import edit_markdown
from .composer import (
    CategoryPickerModal,
    ConfirmModal,
    FilePickerModal,
    NewTopicModal,
    PMComposerModal,
    SearchModal,
)
from .leaderboard import LeaderboardScreen
from .thread import ThreadScreen, _copy_to_clipboard


FEEDS = ("latest", "new", "top", "unseen")
TAB_ORDER = (
    *[f"tab-{f}" for f in FEEDS],
    "tab-categories",
    "tab-messages",
    "tab-notifications",
    "tab-search",
)


def _pretty_time(s: str) -> str:
    if not s:
        return ""
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    now = datetime.now(timezone.utc)
    secs = int((now - t).total_seconds())
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


def _truncate(s: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    if width <= 1:
        return s[:width]
    return s[: width - 1] + "…"


def _topic_state(topic: dict) -> str:
    """'new' | 'unread' | 'pinned' | 'read' — mimics Discourse topic state."""
    if topic.get("unseen") or topic.get("new_posts"):
        return "new"
    if topic.get("unread_posts"):
        return "unread"
    if topic.get("pinned") or topic.get("pinned_globally"):
        return "pinned"
    return "read"


def _state_cell(topic: dict) -> Text:
    state = _topic_state(topic)
    if state == "new":
        return Text("●", style="bold yellow")
    if state == "unread":
        return Text("●", style="bold cyan")
    if state == "pinned":
        return Text("★", style="yellow")
    return Text("·", style="dim")


def _title_cell(topic: dict, width: int) -> Text:
    text = _truncate(topic.get("title", ""), width)
    state = _topic_state(topic)
    if state in ("new", "unread"):
        return Text(text, style="bold")
    if state == "read":
        return Text(text, style="dim")
    return Text(text)


def _posts_cell(topic: dict) -> Text:
    total = topic.get("posts_count")
    if total is None:
        rc = topic.get("reply_count")
        total = (rc + 1) if isinstance(rc, int) else 0
    unread = topic.get("unread_posts") or 0
    new = topic.get("new_posts") or 0
    badge = unread or new
    if badge:
        return Text(f"{badge}/{total}", style="bold cyan")
    return Text(str(total), style="dim")


class VimDataTable(DataTable):
    """DataTable with j/k/g/G, up-at-top-handoff, and request-more-on-bottom."""

    class RequestMore(Message):
        """Posted when the cursor is at the last row and the user tries to go further."""

        def __init__(self, table: "VimDataTable") -> None:
            super().__init__()
            self.table = table

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "cursor_top", "Top", show=False),
        Binding("G", "cursor_bottom", "Bottom", show=False),
    ]

    def action_cursor_top(self) -> None:
        if self.row_count:
            self.move_cursor(row=0)

    def action_cursor_bottom(self) -> None:
        if self.row_count:
            self.move_cursor(row=self.row_count - 1)

    def action_cursor_up(self) -> None:
        if self.cursor_row == 0:
            try:
                tabs = self.screen.query_one(Tabs)
            except NoMatches:
                tabs = None
            if tabs is not None:
                tabs.focus()
                return
        super().action_cursor_up()

    def action_cursor_down(self) -> None:
        at_bottom = self.row_count and self.cursor_row >= self.row_count - 1
        super().action_cursor_down()
        if at_bottom:
            self.post_message(self.RequestMore(self))


class MainScreen(Screen):
    BINDINGS = [
        Binding("ctrl+q", "app.quit", "Quit", priority=True),
        Binding("R", "reload", "Reload"),
        Binding("N", "new_topic", "New"),
        Binding("M", "new_pm", "New PM"),
        Binding("slash", "search", "Search"),
        Binding("U", "upload", "Upload"),
        Binding("L", "leaderboard", "Leaderboard"),
        Binding("O", "logout", "Logout"),
        Binding("ctrl+shift+o", "logout", "Logout", show=False),
        Binding("ctrl+right", "next_tab", "Next tab", show=False),
        Binding("ctrl+left", "prev_tab", "Prev tab", show=False),
        Binding("down", "tabs_down", "", show=False),
        # Jump directly to a tab by number. 1=Latest … 8=Search.
        *[
            Binding(str(i + 1), f"goto_tab({i})", "", show=False)
            for i in range(len(TAB_ORDER))
        ],
    ]

    def action_goto_tab(self, index: int) -> None:
        if 0 <= index < len(TAB_ORDER):
            tabs = self.query_one("#tabs", TabbedContent)
            tabs.active = TAB_ORDER[index]

    def __init__(self) -> None:
        super().__init__()
        self._topic_cache: dict[str, dict] = {}
        self._categories_cache: list[dict] = []
        self._notifications_cache: list[dict] = []
        # Source data per view, cached so we can re-render on resize.
        self._view_data: dict[str, list] = {}
        # Feed pagination state: page index and "has more" flag.
        self._feed_page: dict[str, int] = {}
        self._feed_has_more: dict[str, bool] = {}
        self._feed_loading_more: set[str] = set()
        self._did_initial_load = False

    def on_screen_resume(self) -> None:
        # First resume coincides with on_mount, which already triggers a load.
        if not self._did_initial_load:
            self._did_initial_load = True
            return
        # Coming back from thread/profile/leaderboard — refresh current tab so
        # read state, new posts and unread counts stay accurate.
        self._load_for_tab(self._current_tab_id())

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs"):
            for f in FEEDS:
                with TabPane(f.capitalize(), id=f"tab-{f}"):
                    yield VimDataTable(id=f"tbl-{f}", cursor_type="row", zebra_stripes=True)
            with TabPane("Categories", id="tab-categories"):
                yield VimDataTable(id="tbl-categories", cursor_type="row", zebra_stripes=True)
            with TabPane("Messages", id="tab-messages"):
                yield VimDataTable(id="tbl-messages", cursor_type="row", zebra_stripes=True)
            with TabPane("Notifications", id="tab-notifications"):
                yield VimDataTable(id="tbl-notifications", cursor_type="row", zebra_stripes=True)
            with TabPane("Search", id="tab-search"):
                with Vertical():
                    yield Input(placeholder="Type query, press enter…", id="search-input")
                    yield VimDataTable(id="tbl-search", cursor_type="row", zebra_stripes=True)
        yield SmartFooter()

    def on_mount(self) -> None:
        # Show "signed in as @user" in the header sub-title when available.
        user = (self.app.cfg.username or "").strip()
        if user:
            self.app.sub_title = f"signed in as @{user}"
        # Columns
        for f in FEEDS:
            t = self.query_one(f"#tbl-{f}", DataTable)
            t.add_columns(" ", "Title", "Posts", "Activity")
        self.query_one("#tbl-categories", DataTable).add_columns("Category", "Topics", "Description")
        self.query_one("#tbl-messages", DataTable).add_columns(" ", "Subject", "Posts", "Activity")
        self.query_one("#tbl-notifications", DataTable).add_columns("●", "Type", "From", "Title")
        self.query_one("#tbl-search", DataTable).add_columns("Title", "Blurb")

        tabs = self.query_one("#tabs", TabbedContent)
        default = (self.app.cfg.default_feed or "latest").strip()
        target = f"tab-{default}" if default in FEEDS else (
            "tab-categories" if default == "categories" else
            "tab-messages" if default == "messages" else
            "tab-notifications" if default == "notifications" else
            "tab-latest"
        )
        try:
            tabs.active = target
        except Exception:  # noqa: BLE001
            pass
        self._load_for_tab(tabs.active)
        self.call_after_refresh(self._focus_active_table)

    def _focus_active_table(self) -> None:
        tab_id = self._current_tab_id()
        if not tab_id.startswith("tab-"):
            return
        name = tab_id[4:]
        selector = "#search-input" if name == "search" else f"#tbl-{name}"
        try:
            self.query_one(selector).focus()
        except Exception:  # noqa: BLE001
            pass

    def _title_width(self, right_reserved: int) -> int:
        """Width for the title column given how much space fixed columns need."""
        total = self.size.width or 80
        return max(10, total - right_reserved - 4)  # 4 = cell padding

    def _restore_cursor(self, table: DataTable, prev: int) -> None:
        if not table.row_count:
            return
        row = max(0, min(prev, table.row_count - 1))
        try:
            table.move_cursor(row=row)
        except Exception:  # noqa: BLE001
            pass

    def on_resize(self, event: events.Resize) -> None:
        """Re-render cached views so truncation adapts to the new width."""
        for key, data in list(self._view_data.items()):
            if key.startswith("feed:"):
                self._populate_feed(key[5:], data)
            elif key == "cats":
                self._populate_categories(data)
            elif key == "messages":
                self._populate_messages(data)
            elif key == "notifs":
                self._populate_notifications(data)
            elif key == "search":
                self._populate_search(data)

    # --- tab navigation ---
    @on(TabbedContent.TabActivated)
    def _tab_changed(self, event: TabbedContent.TabActivated) -> None:
        # Only load data; don't steal focus from the tab bar so the user can
        # keep navigating with ← / → without a round-trip.
        self._load_for_tab(self._current_tab_id())

    def action_tabs_down(self) -> None:
        """Fires when 'down' wasn't consumed by a widget — i.e. the tab bar
        has focus. Hand focus off to the current table/input."""
        focused = self.focused
        if focused is None or isinstance(focused, Tabs):
            self._focus_active_table()

    def _current_tab_id(self) -> str:
        return self.query_one("#tabs", TabbedContent).active or "tab-latest"

    def action_next_tab(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        try:
            idx = TAB_ORDER.index(tabs.active)
        except ValueError:
            idx = 0
        tabs.active = TAB_ORDER[(idx + 1) % len(TAB_ORDER)]

    def action_prev_tab(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        try:
            idx = TAB_ORDER.index(tabs.active)
        except ValueError:
            idx = 0
        tabs.active = TAB_ORDER[(idx - 1) % len(TAB_ORDER)]

    def action_reload(self) -> None:
        self._load_for_tab(self._current_tab_id())

    def _load_for_tab(self, tab_id: str) -> None:
        if not tab_id:
            return
        # TabbedContent sometimes reports the content-tab id; normalize to our id
        for candidate in TAB_ORDER:
            if tab_id.endswith(candidate):
                tab_id = candidate
                break
        if tab_id.startswith("tab-") and tab_id[4:] in FEEDS:
            self._load_feed(tab_id[4:])
        elif tab_id == "tab-categories":
            self._load_categories()
        elif tab_id == "tab-messages":
            self._load_messages()
        elif tab_id == "tab-notifications":
            self._load_notifications()
        elif tab_id == "tab-search":
            self.query_one("#search-input", Input).focus()

    # --- feeds ---
    @work(thread=True, exclusive=True, group="feed")
    def _load_feed(self, feed: str) -> None:
        try:
            topics, more_url = self.app.client.feed_page(feed, 0)
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Load failed: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self._populate_feed, feed, topics, bool(more_url))

    def _populate_feed(self, feed: str, topics: list[dict], has_more: bool = False) -> None:
        self._view_data[f"feed:{feed}"] = topics
        self._feed_page[feed] = 0
        self._feed_has_more[feed] = has_more
        self._feed_loading_more.discard(feed)
        t = self.query_one(f"#tbl-{feed}", DataTable)
        prev = t.cursor_row
        t.clear()
        # State(3) + Posts(8) + Activity(6) + cell padding.
        title_w = self._title_width(right_reserved=22)
        for topic in topics:
            self._add_feed_row(t, feed, topic, title_w)
        self._restore_cursor(t, prev)

    def _add_feed_row(self, t: DataTable, feed: str, topic: dict, title_w: int) -> None:
        key = f"topic:{topic.get('id')}"
        self._topic_cache[key] = topic
        t.add_row(
            _state_cell(topic),
            _title_cell(topic, title_w),
            _posts_cell(topic),
            _pretty_time(topic.get("last_posted_at") or topic.get("bumped_at", "")),
            key=key,
        )

    @work(thread=True, exclusive=True, group="feed-more")
    def _load_more_feed(self, feed: str) -> None:
        next_page = self._feed_page.get(feed, 0) + 1
        try:
            topics, more_url = self.app.client.feed_page(feed, next_page)
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Load more failed: {e}", severity="error"
            )
            self._feed_loading_more.discard(feed)
            return
        self.app.call_from_thread(self._append_feed_page, feed, topics, bool(more_url), next_page)

    def _append_feed_page(
        self, feed: str, topics: list[dict], has_more: bool, page: int
    ) -> None:
        self._feed_loading_more.discard(feed)
        if not topics:
            self._feed_has_more[feed] = False
            return
        # Filter out any duplicates that might come back from Discourse.
        existing = self._view_data.get(f"feed:{feed}", [])
        existing_ids = {t_.get("id") for t_ in existing}
        fresh = [t_ for t_ in topics if t_.get("id") not in existing_ids]
        existing.extend(fresh)
        self._view_data[f"feed:{feed}"] = existing
        self._feed_page[feed] = page
        self._feed_has_more[feed] = has_more
        t = self.query_one(f"#tbl-{feed}", DataTable)
        title_w = self._title_width(right_reserved=22)
        for topic in fresh:
            self._add_feed_row(t, feed, topic, title_w)
        if fresh:
            self.app.notify(f"+{len(fresh)} more.", severity="information")

    @on(VimDataTable.RequestMore)
    def _on_request_more(self, event: "VimDataTable.RequestMore") -> None:
        tid = event.table.id or ""
        if not tid.startswith("tbl-"):
            return
        feed = tid[4:]
        if feed not in FEEDS:
            return
        if not self._feed_has_more.get(feed):
            return
        if feed in self._feed_loading_more:
            return
        self._feed_loading_more.add(feed)
        self._load_more_feed(feed)

    # --- categories ---
    @work(thread=True, exclusive=True, group="cats")
    def _load_categories(self) -> None:
        try:
            cats = self.app.client.categories()
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Load failed: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self._populate_categories, cats)

    def _populate_categories(self, cats: list[dict]) -> None:
        self._categories_cache = cats
        self._view_data["cats"] = cats
        t = self.query_one("#tbl-categories", DataTable)
        prev = t.cursor_row
        t.clear()
        total = self.size.width or 80
        # Reserve: name column ~20 + topics ~6 + padding ~6
        desc_w = max(10, total - 32)
        for c in cats:
            key = f"cat:{c.get('id')}"
            self._topic_cache[key] = c
            t.add_row(
                _truncate(c.get("name", ""), 20),
                str(c.get("topic_count", 0)),
                _truncate((c.get("description") or ""), desc_w),
                key=key,
            )
        self._restore_cursor(t, prev)

    # --- messages (PMs) ---
    @work(thread=True, exclusive=True, group="pms")
    def _load_messages(self) -> None:
        try:
            user = self.app.cfg.username or self.app.client.current_username()
            if not user:
                self.app.call_from_thread(
                    self.app.notify, "No username on record — log in again.", severity="error"
                )
                return
            if not self.app.cfg.username:
                self.app.cfg.username = user
                self.app.cfg.save()
            msgs = self.app.client.private_messages(user)
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Load failed: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self._populate_messages, msgs)

    def _populate_messages(self, msgs: list[dict]) -> None:
        self._view_data["messages"] = msgs
        t = self.query_one("#tbl-messages", DataTable)
        prev = t.cursor_row
        t.clear()
        subj_w = self._title_width(right_reserved=22)
        for m in msgs:
            key = f"topic:{m.get('id')}"
            self._topic_cache[key] = m
            t.add_row(
                _state_cell(m),
                _title_cell(m, subj_w),
                _posts_cell(m),
                _pretty_time(m.get("last_posted_at") or m.get("bumped_at", "")),
                key=key,
            )
        self._restore_cursor(t, prev)

    # --- notifications ---
    @work(thread=True, exclusive=True, group="notifs")
    def _load_notifications(self) -> None:
        try:
            notifs = self.app.client.notifications()
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Load failed: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self._populate_notifications, notifs)

    def _populate_notifications(self, notifs: list[dict]) -> None:
        self._notifications_cache = notifs
        self._view_data["notifs"] = notifs
        t = self.query_one("#tbl-notifications", DataTable)
        prev = t.cursor_row
        t.clear()
        total = self.size.width or 80
        # Reserve: read-marker(2) + type(12) + from(14) + padding(8)
        title_w = max(10, total - 36)
        for n in notifs:
            nid = n.get("id")
            tid = n.get("topic_id")
            key = f"notif:{nid}:{tid or ''}"
            self._topic_cache[key] = n
            title = n.get("fancy_title") or ((n.get("data") or {}).get("topic_title")) or ""
            who = (n.get("data") or {}).get("display_username", "") or ""
            t.add_row(
                "·" if n.get("read") else "●",
                _truncate(NOTIFICATION_TYPES.get(n.get("notification_type"), "?"), 12),
                _truncate(who, 14),
                _truncate(title, title_w),
                key=key,
            )
        self._restore_cursor(t, prev)

    # --- search ---
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-input":
            q = event.value.strip()
            if q:
                self._run_search(q)

    def on_key(self, event: events.Key) -> None:
        # `esc` in the inline search input hands focus back to the tab bar
        # so the user can navigate away without clicking or tabbing through.
        if event.key == "escape":
            focused = self.focused
            if focused is not None and getattr(focused, "id", "") == "search-input":
                try:
                    self.query_one(Tabs).focus()
                except NoMatches:
                    pass
                event.stop()

    @work(thread=True, exclusive=True, group="search")
    def _run_search(self, query: str) -> None:
        try:
            res = self.app.client.search(query)
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Search failed: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self._populate_search, res)

    def _populate_search(self, res: dict) -> None:
        self._view_data["search"] = res
        t = self.query_one("#tbl-search", DataTable)
        prev = t.cursor_row
        t.clear()
        total = self.size.width or 80
        title_w = max(20, total // 3)
        blurb_w = max(20, total - title_w - 6)
        topics_by_id = {t_.get("id"): t_ for t_ in (res.get("topics") or [])}
        posts = res.get("posts") or []
        seen: set[int] = set()
        for p in posts:
            tid = p.get("topic_id")
            if not tid or tid in seen:
                continue
            seen.add(tid)
            topic = topics_by_id.get(tid, {"id": tid, "title": p.get("topic_title_headline") or ""})
            key = f"topic:{tid}"
            self._topic_cache[key] = topic
            blurb = (p.get("blurb") or "").replace("\n", " ")
            title = topic.get("title") or p.get("topic_title_headline") or str(tid)
            t.add_row(_truncate(title, title_w), _truncate(blurb, blurb_w), key=key)
        if not seen:
            for topic in res.get("topics") or []:
                tid = topic.get("id")
                key = f"topic:{tid}"
                self._topic_cache[key] = topic
                t.add_row(_truncate(topic.get("title", ""), title_w), "", key=key)
        self._restore_cursor(t, prev)

    # --- row selection routing ---
    @on(DataTable.RowSelected)
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        key_obj = event.row_key
        key = key_obj.value if hasattr(key_obj, "value") else str(key_obj)
        if not key:
            return
        if key.startswith("topic:"):
            topic = self._topic_cache.get(key)
            if topic:
                self.app.push_screen(ThreadScreen(topic))
        elif key.startswith("cat:"):
            cat = self._topic_cache.get(key)
            if cat:
                self.app.push_screen(CategoryTopicsScreen(cat))
        elif key.startswith("notif:"):
            notif = self._topic_cache.get(key, {})
            tid = notif.get("topic_id")
            if tid:
                topic = {
                    "id": tid,
                    "title": notif.get("fancy_title") or (notif.get("data") or {}).get("topic_title", ""),
                }
                self.app.push_screen(ThreadScreen(topic))

    # --- compose actions ---
    def action_search(self) -> None:
        def _done(q: str | None) -> None:
            if q:
                tabs = self.query_one("#tabs", TabbedContent)
                tabs.active = "tab-search"
                self.query_one("#search-input", Input).value = q
                self._run_search(q)

        self.app.push_screen(SearchModal(), _done)

    def action_new_topic(self) -> None:
        if self._categories_cache:
            self._show_new_topic_modal(self._categories_cache)
        else:
            self._fetch_cats_then_compose()

    @work(thread=True, exclusive=True, group="cats")
    def _fetch_cats_then_compose(self) -> None:
        try:
            cats = self.app.client.categories()
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Load failed: {e}", severity="error"
            )
            return
        self._categories_cache = cats
        self.app.call_from_thread(self._show_new_topic_modal, cats)

    def _show_new_topic_modal(self, cats: list[dict]) -> None:
        def _done(result: dict | None) -> None:
            if not result:
                return
            template = f"# {result['title']}\n\nWrite your topic body in markdown.\n"
            with self.app.suspend():
                content = edit_markdown(template)
            self.refresh()
            if not content:
                return
            body = _strip_title_header(content).strip()
            if not body:
                self.app.notify("Empty body — not posted.", severity="warning")
                return
            self._submit_topic(result["title"], body, result["category_id"])

        self.app.push_screen(NewTopicModal(cats), _done)

    @work(thread=True, exclusive=True, group="post")
    def _submit_topic(self, title: str, body: str, category_id: int) -> None:
        try:
            self.app.client.create_topic(title, body, category_id)
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Post failed: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self.app.notify, "Topic created.", severity="information")
        self.app.call_from_thread(self._load_for_tab, self._current_tab_id())

    def action_new_pm(self) -> None:
        def _done(result: dict | None) -> None:
            if not result:
                return
            template = f"# {result['title']}\n\nWrite your private message in markdown.\n"
            with self.app.suspend():
                content = edit_markdown(template)
            self.refresh()
            if not content:
                return
            body = _strip_title_header(content).strip()
            if not body:
                self.app.notify("Empty body — not sent.", severity="warning")
                return
            self._submit_pm(result["title"], body, result["recipients"])

        self.app.push_screen(PMComposerModal(), _done)

    @work(thread=True, exclusive=True, group="post")
    def _submit_pm(self, title: str, body: str, recipients: list[str]) -> None:
        try:
            self.app.client.send_pm(title, body, recipients)
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Send failed: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self.app.notify, "Message sent.", severity="information")

    def action_upload(self) -> None:
        def _done(path: str | None) -> None:
            if path:
                self._do_upload(path)
        self.app.push_screen(FilePickerModal("Upload to forum"), _done)

    def action_leaderboard(self) -> None:
        self.app.push_screen(LeaderboardScreen())

    def action_logout(self) -> None:
        user = (self.app.cfg.username or "").strip()
        prompt = (
            f"Sign out @{user}?\n\nThis will clear the saved session."
            if user
            else "Sign out?\n\nThis will clear the saved session."
        )

        def _done(confirmed: bool | None) -> None:
            if confirmed:
                self.app.logout()

        self.app.push_screen(ConfirmModal(prompt), _done)

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


def _strip_title_header(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines)


class CategoryTopicsScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back"),
        Binding("R", "reload", "Reload"),
    ]

    def __init__(self, category: dict) -> None:
        super().__init__()
        self._cat = category
        self._topics: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield VimDataTable(id="tbl-cat", cursor_type="row", zebra_stripes=True)
        yield SmartFooter()

    def on_mount(self) -> None:
        self.sub_title = self._cat.get("name", "")
        t = self.query_one("#tbl-cat", DataTable)
        t.add_columns(" ", "Title", "Posts", "Activity")
        self._load()
        self.call_after_refresh(t.focus)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        self._load()

    def _load(self) -> None:
        self._fetch()

    @work(thread=True, exclusive=True, group="cat-topics")
    def _fetch(self) -> None:
        try:
            topics = self.app.client.category_topics(
                self._cat.get("slug", ""), int(self._cat.get("id", 0))
            )
        except Unauthorized:
            self.app.call_from_thread(self.app.reauth)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(
                self.app.notify, f"Load failed: {e}", severity="error"
            )
            return
        self.app.call_from_thread(self._populate, topics)

    def _populate(self, topics: list[dict]) -> None:
        t = self.query_one("#tbl-cat", DataTable)
        t.clear()
        total = self.size.width or 80
        title_w = max(10, total - 26)
        for topic in topics:
            key = str(topic.get("id"))
            self._topics[key] = topic
            t.add_row(
                _state_cell(topic),
                _title_cell(topic, title_w),
                _posts_cell(topic),
                _pretty_time(topic.get("last_posted_at") or topic.get("bumped_at", "")),
                key=key,
            )

    @on(DataTable.RowSelected)
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        key_obj = event.row_key
        key = key_obj.value if hasattr(key_obj, "value") else str(key_obj)
        topic = self._topics.get(key)
        if topic:
            self.app.push_screen(ThreadScreen(topic))
