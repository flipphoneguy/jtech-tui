"""Search tab: paged results (one row per topic) and a filter builder that writes Discourse search syntax."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rich.text import Text
from textual import work
from textual.widgets import DataTable, Input

from ..api import ApiError
from ..util import parse_duration, parse_when, time_ago, truncate
from .composer import MenuModal, PromptModal

FILTERS = [
    ("category", "Category…"),
    ("user", "Posted by user…"),
    ("tag", "Tag…"),
    ("after", "After date…"),
    ("before", "Before date…"),
    ("in:title", "Titles only"),
    ("status:solved", "Solved only"),
    ("status:unsolved", "Unsolved only"),
    ("in:first", "First posts only"),
    ("order:latest", "Sort: newest"),
    ("order:likes", "Sort: most liked"),
    ("order:views", "Sort: most viewed"),
    ("clear", "✕ Clear filters"),
]


def _strip_filters(q: str) -> str:
    keep = [w for w in q.split() if not (w.startswith(("#", "@")) or ":" in w)]
    return " ".join(keep)


class SearchTabMixin:
    """Mixed into MainScreen."""

    _search_query = ""
    _search_page = 1
    _search_more = False
    _search_loading = False
    _search_targets: dict[int, int] = {}
    _search_pages: list[dict] = []

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-input":
            q = event.value.strip()
            if q:
                self._run_search(q)

    def _run_search(self, query: str, page: int = 1) -> None:
        self._search_query = query
        self._search_loading = True
        self._search_worker(query, page)

    @work(thread=True, exclusive=True, group="search")
    def _search_worker(self, query: str, page: int) -> None:
        try:
            res = self.app.client.search(query, page)
        except ApiError as e:
            self._search_loading = False
            self.app.call_from_thread(self.app.report_error, e, "Search")
            return
        self.app.call_from_thread(self._populate_search, res, page)

    def _search_load_more(self) -> None:
        if self._search_more and not self._search_loading and self._search_query:
            self._run_search(self._search_query, self._search_page + 1)

    def _populate_search(self, res: dict, page: int = 1) -> None:
        self._search_loading = False
        self._search_page = page
        self._search_more = bool((res.get("grouped_search_result") or {}).get("more_full_page_results"))
        if page == 1:
            self._search_pages = [res]
            self._reset_search_table()
        else:
            self._search_pages.append(res)
        added = self._add_search_rows(res)
        t = self.query_one("#tbl-search", DataTable)
        if page == 1:
            if not t.row_count:
                t.add_row(Text("No results.", style="dim"), "", "", key="none")
            self._restore_cursor(t, 0)
        elif added:
            self.app.notify(f"+{added} more results.")

    def _rebuild_search(self) -> None:
        """Redraw every loaded page (after a resize) and keep the cursor where it was."""
        pages = getattr(self, "_search_pages", [])
        if not pages:
            return
        t = self.query_one("#tbl-search", DataTable)
        row = t.cursor_row
        self._reset_search_table()
        for res in pages:
            self._add_search_rows(res)
        if not t.row_count:
            t.add_row(Text("No results.", style="dim"), "", "", key="none")
        self._restore_cursor(t, row)

    def _reset_search_table(self) -> None:
        self._search_targets = {}
        t = self.query_one("#tbl-search", DataTable)
        t.clear(columns=True)
        t.add_columns("Topic", "Match", "When")

    def _add_search_rows(self, res: dict) -> int:
        t = self.query_one("#tbl-search", DataTable)
        total = self.size.width or 80
        title_w = max(20, total * 2 // 5)
        blurb_w = max(20, total - title_w - 14)
        topics = {x.get("id"): x for x in res.get("topics") or []}
        users = {u.get("id"): u for u in res.get("users") or []}
        added = 0
        for p in res.get("posts") or []:
            tid = p.get("topic_id")
            if not tid or tid in self._search_targets:
                continue
            self._search_targets[tid] = p.get("post_number") or 1
            topic = topics.get(tid) or {"id": tid, "title": p.get("topic_title_headline") or ""}
            self._topic_cache[f"stopic:{tid}"] = topic
            title = Text(truncate(topic.get("title") or str(tid), title_w), style="bold")
            cat = self.app.site.category_label(topic.get("category_id"))
            if cat:
                title.append("\n" + truncate(cat, title_w), style="dim italic")
            who = p.get("username") or (users.get(p.get("user_id")) or {}).get("username") or ""
            blurb = Text()
            if who:
                blurb.append(f"@{who} #{p.get('post_number', '')}: ", style="dim")
            blurb.append(truncate((p.get("blurb") or "").replace("\n", " "), blurb_w))
            t.add_row(title, blurb, time_ago(p.get("created_at")), key=f"stopic:{tid}", height=2)
            added += 1
        return added

    def _search_row_selected(self, key: str) -> bool:
        if not key.startswith("stopic:"):
            return False
        from .thread import ThreadScreen

        tid = int(key.split(":", 1)[1])
        topic = self._topic_cache.get(key) or {"id": tid}
        self.app.push_screen(ThreadScreen(topic, target_post=self._search_targets.get(tid)))
        return True

    # --- filter builder ---

    def action_search_filters(self) -> None:
        def picked(choice: str | None) -> None:
            if not choice:
                return
            if choice == "clear":
                self._set_query(_strip_filters(self._query_box().value))
            elif choice == "category":
                site = self.app.site
                items = [
                    (str(cid), site.category_label(cid))
                    for cid in sorted(site.categories, key=lambda c: site.category_label(c).lower())
                ]
                self.app.push_screen(MenuModal("Search in category", items), self._add_category)
            elif choice == "user":
                self._ask_token("Posted by username:", lambda v: "@" + v.lstrip("@"))
            elif choice == "tag":
                self._ask_token("Tag:", lambda v: "tags:" + v.lstrip("#"))
            elif choice in ("after", "before"):
                def fmt(v: str, kind: str = choice) -> str | None:
                    secs = parse_duration(v)
                    if secs is not None:
                        # "1w" here means a week ago.
                        when = datetime.now(timezone.utc) - timedelta(seconds=secs)
                    else:
                        when = parse_when(v)
                    if when is None:
                        self.app.notify("Use e.g. 1w, 3mo, 2026-01-01.", severity="warning")
                        return None
                    return f"{kind}:{when.date().isoformat()}"

                self._ask_token(f"{choice.capitalize()} (e.g. 1w ago, 3mo ago, or 2026-01-01):", fmt)
            else:
                self._add_token(choice)

        self.app.push_screen(MenuModal("Search filters", FILTERS), picked)

    def _query_box(self) -> Input:
        return self.query_one("#search-input", Input)

    def _set_query(self, q: str) -> None:
        box = self._query_box()
        box.value = q.strip()
        box.focus()
        if box.value:
            self._run_search(box.value)

    def _add_token(self, token: str) -> None:
        words = self._query_box().value.split()
        if token.startswith("order:"):
            words = [w for w in words if not w.startswith("order:")]
        if token not in words:
            words.append(token)
        self._set_query(" ".join(words))

    def _ask_token(self, title: str, fmt) -> None:
        def done(v: str | None) -> None:
            if v:
                tok = fmt(v.strip())
                if tok:
                    self._add_token(tok)

        self.app.push_screen(PromptModal(title), done)

    def _add_category(self, choice: str | None) -> None:
        if not choice:
            return
        site = self.app.site
        cat = site.category(int(choice))
        parent = site.category(cat.get("parent_category_id"))
        slug = f"#{parent['slug']}:{cat['slug']}" if parent else f"#{cat.get('slug', '')}"
        self._add_token(slug)
