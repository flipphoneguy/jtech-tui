from __future__ import annotations

import time

from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Header,
    Input,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
)

from .smart_footer import SmartFooter

from ..render import plain_text
from ..util import copy_to_clipboard, time_ago as _pretty_time, truncate as _truncate

from .. import complete
from ..api import ApiError
from ..editor import edit_markdown
from .composer import (
    ConfirmModal,
    FilePickerModal,
    MenuModal,
    NewTopicModal,
    PMComposerModal,
    PromptModal,
    SearchModal,
)
from .leaderboard import LeaderboardScreen
from ..widgets import VimDataTable
from .search_tab import SearchTabMixin
from .staff_tab import StaffTabMixin
from .thread import ThreadScreen, upload_and_copy


FEEDS = ("latest", "unseen")
FEED_LABELS = {"latest": "Latest", "unseen": "Unseen"}
# With the site's unified New view, /new lists brand-new topics and topics with unread replies.
FEED_PATHS = {"latest": "latest", "unseen": "new"}
NOTIFICATION_LEVELS = [("3", "Watching"), ("2", "Tracking"), ("1", "Normal"), ("0", "Muted")]
TAB_ORDER = (
    *[f"tab-{f}" for f in FEEDS],
    "tab-categories",
    "tab-messages",
    "tab-notifications",
    "tab-staff",
    "tab-search",
)


def _unread_count(topic: dict) -> int:
    """Posts after the last one read. The site only sends this for tracked topics, so work it out the same way for the rest."""
    if topic.get("unread_posts"):
        return topic["unread_posts"]
    last = topic.get("last_read_post_number")
    high = topic.get("highest_post_number")
    if isinstance(last, int) and last > 0 and isinstance(high, int):
        return max(0, high - last)
    return 0


def _topic_state(topic: dict) -> str:
    """'new' (never opened, recent) | 'unread' (tracked, has unread) | 'regular' (not tracked, has unread) | 'unopened' | 'read'."""
    if topic.get("unseen"):
        return "new"
    if _unread_count(topic):
        return "unread" if (topic.get("notification_level") or 0) >= 2 else "regular"
    if not topic.get("last_read_post_number"):
        return "unopened"
    return "read"


def _state_cell(topic: dict) -> Text:
    state = _topic_state(topic)
    if state == "new":
        return Text("●", style="bold yellow")
    if state == "unread":
        return Text("●", style="bold cyan")
    return Text(" ")


def _markers(topic: dict) -> str:
    m = ""
    if topic.get("has_accepted_answer"):
        m += "✓"
    if topic.get("pinned") or topic.get("pinned_globally"):
        m += "📌"
    if topic.get("closed"):
        m += "🔒"
    if topic.get("is_hot"):
        m += "🔥"
    return m


def _title_cell(topic: dict, width: int, site=None) -> Text:
    """Title with status markers; with ``site``, a second dim line of category, tags and last poster."""
    marks = _markers(topic)
    t = Text()
    if marks:
        t.append(marks + " ")
    state = _topic_state(topic)
    style = {"new": "bold", "unread": "bold", "read": "dim"}.get(state, "")
    t.append(_truncate(topic.get("title", ""), max(5, width - t.cell_len)), style=style)
    if site is None:
        return t
    t.append("\n  ")
    t.append_text(_meta_line(topic, site, max(10, width - 2)))
    return t


def _meta_line(topic: dict, site, width: int) -> Text:
    """Category · tags · @poster, each part shortened within its own budget so none crowds out the rest."""
    sep = " · "
    poster = f"@{topic['last_poster_username']}" if topic.get("last_poster_username") else ""
    tags = " ".join(
        f"#{x if isinstance(x, str) else x.get('name', '')}" for x in topic.get("tags") or []
    )
    cat = site.category_label(topic.get("category_id"))
    # Fixed parts first, then split what's left between category and tags.
    poster = _truncate(poster, 16)
    fixed = len(poster) + len(sep) if poster else 0
    room = max(0, width - fixed)
    if cat and tags:
        cat_w = max(8, min(len(cat), room // 2 if len(tags) > room // 2 else room - len(tags) - len(sep)))
        tag_w = max(0, room - cat_w - len(sep))
    else:
        cat_w = tag_w = room
    parts = [
        (_truncate(cat, cat_w), "italic"),
        (_truncate(tags, tag_w) if tag_w >= 4 else "", "#9a86c8"),
        (poster, ""),
    ]
    out = Text(style="dim")
    for text, style in parts:
        if not text:
            continue
        if out.plain:
            out.append(sep)
        out.append(text, style=style)
    return out


def _posts_cell(topic: dict, with_time: bool = False) -> Text:
    """Post count, plus the last activity time on a second line for two-line rows."""
    total = topic.get("posts_count")
    if total is None:
        rc = topic.get("reply_count")
        total = (rc + 1) if isinstance(rc, int) else 0
    state = _topic_state(topic)
    if state in ("unread", "regular"):
        t = Text(f"{_unread_count(topic)}/{total}", style="bold cyan" if state == "unread" else "", justify="right")
    else:
        t = Text(str(total), style="dim" if state == "read" else "", justify="right")
    if with_time:
        t.append("\n" + _pretty_time(topic.get("last_posted_at") or topic.get("bumped_at", "")), style="dim")
    return t


class MainScreen(StaffTabMixin, SearchTabMixin, Screen):
    BINDINGS = [
        Binding("ctrl+q", "app.quit", "Quit", priority=True),
        Binding("R", "reload", "Reload"),
        Binding("N", "new_topic", "New"),
        Binding("M", "new_pm", "New PM"),
        Binding("slash", "search", "Search"),
        Binding("U", "upload", "Upload"),
        Binding("L", "leaderboard", "Leaderboard"),
        Binding("P", "my_profile", "Me"),
        # Works while typing in the search box, where plain letters would be text.
        Binding("ctrl+f", "filter", "Filters", show=False, priority=True),
        Binding("f", "filter", "Filter"),
        Binding("w", "topic_level", "Watch…"),
        Binding("D", "dismiss_unseen", "Dismiss"),
        Binding("y", "copy_link", "Copy link"),
        Binding("ctrl+right", "next_tab", "Next tab", show=False),
        Binding("ctrl+left", "prev_tab", "Prev tab", show=False),
        Binding("down", "tabs_down", "", show=False),
        Binding("up", "tabs_up", "", show=False),
        Binding("n", "next_unread", "Next unread", show=False),
        Binding("p", "prev_unread", "Prev unread", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._topic_cache: dict[str, dict] = {}
        self._categories_cache: list[dict] = []
        self._notifications_cache: list[dict] = []
        self._view_data: dict[str, list] = {}
        self._feed_page: dict[str, int] = {}
        self._feed_has_more: dict[str, bool] = {}
        self._feed_loading_more: set[str] = set()
        self._did_initial_load = False
        self._live_new: dict[str, set[int]] = {}
        # feed -> ("category", category dict) | ("tag", name)
        self._filters: dict[str, tuple[str, object]] = {}
        self._top_tags: list[str] = []
        self._thread_prefetch: dict[int, tuple[dict, float]] = {}
        self._prefetch_timer = None

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
                with TabPane(FEED_LABELS[f], id=f"tab-{f}"):
                    yield VimDataTable(id=f"tbl-{f}", cursor_type="row", zebra_stripes=True)
            with TabPane("Categories", id="tab-categories"):
                yield VimDataTable(id="tbl-categories", cursor_type="row", zebra_stripes=True)
            with TabPane("Messages", id="tab-messages"):
                yield VimDataTable(id="tbl-messages", cursor_type="row", zebra_stripes=True)
            with TabPane("Notifications", id="tab-notifications"):
                yield VimDataTable(id="tbl-notifications", cursor_type="row", zebra_stripes=True)
            with TabPane("Staff", id="tab-staff"):
                with Vertical():
                    yield Static(id="staff-sections")
                    yield VimDataTable(id="tbl-staff", cursor_type="row", zebra_stripes=True)
            with TabPane("Search", id="tab-search"):
                with Vertical():
                    yield Input(placeholder="Search… (ctrl+f filters: category, user, tag, date, solved, sort)", id="search-input")
                    yield VimDataTable(id="tbl-search", cursor_type="row", zebra_stripes=True)
        yield SmartFooter()

    def on_mount(self) -> None:
        # Columns
        for f in FEEDS:
            t = self.query_one(f"#tbl-{f}", DataTable)
            t.add_columns(" ", "Title", "Posts")
        self.query_one("#tbl-categories", DataTable).add_columns("Category", "Topics", "Description")
        self.query_one("#tbl-messages", DataTable).add_columns(" ", "Subject", "Posts", "Last")
        self.query_one("#tbl-notifications", DataTable).add_columns("●", "Type", "From", "Title")
        self.query_one("#tbl-search", DataTable).add_columns("Topic", "Match", "When")

        self.query_one("#tbl-staff", VimDataTable).sideways = self._staff_switch
        self.query_one("#tbl-search", VimDataTable).up_target = lambda: self.query_one("#search-input")

        tabs = self.query_one("#tabs", TabbedContent)
        self._sync_staff_tabs()
        default = (self.app.cfg.default_feed or "latest").strip()
        # Old configs may name feeds that were folded into others.
        default = {"new": "unseen", "unread": "unseen", "top": "latest"}.get(default, default)
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
        if tabs.active != "tab-notifications":
            self._load_notifications()
        self.call_after_refresh(self._focus_active_table)
        live = self.app.live
        live.subscribe("/latest", lambda d: self._on_live_topics("latest", d))
        live.subscribe("/new", lambda d: self._on_live_topics("unseen", d))

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
                self._populate_feed(key[5:], data, redraw=True)
            elif key == "cats":
                self._populate_categories(data)
            elif key == "messages":
                self._populate_messages(data)
            elif key == "notifs":
                self._populate_notifications(data)
        self._rebuild_search()
        staff = self._staff_data.get(self._staff_section)
        if staff is not None:
            self._populate_staff(self._staff_section, staff)

    # --- prefetch on hover ---
    @on(DataTable.RowHighlighted)
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table = event.data_table
        if not (table.id and table.id.startswith("tbl-") and table.id[4:] in FEEDS):
            return
        key_obj = event.row_key
        key = key_obj.value if hasattr(key_obj, "value") else str(key_obj or "")
        if not key or not key.startswith("topic:"):
            return
        topic = self._topic_cache.get(key)
        if not topic:
            return
        tid = topic.get("id")
        cached = self._thread_prefetch.get(tid) if tid else None
        if not tid or (cached and time.monotonic() - cached[1] < 45):
            return
        if self._prefetch_timer is not None:
            self._prefetch_timer.stop()
        self._prefetch_timer = self.set_timer(
            0.3, lambda: self._do_prefetch(int(tid), topic)
        )

    @work(thread=True, exclusive=False, group="prefetch-thread")
    def _do_prefetch(self, tid: int, topic: dict) -> None:
        try:
            last_read = topic.get("last_read_post_number")
            near_post = last_read + 1 if isinstance(last_read, int) and last_read > 0 else None
            data = self.app.client.topic(tid, near_post=near_post)
        except ApiError:
            return
        self.app.call_from_thread(self._store_prefetch, tid, data)

    def _store_prefetch(self, tid: int, data: dict) -> None:
        self._thread_prefetch[tid] = (data, time.monotonic())
        # Keep cache from growing unbounded.
        if len(self._thread_prefetch) > 10:
            self._thread_prefetch.pop(next(iter(self._thread_prefetch)))

    # --- n/p next/prev unread ---
    def _unread_jump(self, direction: int) -> None:
        tab_id = self._current_tab_id()
        if not tab_id.startswith("tab-"):
            return
        name = tab_id[4:]
        key = f"feed:{name}" if name in FEEDS else name
        topics = self._view_data.get(key)
        if not topics:
            return
        try:
            t = self.query_one(f"#tbl-{name}", DataTable)
        except Exception:  # noqa: BLE001
            return
        cur = t.cursor_row
        indices = range(cur + 1, len(topics)) if direction > 0 else range(cur - 1, -1, -1)
        for i in indices:
            if _topic_state(topics[i]) in ("new", "unread"):
                t.move_cursor(row=i)
                return

    def action_next_unread(self) -> None:
        self._unread_jump(1)

    def action_prev_unread(self) -> None:
        self._unread_jump(-1)

    # --- tab navigation ---
    @on(TabbedContent.TabActivated)
    def _tab_changed(self, event: TabbedContent.TabActivated) -> None:
        # Only load data; don't steal focus from the tab bar so the user can
        # keep navigating with ← / → without a round-trip.
        self._load_for_tab(self._current_tab_id())

    def action_tabs_down(self) -> None:
        """Fires when 'down' wasn't consumed by a widget: tab bar → current table/input, search box → results."""
        focused = self.focused
        if focused is None or isinstance(focused, Tabs):
            self._focus_active_table()
        elif getattr(focused, "id", None) == "search-input":
            self.query_one("#tbl-search").focus()

    def action_tabs_up(self) -> None:
        """Search box → tab bar."""
        if getattr(self.focused, "id", None) == "search-input":
            self.query_one(Tabs).focus()

    def _current_tab_id(self) -> str:
        return self.query_one("#tabs", TabbedContent).active or "tab-latest"

    def _tab_cycle(self) -> list[str]:
        return [t for t in TAB_ORDER if t != "tab-staff" or self.app.site.is_staff]

    def action_next_tab(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        order = self._tab_cycle()
        idx = order.index(tabs.active) if tabs.active in order else 0
        tabs.active = order[(idx + 1) % len(order)]

    def action_prev_tab(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        order = self._tab_cycle()
        idx = order.index(tabs.active) if tabs.active in order else 0
        tabs.active = order[(idx - 1) % len(order)]

    def action_reload(self) -> None:
        self._load_for_tab(self._current_tab_id())
        if self._current_tab_id() != "tab-notifications":
            self._load_notifications()

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
        elif tab_id == "tab-staff":
            self._load_staff()

    # --- feeds ---

    def _fetch_feed_page(self, feed: str, page: int) -> tuple[list[dict], bool]:
        """One page of a feed tab, honoring its category/tag filter. Blocking."""
        client = self.app.client
        path = FEED_PATHS[feed]
        flt = self._filters.get(feed)
        if flt and flt[0] == "category":
            d = client.category_feed(flt[1], path, page)
        elif flt and flt[0] == "tag":
            d = client.topic_list(f"/tag/{flt[1]}/l/{path}", page)
        else:
            d = client.feed(path, page)
        tl = d.get("topic_list") or {}
        if tl.get("top_tags"):
            self._top_tags = [t if isinstance(t, str) else t.get("name", "") for t in tl["top_tags"]]
        return tl.get("topics") or [], bool(tl.get("more_topics_url"))

    def _feed_label(self, feed: str) -> str:
        flt = self._filters.get(feed)
        if not flt:
            return FEED_LABELS[feed]
        name = flt[1].get("name", "") if flt[0] == "category" else f"#{flt[1]}"
        return f"{FEED_LABELS[feed]} · {_truncate(name, 14)}"

    @work(thread=True, exclusive=True, group="feed")
    def _load_feed(self, feed: str) -> None:
        try:
            topics, more = self._fetch_feed_page(feed, 0)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Load")
            return
        self.app.call_from_thread(self._populate_feed, feed, topics, more)

    def _populate_feed(self, feed: str, topics: list[dict], has_more: bool = False, *, redraw: bool = False) -> None:
        """Show a freshly loaded first page, or with ``redraw`` just re-render what's loaded (on resize)."""
        if not redraw:
            self._live_new.pop(feed, None)
            self._set_tab_badge(f"tab-{feed}", self._feed_label(feed), 0)
            self._feed_page[feed] = 0
            self._feed_has_more[feed] = has_more
            self._feed_loading_more.discard(feed)
        self._view_data[f"feed:{feed}"] = topics
        t = self.query_one(f"#tbl-{feed}", DataTable)
        prev = t.cursor_row
        t.clear()
        # State(3) + Posts with time under it(11).
        title_w = self._title_width(right_reserved=14)
        for topic in topics:
            self._add_feed_row(t, feed, topic, title_w)
        if not topics:
            t.add_row("", Text("Nothing here.", style="dim"), "", key="none")
        self._restore_cursor(t, prev)

    def _add_feed_row(self, t: DataTable, feed: str, topic: dict, title_w: int) -> None:
        key = f"topic:{topic.get('id')}"
        self._topic_cache[key] = topic
        t.add_row(
            _state_cell(topic),
            _title_cell(topic, title_w, self.app.site),
            _posts_cell(topic, with_time=True),
            key=key,
            height=2,
        )

    @work(thread=True, exclusive=True, group="feed-more")
    def _load_more_feed(self, feed: str) -> None:
        next_page = self._feed_page.get(feed, 0) + 1
        try:
            topics, more = self._fetch_feed_page(feed, next_page)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Load more")
            self._feed_loading_more.discard(feed)
            return
        self.app.call_from_thread(self._append_feed_page, feed, topics, more, next_page)

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
        title_w = self._title_width(right_reserved=14)
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
        if feed == "search":
            self._search_load_more()
            return
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
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Load")
            return
        self.app.call_from_thread(self._populate_categories, cats)

    def _populate_categories(self, cats: list[dict]) -> None:
        self._categories_cache = cats
        self._view_data["cats"] = cats
        t = self.query_one("#tbl-categories", DataTable)
        prev = t.cursor_row
        t.clear()
        total = self.size.width or 80
        name_w = 30
        desc_w = max(10, total - name_w - 14)
        # Parents in the server's order, each followed by its subcategories.
        by_parent: dict[int | None, list[dict]] = {}
        for c in cats:
            by_parent.setdefault(c.get("parent_category_id"), []).append(c)
        for kids in by_parent.values():
            kids.sort(key=lambda c: (c.get("position") or 0, c.get("name", "")))
        ids = {c["id"] for c in cats}
        roots = [c for c in cats if c.get("parent_category_id") not in ids]

        def add(c: dict, lead: str, kids_lead: str) -> None:
            """``lead`` goes before this row's name; ``kids_lead`` before its children's branches."""
            key = f"cat:{c.get('id')}"
            self._topic_cache[key] = c
            name = Text(lead, style="dim")
            name.append(_truncate(c.get("name", ""), name_w - len(lead)), style="" if lead else "bold")
            desc = plain_text(c.get("description") or c.get("description_text") or "")
            t.add_row(name, str(c.get("topic_count", 0)), Text(_truncate(desc, desc_w), style="dim"), key=key)
            kids = by_parent.get(c["id"], [])
            for i, kid in enumerate(kids):
                last = i == len(kids) - 1
                add(kid, kids_lead + ("└ " if last else "├ "), kids_lead + ("  " if last else "│ "))

        for root in roots:
            add(root, "", "")
        self._restore_cursor(t, prev)

    # --- messages (PMs) ---
    @work(thread=True, exclusive=True, group="pms")
    def _load_messages(self) -> None:
        try:
            user = self.app.site.username or self.app.cfg.username
            if not user:
                user = self.app.client.current_user().get("username", "")
            if not user:
                self.app.call_from_thread(self.app.reauth)
                return
            if not self.app.cfg.username:
                self.app.cfg.username = user
                self.app.cfg.save()
            msgs = (self.app.client.private_messages(user).get("topic_list") or {}).get("topics") or []
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Load")
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
            notifs = self.app.client.notifications().get("notifications") or []
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Load")
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
                _truncate(self.app.site.notification_name(n.get("notification_type")), 14),
                _truncate(who, 14),
                _truncate(title, title_w),
                key=key,
            )
        self._restore_cursor(t, prev)
        self._update_notif_badge()

    def _is_mod_inbox_notif(self, n: dict) -> bool:
        name = self.app.site.notification_types.get(n.get("notification_type"))
        group = (n.get("data") or {}).get("group_name")
        return self.app.site.is_staff and name == "group_message_summary" and group == "moderators"

    @work(thread=True, group="notif-read")
    def _notif_read_worker(self, n: dict) -> None:
        try:
            self.app.client.mark_notification_read(n.get("id"))
        except ApiError:
            return
        n["read"] = True
        self.app.call_from_thread(self._populate_notifications, self._notifications_cache)

    def _notif_url(self, n: dict) -> str:
        base = self.app.client.base_url
        data = n.get("data") or {}
        if n.get("topic_id"):
            slug = n.get("slug") or "-"
            pn = n.get("post_number")
            return f"{base}/t/{slug}/{n['topic_id']}" + (f"/{pn}" if isinstance(pn, int) and pn > 0 else "")
        name = self.app.site.notification_types.get(n.get("notification_type"))
        if name == "group_message_summary" and data.get("group_name"):
            user = data.get("username") or self.app.site.username or self.app.cfg.username
            return f"{base}/u/{user}/messages/group/{data['group_name']}"
        if data.get("badge_id"):
            return f"{base}/badges/{data['badge_id']}/{data.get('badge_slug') or '-'}"
        url = data.get("url") or ""
        if url:
            return url if url.startswith("http") else base + url
        return ""

    def action_copy_link(self) -> None:
        if self._current_tab_id() != "tab-notifications":
            return
        t = self.query_one("#tbl-notifications", DataTable)
        try:
            key = t.coordinate_to_cell_key((t.cursor_row, 0)).row_key.value
        except Exception:  # noqa: BLE001
            return
        url = self._notif_url(self._topic_cache.get(key or "", {}))
        if not url:
            self.app.notify("No link for this notification.", severity="warning")
        elif copy_to_clipboard(url):
            self.app.notify(f"Copied: {url}")
        else:
            self.app.notify("No clipboard tool found.", severity="warning")

    def _update_notif_badge(self, count: int | None = None) -> None:
        if count is None:
            count = sum(1 for n in self._notifications_cache if not n.get("read"))
        self._set_tab_badge("tab-notifications", "Notifications", count)

    # --- live updates ---

    def on_live_notification(self, data: dict) -> None:
        count = data.get("all_unread_notifications_count")
        if count is None:
            count = (data.get("unread_notifications") or 0) + (data.get("unread_high_priority_notifications") or 0)
        self._update_notif_badge(count)
        if self._current_tab_id() == "tab-notifications":
            self._load_notifications()

    def on_live_review_count(self, data: dict) -> None:
        if "reviewable_count" in data:
            self._set_staff_counts({"review": int(data["reviewable_count"] or 0)})

    def _on_live_topics(self, feed: str, data: dict) -> None:
        tid = data.get("topic_id")
        if not tid:
            return
        shown = {t.get("id") for t in self._view_data.get(f"feed:{feed}") or []}
        if tid in shown and feed == "unseen":
            return
        self._live_new.setdefault(feed, set()).add(tid)
        self._set_tab_badge(f"tab-{feed}", self._feed_label(feed), len(self._live_new[feed]), mark="↑")

    # --- feed actions ---

    def _current_feed(self) -> str | None:
        tab = self._current_tab_id()
        name = tab[4:] if tab.startswith("tab-") else ""
        return name if name in FEEDS else None

    def _highlighted_topic(self) -> dict | None:
        tab = self._current_tab_id()
        try:
            t = self.query_one(f"#tbl-{tab[4:]}", DataTable)
            key = t.coordinate_to_cell_key((t.cursor_row, 0)).row_key.value
        except Exception:  # noqa: BLE001
            return None
        return self._topic_cache.get(key or "") if key and key.startswith("topic:") else None

    def action_filter(self) -> None:
        if self._current_tab_id() == "tab-search":
            self.action_search_filters()
            return
        feed = self._current_feed()
        if feed is None:
            self.app.notify("Filters work on the Latest, Unseen and Search tabs.")
            return
        site = self.app.site
        items: list[tuple[str, str]] = []
        if self._filters.get(feed):
            items.append(("clear", "✕ Clear filter"))
        items.append(("", "CATEGORIES"))
        for cid in sorted(site.categories, key=lambda c: site.category_label(c).lower()):
            items.append((f"c:{cid}", site.category_label(cid)))
        if self._top_tags:
            items.append(("", "TAGS"))
            items += [(f"t:{tag}", f"#{tag}") for tag in self._top_tags]
        items.append(("tag?", "Other tag…"))

        def done(choice: str | None) -> None:
            if not choice:
                return
            if choice == "clear":
                self._filters.pop(feed, None)
            elif choice.startswith("c:"):
                self._filters[feed] = ("category", site.category(int(choice[2:])))
            elif choice.startswith("t:"):
                self._filters[feed] = ("tag", choice[2:])
            elif choice == "tag?":
                def got(tag: str | None) -> None:
                    if tag:
                        self._filters[feed] = ("tag", tag.lstrip("#").strip())
                        self._load_feed(feed)

                self.app.push_screen(PromptModal("Filter by tag:", "tag name", complete=complete.tags(self.app)), got)
                return
            self._load_feed(feed)

        self.app.push_screen(MenuModal(f"Filter {FEED_LABELS[feed]}", items), done)

    def action_dismiss_unseen(self) -> None:
        if self._current_feed() != "unseen":
            self.app.notify("Dismiss works on the Unseen tab.")
            return
        flt = self._filters.get("unseen")
        if flt and flt[0] == "tag":
            # The server can't dismiss by tag, so dismiss exactly the topics on screen.
            ids = [t["id"] for t in self._view_data.get("feed:unseen") or [] if t.get("id")]
            if not ids:
                self.app.notify("Nothing to dismiss.")
                return
            question = f"Dismiss the {len(ids)} topic{'s' if len(ids) != 1 else ''} shown for #{flt[1]}?"
            category_id = None
        else:
            ids = []
            cat = flt[1] if flt else None
            category_id = cat.get("id") if cat else None
            scope = f" in {cat.get('name')}" if cat else ""
            question = f"Dismiss all new and unread{scope}?"

        def done(ok: bool | None) -> None:
            if ok:
                self._dismiss_worker(category_id, ids)

        self.app.push_screen(ConfirmModal(question), done)

    @work(thread=True, group="dismiss")
    def _dismiss_worker(self, category_id: int | None, topic_ids: list[int]) -> None:
        try:
            if topic_ids:
                self.app.client.dismiss_topic_ids(topic_ids)
            else:
                self.app.client.dismiss_new(category_id)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Dismiss")
            return
        self.app.call_from_thread(self.app.notify, "Dismissed.")
        self.app.call_from_thread(self._load_feed, "unseen")

    def action_topic_level(self) -> None:
        topic = self._highlighted_topic()
        if topic is None:
            self.app.notify("Highlight a topic first.")
            return
        cur = str(topic.get("notification_level", 1))
        items = [(lvl, ("• " if lvl == cur else "  ") + name) for lvl, name in NOTIFICATION_LEVELS]

        def done(choice: str | None) -> None:
            if choice is not None and choice != cur:
                self._level_worker(topic, int(choice))

        self.app.push_screen(MenuModal(_truncate(topic.get("title", ""), 50), items), done)

    @work(thread=True, group="level")
    def _level_worker(self, topic: dict, level: int) -> None:
        try:
            self.app.client.set_topic_notification_level(int(topic["id"]), level)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Notification level")
            return
        topic["notification_level"] = level
        name = dict(NOTIFICATION_LEVELS)[str(level)]
        self.app.call_from_thread(self.app.notify, f"{name}: {_truncate(topic.get('title', ''), 40)}")

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
                tid = topic.get("id")
                prefetched = self._thread_prefetch.pop(int(tid), None) if tid else None
                self.app.push_screen(ThreadScreen(topic, prefetched=prefetched))
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
                post_number = notif.get("post_number")
                target = post_number if isinstance(post_number, int) and post_number > 0 else None
                self.app.push_screen(ThreadScreen(topic, target_post=target))
            elif self._is_mod_inbox_notif(notif):
                self._open_mod_inbox()
                if not notif.get("read"):
                    self._notif_read_worker(notif)
        elif not self._search_row_selected(key):
            self._staff_row_selected(key)

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
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Load")
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
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Post")
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
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Send")
            return
        self.app.call_from_thread(self.app.notify, "Message sent.", severity="information")

    def action_upload(self) -> None:
        def _done(path: str | None) -> None:
            if path:
                self._do_upload(path)
        self.app.push_screen(FilePickerModal("Upload to forum"), _done)

    def action_my_profile(self) -> None:
        from .user_profile import UserProfileScreen

        me = self.app.site.username or self.app.cfg.username
        if me:
            self.app.push_screen(UserProfileScreen(me))

    def action_leaderboard(self) -> None:
        self.app.push_screen(LeaderboardScreen())

    @work(thread=True, exclusive=True, group="upload")
    def _do_upload(self, path: str) -> None:
        upload_and_copy(self.app, path)


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
        t.add_columns(" ", "Title", "Posts", "Last")
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
            d = self.app.client.category_feed(self._cat)
            topics = (d.get("topic_list") or {}).get("topics") or []
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Load")
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
