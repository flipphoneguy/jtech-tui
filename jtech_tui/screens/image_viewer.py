from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import Header, LoadingIndicator, Static

from .. import images
from ..util import truncate
from .smart_footer import SmartFooter


class ImageViewerScreen(Screen):
    """Full screen viewer for a list of ``(label, url)`` images."""

    BINDINGS = [
        Binding("escape,q", "back", "Back"),
        Binding("right,down,l,j,space", "step(1)", "Next", key_display="→"),
        Binding("left,up,h,k,b", "step(-1)", "Prev", key_display="←"),
        Binding("o", "open_url", "Browser"),
        Binding("y", "copy_link", "Copy link"),
    ]

    def __init__(self, items: list[tuple[str, str]], title: str) -> None:
        super().__init__()
        self._items = items
        self._title = title
        self._i = 0

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "step":
            return len(self._items) > 1
        return True

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="thread-header")
        yield LoadingIndicator(id="loader")
        yield Static(id="viewer-error")
        with Container(id="viewer-box"):
            yield images.widget(id="viewer-img")
        yield SmartFooter()

    def on_mount(self) -> None:
        self.sub_title = self._title
        self._show()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_step(self, step: int) -> None:
        self._i = (self._i + step) % len(self._items)
        self._show()

    def action_open_url(self) -> None:
        self.app.open_url(self._items[self._i][1])

    def action_copy_link(self) -> None:
        self.app.copy_link(self._items[self._i][1])

    def _show(self) -> None:
        label, url = self._items[self._i]
        count = f"{self._i + 1}/{len(self._items)} · " if len(self._items) > 1 else ""
        self.query_one("#thread-header", Static).update(f"{count}{truncate(label, 80)}")
        self.query_one("#viewer-img").image = None
        self.query_one("#viewer-error").display = False
        self.query_one("#loader").display = True
        self._load(self._i, url)

    @work(thread=True, exclusive=True, group="image")
    def _load(self, i: int, url: str) -> None:
        try:
            img = images.fetch(self.app.client, url)
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self._failed, i, e)
            return
        self.app.call_from_thread(self._loaded, i, img)

    def _loaded(self, i: int, img) -> None:
        if i != self._i:
            return
        self.query_one("#loader").display = False
        self.query_one("#viewer-img").image = img

    def _failed(self, i: int, e: Exception) -> None:
        if i != self._i:
            return
        self.query_one("#loader").display = False
        err = self.query_one("#viewer-error", Static)
        err.update(f"Couldn't load this image: {e}\no opens it in the browser.")
        err.display = True
