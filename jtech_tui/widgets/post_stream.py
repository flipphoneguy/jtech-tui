"""Line-API thread view.

Every post is rendered to a list of strips once per width. Inserting posts above the viewport shifts ``scroll_y`` by their exact height in the same call, so the view never jumps.
"""

from __future__ import annotations

from bisect import bisect_right
from typing import TYPE_CHECKING, Iterable


from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip

from ..render import Rendered, render_cooked
from ..util import time_ago

if TYPE_CHECKING:
    from ..site import Site

COLLAPSE_LINES = 40
COLLAPSED_PREVIEW = 10

C = {
    "rule": "#4e4e4e",
    "rule_sel": "#5fafff",
    "bar_sel": "bold #5fafff",
    "user": "bold #e4e4e4",
    "user_me": "bold #87d7af",
    "op": "bold #ffd75f",
    "meta": "#8a8a8a",
    "mod": "bold #1c1c1c on #5fafff",
    "admin": "bold #1c1c1c on #ff875f",
    "whisper": "#af87ff",
    "whisper_label": "bold #af87ff",
    "react": "#bcbcbc",
    "react_mine": "bold #1c1c1c on #87afd7",
    "solved": "bold #87d787",
    "deleted": "#d75f5f",
    "last_visit": "#d7af5f",
    "small": "#8a8a8a italic",
    "checked": "bold #ffaf00",
}


class Entry:
    """One post plus its render cache."""

    __slots__ = (
        "post", "collapsed", "reveal", "expand", "reply_to_user", "fold", "run",
        "_width", "_body", "_rendered", "_header", "_extra", "height", "shown",
    )

    def __init__(self, post: dict, reply_to_user: str | None = None) -> None:
        self.post = post
        self.reply_to_user = reply_to_user
        self.collapsed: bool | None = None
        self.reveal = False
        self.expand = False
        # Deleted posts (staff see them) start folded into one "N deleted posts" line.
        self.fold = True
        # Set on the first of a folded run: how many posts the line stands for.
        self.run = 0
        self._width = -1
        self._body: list[Strip] = []
        self._rendered: Rendered | None = None
        self._header: dict[tuple[bool, bool], Strip] = {}
        self._extra: list[Strip] = []
        self.height = 0
        # Height on screen: ``height``, or the fold line, or 0 inside a folded run.
        self.shown = 0

    @property
    def id(self) -> int:
        return int(self.post.get("id") or 0)

    @property
    def number(self) -> int:
        return int(self.post.get("post_number") or 0)

    @property
    def is_small(self) -> bool:
        return self.post.get("post_type") == 3

    @property
    def is_deleted(self) -> bool:
        return bool(self.post.get("deleted_at"))

    @property
    def folded(self) -> bool:
        return self.is_deleted and self.fold

    @property
    def is_whisper(self) -> bool:
        return bool(self.post.get("mod_is_whisper")) or self.post.get("post_type") == 4

    @property
    def rendered(self) -> Rendered | None:
        return self._rendered

    def invalidate(self) -> None:
        self._width = -1
        self._header.clear()


class PostStream(ScrollView, can_focus=True):
    """Scrollable list of posts with a post cursor."""

    BINDINGS = [
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("space,pagedown", "page_down", "Page down", show=False),
        Binding("b,pageup", "page_up", "Page up", show=False),
    ]

    DEFAULT_CSS = """
    PostStream {
        background: $surface;
        scrollbar-size-vertical: 1;
    }
    """

    class CursorMoved(Message):
        def __init__(self, stream: "PostStream", entry: Entry | None) -> None:
            super().__init__()
            self.stream = stream
            self.entry = entry

    class NeedMore(Message):
        def __init__(self, stream: "PostStream", direction: str) -> None:
            super().__init__()
            self.stream = stream
            self.direction = direction

    class Seen(Message):
        """Posts that have been on screen."""

        def __init__(self, stream: "PostStream", numbers: list[int]) -> None:
            super().__init__()
            self.stream = stream
            self.numbers = numbers

    def __init__(self, site: "Site | None", base_url: str, topic_id: int, **kw) -> None:
        super().__init__(**kw)
        self.site = site
        self.base_url = base_url
        self.topic_id = topic_id
        self.entries: list[Entry] = []
        self._starts: list[int] = []
        self._total = 0
        self.cursor = 0
        self.op_username = ""
        self.my_username = site.username if site is not None else ""
        self.more_above = False
        self.more_below = False
        # Post ids picked for bulk actions.
        self.checked: set[int] = set()
        self._seen: set[int] = set()
        self._pending_top = False
        # Last post read before this visit; a "last visit" line goes above the next one.
        self.last_read = 0
        self._divider: Entry | None = None

    # --- data ---

    def _content_width(self) -> int:
        return max(20, self.scrollable_content_region.width)

    def _make_entry(self, post: dict) -> Entry:
        return Entry(post)

    def _link_parents(self) -> None:
        by_number = {e.number: e.post for e in self.entries}
        for e in self.entries:
            rpn = e.post.get("reply_to_post_number")
            parent = by_number.get(rpn) if rpn else None
            e.reply_to_user = (
                (parent or {}).get("username")
                or (e.post.get("reply_to_user") or {}).get("username")
            )

    def set_posts(
        self,
        posts: list[dict],
        cursor_number: int | None = None,
        *,
        more_above: bool = False,
        more_below: bool = False,
    ) -> None:
        self.more_above = more_above
        self.more_below = more_below
        self.entries = [self._make_entry(p) for p in sorted(posts, key=lambda p: p.get("post_number") or 0)]
        self._link_parents()
        self._seen.clear()
        self.cursor = 0
        if cursor_number is not None:
            idx = self.index_of_number(cursor_number, nearest=True)
            if idx is not None:
                self.cursor = idx
        # The size may not be known yet (just made visible); _relayout puts the cursor at the top once it is.
        self._pending_top = True
        self._relayout(anchor=None)
        self._after_move()

    def add_posts(self, posts: Iterable[dict]) -> int:
        """Insert posts (above, below, or between) keeping the view still. Returns count added."""
        have = {e.id for e in self.entries}
        new = [p for p in posts if p.get("id") not in have]
        if not new:
            return 0
        anchor = self._anchor()
        cur = self.entries[self.cursor] if self.entries else None
        merged = self.entries + [self._make_entry(p) for p in new]
        merged.sort(key=lambda e: e.number)
        self.entries = merged
        self._link_parents()
        if cur is not None:
            self.cursor = self.entries.index(cur)
        self._relayout(anchor)
        return len(new)

    def update_post(self, post: dict) -> bool:
        for e in self.entries:
            if e.id == post.get("id"):
                anchor = self._anchor()
                e.post = post
                e.invalidate()
                self._link_parents()
                self._relayout(anchor)
                return True
        return False

    def remove_ids(self, ids: set[int]) -> None:
        if not ids:
            return
        anchor = self._anchor()
        cur = self.entries[self.cursor] if self.entries else None
        self.entries = [e for e in self.entries if e.id not in ids]
        self.checked -= ids
        if cur is not None and cur in self.entries:
            self.cursor = self.entries.index(cur)
        else:
            self.cursor = min(self.cursor, max(0, len(self.entries) - 1))
        self._relayout(anchor)

    def set_checked(self, ids: set[int]) -> None:
        changed = self.checked ^ ids
        self.checked = set(ids)
        for e in self.entries:
            if e.id in changed:
                e._header.clear()
        self.refresh()

    def refresh_entry(self, entry: Entry) -> None:
        anchor = self._anchor()
        entry.invalidate()
        self._relayout(anchor)

    # --- lookup ---

    @property
    def current(self) -> Entry | None:
        if 0 <= self.cursor < len(self.entries):
            return self.entries[self.cursor]
        return None

    def index_of_number(self, number: int, nearest: bool = False) -> int | None:
        for i, e in enumerate(self.entries):
            if e.number == number:
                return i
            if nearest and e.number > number:
                return i
        if nearest and self.entries:
            return len(self.entries) - 1
        return None

    def loaded_numbers(self) -> list[int]:
        return [e.number for e in self.entries]

    # --- deleted post folding ---

    def _fold_head(self, idx: int) -> int:
        while idx > 0 and self.entries[idx].shown == 0:
            idx -= 1
        return idx

    def toggle_fold(self, idx: int) -> None:
        """Fold or unfold the run of deleted posts around ``idx``."""
        lo = hi = idx
        while lo > 0 and self.entries[lo - 1].is_deleted:
            lo -= 1
        while hi + 1 < len(self.entries) and self.entries[hi + 1].is_deleted:
            hi += 1
        fold = not self.entries[idx].fold
        for e in self.entries[lo:hi + 1]:
            e.fold = fold
            e._header.clear()
        self.cursor = lo
        self._relayout(None)
        self.move_to(lo)

    # --- layout ---

    def _ensure(self, e: Entry, width: int) -> None:
        if e._width == width:
            return
        e._width = width
        e._header.clear()
        console = self.app.console
        inner = max(10, width - 2)
        opts = console.options.update(width=inner, height=None)
        if e.is_small:
            e._rendered = None
            e._body = []
            e._extra = []
            e.height = 2
            return
        r = render_cooked(
            e.post.get("cooked") or "",
            self.site,
            self.base_url,
            post=e.post,
            topic_id=self.topic_id,
            reveal_spoilers=e.reveal,
            expand_details=e.expand,
        )
        e._rendered = r
        lines = console.render_lines(r.body, opts, pad=False)
        body = [Strip(line) for line in lines]
        if e.collapsed is None:
            e.collapsed = len(body) > COLLAPSE_LINES
        if e.collapsed and len(body) > COLLAPSE_LINES:
            more = len(body) - COLLAPSED_PREVIEW
            body = body[:COLLAPSED_PREVIEW]
            body.append(Strip(Text(f"… {more} more lines · x to expand", style=C["meta"]).render(console)))
        e._body = body
        extra: list[Strip] = []
        if e.is_whisper:
            extra.append(self._strip(self._whisper_label(e.post), inner))
        e._extra = extra
        foot = self._footer(e.post)
        if foot is not None:
            e._body = e._body + [self._strip(foot, inner)]
        # header + whisper label + body + blank separator
        e.height = 1 + len(e._extra) + len(e._body) + 1

    def _strip(self, text: Text, width: int) -> Strip:
        text = text.copy()
        text.truncate(width, overflow="ellipsis")
        return Strip(text.render(self.app.console))

    def _divider_index(self) -> int | None:
        """Where the "last visit" line goes: the first unread post, or the fold line it's hidden under."""
        if not self.last_read:
            return None
        for i, e in enumerate(self.entries):
            if e.number > self.last_read:
                while i > 0 and self.entries[i].folded and self.entries[i - 1].folded:
                    i -= 1
                return i
        return None

    def _relayout(self, anchor: tuple[Entry, int] | None) -> None:
        width = self._content_width()
        starts: list[int] = []
        y = 0
        head: Entry | None = None
        div = self._divider_index()
        self._divider = self.entries[div] if div is not None else None
        for e in self.entries:
            self._ensure(e, width)
            if e.run:
                # The fold line's count is cached with the header.
                e._header.clear()
            e.run = 0
            if e.folded and head is not None:
                head.run += 1
                e.shown = 0
            elif e.folded:
                head = e
                e.run = 1
                e.shown = 2
            else:
                head = None
                e.shown = e.height
            if e is self._divider:
                e.shown += 1
            starts.append(y)
            y += e.shown
        if 0 <= self.cursor < len(self.entries) and self.entries[self.cursor].shown == 0:
            self.cursor = self._fold_head(self.cursor)
        self._starts = starts
        self._total = y
        # Reserve a screen of space while more posts exist below, so any post can sit at the top and arriving posts fill it without moving anything.
        pad = self._view_h() if self.more_below else 0
        self.virtual_size = Size(width, y + pad)
        if self._pending_top and self.entries:
            self._pending_top = self.scrollable_content_region.height <= 1
            self.scroll_to(y=starts[self.cursor], animate=False, force=True)
        elif anchor is not None and anchor[0] in self.entries:
            idx = self.entries.index(anchor[0])
            self.scroll_to(y=starts[idx] + anchor[1], animate=False, force=True)
        self.refresh()

    def _anchor(self) -> tuple[Entry, int] | None:
        """The entry to hold still across a relayout and its offset from the top of the view."""
        if not self.entries:
            return None
        top = int(self.scroll_y)
        if self._starts and self.cursor < len(self._starts):
            start = self._starts[self.cursor]
            if top <= start < top + self._view_h():
                return self.entries[self.cursor], top - start
        idx = max(0, bisect_right(self._starts, top) - 1)
        if idx >= len(self.entries):
            return None
        return self.entries[idx], top - self._starts[idx]

    def on_resize(self, event: events.Resize) -> None:
        if self.entries:
            self._relayout(self._anchor())

    # --- header / footer text ---

    def _badge(self, post: dict) -> Text | None:
        if post.get("admin"):
            return Text(" ADMIN ", style=C["admin"])
        if post.get("moderator"):
            return Text(" MOD ", style=C["mod"])
        return None

    def _header_text(self, e: Entry) -> Text:
        p = e.post
        t = Text()
        if e.is_small:
            t.append("⟲ ", style=C["small"])
            from ..render import plain_text

            t.append(plain_text(p.get("cooked") or "") or (p.get("action_code") or "").replace("_", " "), style=C["small"])
            t.append(f" · @{p.get('username', '')} · {time_ago(p.get('created_at'))}", style=C["meta"])
            return t
        user = p.get("username") or "?"
        style = C["user"]
        if user == self.my_username:
            style = C["user_me"]
        elif user == self.op_username:
            style = C["op"]
        t.append(f"@{user}", style=style)
        badge = self._badge(p)
        if badge is not None:
            t.append(" ")
            t.append_text(badge)
        status = p.get("user_status") or {}
        if status.get("emoji") and self.site is not None:
            t.append(" ")
            t.append_text(self.site.emoji.rich(status["emoji"]))
        t.append(f" · {time_ago(p.get('created_at'))}", style=C["meta"])
        rpn = p.get("reply_to_post_number")
        if rpn:
            who = f"@{e.reply_to_user} " if e.reply_to_user else ""
            t.append(f" · ↱ {who}#{rpn}", style=C["meta"])
        if (p.get("version") or 1) > 1:
            t.append(" · edited", style=C["meta"])
        if p.get("wiki"):
            t.append(" · wiki", style=C["meta"])
        if p.get("accepted_answer"):
            t.append(" · ✓ solution", style=C["solved"])
        if p.get("deleted_at") or p.get("user_deleted"):
            by = (p.get("deleted_by") or {}).get("username")
            t.append(f" · deleted by @{by}" if by else " · deleted", style=C["deleted"])
        if p.get("hidden"):
            t.append(" · hidden", style=C["deleted"])
        return t

    def _whisper_label(self, p: dict) -> Text:
        names: list[str] = []
        names += [f"@{u.get('username')}" for u in p.get("mod_whisper_targets") or [] if u.get("username")]
        for g in p.get("mod_whisper_target_groups") or []:
            names.append(str(g.get("name") or "group") if isinstance(g, dict) else str(g))
        for b in p.get("mod_whisper_target_badges") or []:
            names.append(f"🏅{b.get('name') if isinstance(b, dict) else b}")
        if not names:
            names = ["admins"] if p.get("mod_whisper_admins_only") else ["staff"]
        t = Text("🔒 WHISPER", style=C["whisper_label"])
        t.append(" → " + ", ".join(names), style=C["whisper"])
        return t

    def _footer(self, p: dict) -> Text | None:
        t = Text()
        mine = p.get("current_user_reaction") or {}
        mine_id = mine.get("id") if isinstance(mine, dict) else None
        reactions = [r for r in p.get("reactions") or [] if r.get("count")]
        if not reactions:
            for a in p.get("actions_summary") or []:
                if a.get("id") == 2 and a.get("count"):
                    reactions = [{"id": "+1", "count": a["count"]}]
                    if a.get("acted"):
                        mine_id = "+1"
        for r in reactions:
            if t.plain:
                t.append("  ")
            emoji = self.site.emoji.rich(r["id"]) if self.site is not None else Text(f":{r['id']}:")
            piece = Text.assemble(emoji, f" {r['count']}")
            if r["id"] == mine_id:
                piece.stylize(C["react_mine"])
            else:
                piece.stylize(C["react"])
            t.append_text(piece)
        replies = p.get("reply_count") or 0
        if replies:
            t.append(f"{'   ' if t.plain else ''}↩ {replies} repl{'y' if replies == 1 else 'ies'}", style=C["meta"])
        return t if t.plain else None

    def _header_strip(self, e: Entry, selected: bool, width: int) -> Strip:
        checked = e.id in self.checked
        key = (selected, checked)
        cached = e._header.get(key)
        if cached is not None:
            return cached
        rule_style = C["rule_sel"] if selected else C["rule"]
        if e.is_whisper and not selected:
            rule_style = C["whisper"]
        ch = "━" if selected else "─"
        lead = "┏" + ch if selected else ch + ch
        info = self._header_text(e)
        if checked:
            info = Text.assemble(("◉ ", C["checked"]), info)
        num = Text(f" #{e.number} " if not e.is_small else " ", style=C["meta"] if not selected else C["bar_sel"])
        avail = width - 3 - num.cell_len - 3
        info.truncate(max(5, avail), overflow="ellipsis")
        line = Text(lead + " ", style=rule_style)
        line.append_text(info)
        fill = max(1, width - line.cell_len - num.cell_len - 2)
        line.append(" " + ch * fill, style=rule_style)
        line.append_text(num)
        line.append(ch * 2, style=rule_style)
        line.truncate(width)
        strip = Strip(line.render(self.app.console)).crop(0, width)
        e._header[key] = strip
        return strip

    def _fold_strip(self, e: Entry, selected: bool, width: int) -> Strip:
        key = (selected, True)
        cached = e._header.get(key)
        if cached is not None:
            return cached
        n = e.run
        label = f" {n} deleted post{'s' if n != 1 else ''} · x to show "
        style = C["rule_sel"] if selected else C["deleted"]
        side = max(2, (width - len(label)) // 2)
        line = Text("─" * side, style=style)
        line.append(label, style=C["deleted"] if not selected else C["bar_sel"])
        line.append("─" * max(0, width - side - len(label)), style=style)
        line.truncate(width)
        strip = Strip(line.render(self.app.console)).crop(0, width)
        e._header[key] = strip
        return strip

    def _divider_strip(self, width: int) -> Strip:
        label = " last visit "
        side = max(2, (width - len(label)) // 2)
        line = Text("─" * side, style=C["last_visit"])
        line.append(label, style=C["last_visit"])
        line.append("─" * max(0, width - side - len(label)), style=C["last_visit"])
        line.truncate(width)
        return Strip(line.render(self.app.console)).crop(0, width)

    # --- painting ---

    def render_line(self, y: int) -> Strip:
        width = self.scrollable_content_region.width
        strip = self._line(int(self.scroll_y) + y, width)
        return strip.extend_cell_length(width).crop(0, width).apply_style(self.rich_style)

    def _line(self, abs_y: int, width: int) -> Strip:
        if abs_y >= self._total or not self.entries:
            if abs_y == self._total and self.more_below:
                return Strip(Text("  loading…", style=C["meta"]).render(self.app.console))
            return Strip.blank(width)
        idx = bisect_right(self._starts, abs_y) - 1
        e = self.entries[idx]
        row = abs_y - self._starts[idx]
        selected = idx == self.cursor
        if e is self._divider:
            if row == 0:
                return self._divider_strip(width)
            row -= 1
        if e.folded:
            return self._fold_strip(e, selected, width) if row == 0 else Strip.blank(width)
        if row == 0:
            return self._header_strip(e, selected, width)
        if e.is_small:
            return Strip.blank(width)
        row -= 1
        if selected:
            bar = Segment("┃ ", Style.parse(C["bar_sel"]))
        elif e.is_whisper:
            bar = Segment("┊ ", Style.parse(C["whisper"]))
        else:
            bar = Segment("  ")
        if row < len(e._extra):
            return Strip.join([Strip([bar]), e._extra[row]])
        row -= len(e._extra)
        if row < len(e._body):
            body = e._body[row]
            if e.is_whisper:
                body = body.apply_style(Style(dim=True))
            return Strip.join([Strip([bar]), body])
        return Strip.blank(width)

    def plain_lines(self, start: int, count: int) -> list[str]:
        """Text of virtual lines, for tests and debugging."""
        width = self.scrollable_content_region.width
        return [self._line(y, width).text for y in range(start, start + count)]

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    # --- cursor & scrolling ---

    def _view_h(self) -> int:
        return max(1, self.scrollable_content_region.height)

    def _span(self, idx: int) -> tuple[int, int]:
        start = self._starts[idx]
        return start, start + max(1, self.entries[idx].shown) - 1  # drop the blank separator

    def move_to(self, idx: int, align: str = "auto") -> None:
        if not self.entries:
            return
        idx = max(0, min(idx, len(self.entries) - 1))
        old = self.cursor
        self.cursor = idx
        if old != idx and 0 <= old < len(self.entries):
            self.entries[old]._header.clear()
        self.entries[idx]._header.clear()
        start, end = self._span(idx)
        h = self._view_h()
        top = int(self.scroll_y)
        if align == "top":
            self.scroll_to(y=start, animate=False, force=True)
        elif align == "bottom":
            self.scroll_to(y=max(start, end - h), animate=False, force=True)
        elif align == "none":
            pass
        elif start < top or end > top + h:
            if end - start <= h:
                target = start if start < top else end - h
            else:
                target = start
            self.scroll_to(y=target, animate=False, force=True)
        self.refresh()
        self._after_move()

    def _after_move(self) -> None:
        self.post_message(self.CursorMoved(self, self.current))
        self._check_edges()
        self._report_seen()

    def action_cursor_down(self) -> None:
        if not self.entries:
            return
        start, end = self._span(self.cursor)
        h = self._view_h()
        top = int(self.scroll_y)
        if end > top + h:
            self.scroll_to(y=min(top + max(1, h - 2), end - h), animate=False, force=True)
            self._report_seen()
            return
        nxt = self._step(self.cursor, 1)
        if nxt is None:
            if self.more_below:
                self.post_message(self.NeedMore(self, "below"))
            return
        self.move_to(nxt)

    def _step(self, idx: int, d: int) -> int | None:
        """The next post on screen from ``idx`` in direction ``d``, skipping ones inside a folded run."""
        i = idx + d
        while 0 <= i < len(self.entries) and self.entries[i].shown == 0:
            i += d
        return i if 0 <= i < len(self.entries) else None

    def action_cursor_up(self) -> None:
        if not self.entries:
            return
        start, _ = self._span(self.cursor)
        h = self._view_h()
        top = int(self.scroll_y)
        if start < top:
            self.scroll_to(y=max(start, top - max(1, h - 2)), animate=False, force=True)
            return
        prev = self._step(self.cursor, -1)
        if prev is None:
            if self.more_above:
                self.post_message(self.NeedMore(self, "above"))
            return
        ps, pe = self._span(prev)
        self.move_to(prev, align="bottom" if pe - ps > h else "auto")

    def action_page_down(self) -> None:
        self.scroll_to(y=int(self.scroll_y) + self._view_h() - 2, animate=False, force=True)

    def action_page_up(self) -> None:
        self.scroll_to(y=int(self.scroll_y) - self._view_h() + 2, animate=False, force=True)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if not self.entries or not self._starts:
            return
        # Keep the cursor on a visible post when scrolling with the mouse or paging.
        top = int(new_value)
        h = self._view_h()
        start, end = self._span(self.cursor) if self.cursor < len(self.entries) else (0, 0)
        if end < top or start >= top + h:
            # Posts inside a folded run share a start, so land on the fold line instead.
            idx = self._fold_head(max(0, bisect_right(self._starts, top) - 1))
            if self._starts[idx] + self.entries[idx].shown - 1 <= top:
                nxt = self._step(idx, 1)
                if nxt is not None:
                    idx = nxt
            if idx != self.cursor:
                old = self.cursor
                self.cursor = idx
                if old < len(self.entries):
                    self.entries[old]._header.clear()
                self.entries[idx]._header.clear()
                self.post_message(self.CursorMoved(self, self.current))
        self._check_edges()
        self._report_seen()

    def _check_edges(self) -> None:
        h = self._view_h()
        top = int(self.scroll_y)
        if self.more_above and top < h:
            self.post_message(self.NeedMore(self, "above"))
        if self.more_below and top + 2 * h >= self._total:
            self.post_message(self.NeedMore(self, "below"))

    def visible_numbers(self) -> list[int]:
        if not self.entries or not self._starts:
            return []
        top = int(self.scroll_y)
        bottom = top + self._view_h()
        lo = max(0, bisect_right(self._starts, top) - 1)
        out: list[int] = []
        for i in range(lo, len(self.entries)):
            if self._starts[i] >= bottom:
                break
            out.append(self.entries[i].number)
        return out

    def _report_seen(self) -> None:
        new = [n for n in self.visible_numbers() if n not in self._seen]
        if new:
            self._seen.update(new)
            self.post_message(self.Seen(self, new))

    def on_click(self, event: events.Click) -> None:
        abs_y = int(self.scroll_y) + event.y
        if not self.entries or abs_y >= self._total:
            return
        idx = bisect_right(self._starts, abs_y) - 1
        if idx != self.cursor:
            self.move_to(idx, align="none")
