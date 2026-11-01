from __future__ import annotations

from typing import Callable

from rich.text import Text
from textual import on, work
from textual.actions import SkipAction
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

# (text that replaces the word being typed, label shown in the list)
Suggestion = tuple[str, Text | str]
Fetch = Callable[[str], list[Suggestion]]


class SuggestBox(Vertical):
    """An Input with a dropdown of suggestions for the word being typed.

    ``fetch`` gets that word and may block; it runs in a worker. ``seps`` are the characters that split words, so a list like ``a, b, c`` completes only the last one.
    """

    DEFAULT_CSS = """
    SuggestBox { height: auto; }
    SuggestBox > OptionList { display: none; }
    SuggestBox.open > OptionList { display: block; }
    """

    BINDINGS = [
        Binding("down", "move(1)", show=False),
        Binding("up", "move(-1)", show=False),
        Binding("tab", "pick", show=False),
        Binding("escape", "close", show=False),
    ]

    def __init__(
        self,
        fetch: Fetch,
        *,
        value: str = "",
        placeholder: str = "",
        input_id: str | None = None,
        seps: str = "",
        min_chars: int = 1,
    ) -> None:
        super().__init__()
        self._fetch = fetch
        self._value = value
        self._placeholder = placeholder
        self._input_id = input_id
        self._seps = seps
        self._min = min_chars
        self._items: list[Suggestion] = []
        self._timer = None
        # The value we just filled in, so it doesn't pop the list open again.
        self._filled: str | None = None

    def compose(self) -> ComposeResult:
        yield Input(value=self._value, placeholder=self._placeholder, id=self._input_id)
        ol = OptionList()
        ol.can_focus = False
        yield ol

    @property
    def input(self) -> Input:
        return self.query_one(Input)

    @property
    def is_open(self) -> bool:
        return self.has_class("open") and bool(self._items)

    def _word(self) -> tuple[int, str]:
        v = self.input.value
        start = max((v.rfind(s) for s in self._seps), default=-1) + 1
        while start < len(v) and v[start] == " ":
            start += 1
        return start, v[start:]

    @on(Input.Changed)
    def _changed(self, event: Input.Changed) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        _, word = self._word()
        if event.value == self._filled or len(word) < self._min:
            self._close()
            return
        self._timer = self.set_timer(0.25, lambda: self._load(word))

    @work(thread=True, exclusive=True, group="suggest")
    def _load(self, word: str) -> None:
        try:
            items = self._fetch(word)
        except Exception:  # noqa: BLE001
            items = []
        self.app.call_from_thread(self._show, word, items)

    def _show(self, word: str, items: list[Suggestion]) -> None:
        # Typed more since this was asked for.
        if self._word()[1] != word:
            return
        self._items = items[:8]
        if not self._items:
            self._close()
            return
        ol = self.query_one(OptionList)
        ol.clear_options()
        ol.add_options([Option(label) for _, label in self._items])
        ol.highlighted = 0
        self.add_class("open")

    def _close(self) -> None:
        self._items = []
        self.remove_class("open")

    def action_move(self, step: int) -> None:
        if not self.is_open:
            raise SkipAction()
        ol = self.query_one(OptionList)
        ol.highlighted = ((ol.highlighted or 0) + step) % len(self._items)

    def action_pick(self) -> None:
        if not self.is_open:
            raise SkipAction()
        value = self._items[self.query_one(OptionList).highlighted or 0][0]
        start, _ = self._word()
        new = self.input.value[:start] + value
        self._filled = new
        self.input.value = new
        self.input.cursor_position = len(new)
        self._close()

    def action_close(self) -> None:
        if not self.is_open:
            raise SkipAction()
        self._close()

    @on(OptionList.OptionSelected)
    def _clicked(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.query_one(OptionList).highlighted = event.option_index
        self.action_pick()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        # Enter picks while the list is open; the next enter submits.
        if self.is_open:
            event.stop()
            self.action_pick()
