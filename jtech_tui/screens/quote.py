from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static, TextArea
from textual.widgets.text_area import Selection

from .composer import FitsScreen

HINT = "hjkl/arrows move · w b word · 0 $ line · g G ends · v select · enter add to reply · y copy · esc"


class QuoteText(TextArea):
    """Read-only post text. Movement keys stretch the selection while ``v`` is on."""

    BINDINGS = [
        Binding("h,left", "move('left')", show=False),
        Binding("l,right", "move('right')", show=False),
        Binding("j,down", "move('down')", show=False),
        Binding("k,up", "move('up')", show=False),
        Binding("w", "move('word_right')", show=False),
        Binding("b", "move('word_left')", show=False),
        Binding("0,home", "move('line_start')", show=False),
        Binding("dollar_sign,end", "move('line_end')", show=False),
        Binding("g", "ends(False)", show=False),
        Binding("G", "ends(True)", show=False),
        Binding("v", "visual", show=False),
        Binding("enter", "done('add')", show=False),
        Binding("y", "done('copy')", show=False),
        Binding("escape", "back", show=False),
    ]

    visual = False

    def action_move(self, where: str) -> None:
        getattr(self, f"action_cursor_{where}")(select=self.visual)

    def action_ends(self, bottom: bool) -> None:
        self.move_cursor(self.document.end if bottom else (0, 0), select=self.visual)

    def action_visual(self) -> None:
        self.visual = not self.visual
        if not self.visual:
            self.selection = Selection.cursor(self.cursor_location)
        self.border_subtitle = "SELECT" if self.visual else ""

    def action_done(self, kind: str) -> None:
        text = (self.selected_text or self.text).strip()
        if text:
            self.screen.dismiss((kind, text))

    def action_back(self) -> None:
        if self.visual or self.selected_text:
            self.visual = True
            self.action_visual()
        else:
            self.screen.dismiss(None)


class QuoteModal(FitsScreen, ModalScreen[tuple[str, str] | None]):
    """Pick part of a post to quote or copy. Returns ('add', text), ('copy', text) or None."""

    DEFAULT_CSS = """
    QuoteModal QuoteText { height: auto; max-height: 24; margin: 0 0 1 0; }
    """

    def __init__(self, title: str, raw: str) -> None:
        super().__init__()
        self._title = title
        self._raw = raw

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box", classes="wide"):
            yield Static(self._title, id="modal-title")
            yield QuoteText(self._raw, read_only=True, soft_wrap=True, highlight_cursor_line=False)
            yield Static(HINT, id="modal-hint")

    def on_mount(self) -> None:
        self.query_one(QuoteText).focus()
