from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Static

from .composer import FitsScreen


_KEY_NAMES = {
    "escape": "esc",
    "enter": "⏎",
    "plus": "+",
    "slash": "/",
    "space": "␣",
    "home": "home",
    "end": "end",
    "question_mark": "?",
    "exclamation_mark": "!",
    "number_sign": "#",
    "ctrl+f": "^f",
}


def key_label(key: str) -> str:
    """``escape,q`` -> ``esc/q``."""
    return "/".join(_KEY_NAMES.get(k, k) for k in key.split(","))


def collect_screen_bindings(screen, app) -> list[tuple[str, str]]:
    """Return ``(key, description)`` pairs for the screen + app bindings.

    Filters to bindings with ``show=True`` and a description.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for src in (getattr(screen, "BINDINGS", []) or [], getattr(app, "BINDINGS", []) or []):
        for b in src:
            if not isinstance(b, Binding):
                continue
            if not b.show or not b.description or not b.description.strip():
                continue
            check = getattr(screen, "check_action", None)
            if src is getattr(screen, "BINDINGS", None) and check is not None and not check(b.action, ()):
                continue
            if b.key in seen:
                continue
            seen.add(b.key)
            out.append((key_label(b.key), b.description))
    return out


class BindingsHelpModal(FitsScreen, ModalScreen[None]):
    """Lists every visible binding on the current screen."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("?", "close", "Close"),
    ]

    def __init__(self, bindings: list[tuple[str, str]]) -> None:
        super().__init__()
        # Note: avoid naming this `_bindings` — Textual uses that attribute name
        # internally on DOMNode/Widget and shadowing it breaks binding resolution.
        self._binding_entries = bindings

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static("Keybindings", id="modal-title")
            with VerticalScroll(id="help-scroll"):
                body = (
                    "\n".join(f"  [b]{k}[/b]   {d}" for k, d in self._binding_entries)
                    or "(no bindings)"
                )
                yield Static(body, markup=True, id="help-body")
            yield Static("esc · q · ?  to close", id="modal-hint")

    def action_close(self) -> None:
        self.dismiss(None)


class SmartFooter(Widget):
    """Single-line footer that truncates overflow to a '? all keys' hint.

    Hosting screens should bind ``?`` so users can actually open the help
    modal (widget BINDINGS require focus, which the footer never has).
    """

    DEFAULT_CSS = """
    SmartFooter {
        dock: bottom;
        height: 1;
        background: $accent 20%;
        color: $text;
    }
    """

    def render(self) -> Text:
        items = collect_screen_bindings(self.screen, self.app)
        if not items:
            return Text("")
        width = self.size.width or 80
        tail = "  ? all keys "
        tail_len = len(tail)
        line = Text()
        for i, (key, desc) in enumerate(items):
            piece = f" {key} {desc} "
            more_left = i < len(items) - 1
            reserve = tail_len if more_left else 0
            if line.cell_len + len(piece) + reserve > width:
                line.append("  ")
                line.append("? all keys", style="reverse bold")
                line.append(" ")
                return line
            line.append(" ")
            line.append(key, style="bold")
            line.append(f" {desc} ")
        return line

    def on_resize(self, event: events.Resize) -> None:
        self.refresh()

    def on_click(self, event: events.Click) -> None:
        # Clicking anywhere on the footer opens the help modal.
        self.app.push_screen(BindingsHelpModal(collect_screen_bindings(self.screen, self.app)))
