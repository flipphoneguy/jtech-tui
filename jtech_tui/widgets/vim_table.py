from __future__ import annotations

from typing import Callable

from textual.binding import Binding
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable, Tabs


class VimDataTable(DataTable):
    """DataTable with j/k/g/G, up-at-top handoff, request-more-at-bottom and optional ←/→ hooks."""

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

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Called with -1 / +1 on ←/→ instead of scrolling sideways.
        self.sideways: Callable[[int], None] | None = None
        # Widget that gets focus on ↑ from the first row (default: the screen's tab bar).
        self.up_target: Callable[[], Widget | None] | None = None

    def action_cursor_top(self) -> None:
        if self.row_count:
            self.move_cursor(row=0)

    def action_cursor_bottom(self) -> None:
        if self.row_count:
            self.move_cursor(row=self.row_count - 1)

    def action_cursor_up(self) -> None:
        if self.cursor_row == 0:
            target = self.up_target() if self.up_target else None
            if target is None:
                try:
                    target = self.screen.query_one(Tabs)
                except NoMatches:
                    target = None
            if target is not None:
                target.focus()
                return
        super().action_cursor_up()

    def action_cursor_down(self) -> None:
        at_bottom = self.row_count and self.cursor_row >= self.row_count - 1
        super().action_cursor_down()
        if at_bottom:
            self.post_message(self.RequestMore(self))

    def action_cursor_left(self) -> None:
        if self.sideways is not None:
            self.sideways(-1)
        else:
            super().action_cursor_left()

    def action_cursor_right(self) -> None:
        if self.sideways is not None:
            self.sideways(1)
        else:
            super().action_cursor_right()
