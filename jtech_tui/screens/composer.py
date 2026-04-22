from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option


# Common Discourse reactions. The server decides which are accepted; unsupported
# ones will surface as an error in the notification.
REACTIONS: list[tuple[str, str, str]] = [
    ("ok_hand", "👌", "OK"),
    ("man_shrugging", "🤷", "Shrug"),
    ("+1", "👍", "Like"),
    ("folded_hands", "🙏", "Thanks"),
    ("laughing", "😆", "Laughing"),
    ("heart", "❤️", "Heart"),
    ("-1", "👎", "Dislike"),
    ("open_mouth", "😮", "Wow"),
    ("cry", "😢", "Sad"),
    ("clap", "👏", "Clap"),
    ("hugs", "🤗", "Hug"),
    ("confetti_ball", "🎉", "Celebrate"),
    ("thinking", "🤔", "Hmm"),
    ("rocket", "🚀", "Rocket"),
    ("eyes", "👀", "Eyes"),
]


_REACTION_META: dict[str, tuple[str, str]] = {rid: (emoji, label) for rid, emoji, label in REACTIONS}


class ReactionModal(ModalScreen[str | None]):
    """Pick a reaction to toggle on a post. Returns the reaction id or None.

    Pass ``supported_ids`` to restrict the list to reactions the server actually
    accepts. Empty/None shows the full fallback set.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, supported_ids: list[str] | None = None) -> None:
        super().__init__()
        self._supported = list(supported_ids or [])

    def _items(self) -> list[tuple[str, str, str]]:
        if not self._supported:
            return REACTIONS
        out: list[tuple[str, str, str]] = []
        for rid in self._supported:
            emoji, label = _REACTION_META.get(rid, (f":{rid}:", rid))
            out.append((rid, emoji, label))
        return out

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static("React to post", id="modal-title")
            yield OptionList(
                *[
                    Option(f"{emoji}   {label}", id=rid)
                    for rid, emoji, label in self._items()
                ],
                id="react-opts",
            )
            yield Static("enter toggle · esc cancel", id="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#react-opts", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class ReactorsModal(ModalScreen[None]):
    """Show who reacted to a post (per-reaction grouping)."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    def __init__(self, groups: list[tuple[str, list[str]]]) -> None:
        super().__init__()
        self._groups = groups

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static("Reactions", id="modal-title")
            if not self._groups:
                yield Static("(no reactions)")
            else:
                lines: list[str] = []
                for rid, users in self._groups:
                    emoji, label = _REACTION_META.get(rid, (f":{rid}:", rid))
                    who = ", ".join(f"@{u}" for u in users) or "(loading…)"
                    lines.append(f"[b]{emoji} {label}[/b] ({len(users)})\n  {who}")
                yield Static("\n\n".join(lines), markup=True)
            yield Static("esc · q to close", id="modal-hint")

    def action_close(self) -> None:
        self.dismiss(None)


class CopyMenuModal(ModalScreen[tuple[str, int] | None]):
    """Pick what to copy from a post. Returns ('link', 0) or ('code', i)."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, code_previews: list[str]) -> None:
        super().__init__()
        self._code_previews = code_previews

    def compose(self) -> ComposeResult:
        opts: list[Option] = [Option("🔗  Copy post link", id="link:0")]
        for i, preview in enumerate(self._code_previews):
            label = preview.strip().splitlines()[0] if preview.strip() else "(empty)"
            if len(label) > 48:
                label = label[:47] + "…"
            opts.append(Option(f"⧉  Copy code #{i + 1}: {label}", id=f"code:{i}"))
        with Vertical(id="modal-box"):
            yield Static("Copy…", id="modal-title")
            yield OptionList(*opts, id="copy-opts")
            yield Static("enter select · esc cancel", id="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#copy-opts", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        oid = event.option.id or ""
        kind, _, idx = oid.partition(":")
        try:
            i = int(idx)
        except ValueError:
            i = 0
        self.dismiss((kind, i))

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class NewTopicModal(ModalScreen[dict | None]):
    """Ask for topic title + category; returns {title, category_id} or None."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Cancel"),
    ]

    def __init__(self, categories: list[dict]) -> None:
        super().__init__()
        self._cats = [c for c in categories if c.get("id")]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static("New topic", id="modal-title")
            yield Input(placeholder="Topic title", id="title")
            yield Label("Category:")
            yield OptionList(
                *[Option(c.get("name", "?"), id=str(c["id"])) for c in self._cats],
                id="cat-list",
            )
            yield Static("tab: switch · enter: confirm · esc: cancel", id="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#title", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "title":
            self.query_one("#cat-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        title = self.query_one("#title", Input).value.strip()
        if not title:
            self.query_one("#title", Input).focus()
            return
        try:
            cid = int(event.option.id or "0")
        except ValueError:
            cid = 0
        if cid == 0:
            return
        self.dismiss({"title": title, "category_id": cid})

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class PMComposerModal(ModalScreen[dict | None]):
    """Ask for PM title + recipient usernames; returns {title, recipients}."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static("New private message", id="modal-title")
            yield Input(placeholder="Recipient usernames (comma separated)", id="to")
            yield Input(placeholder="Subject", id="title")
            yield Static("enter on subject to continue · esc cancel", id="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#to", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "to":
            self.query_one("#title", Input).focus()
            return
        recipients = [
            r.strip() for r in self.query_one("#to", Input).value.split(",") if r.strip()
        ]
        title = self.query_one("#title", Input).value.strip()
        if not recipients or not title:
            return
        self.dismiss({"title": title, "recipients": recipients})

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class SearchModal(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "dismiss_none", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static("Search", id="modal-title")
            yield Input(placeholder="Search terms…", id="q")
            yield Static("enter to search · esc cancel", id="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#q", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value or None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    """Yes/No confirmation. Returns True/False."""

    BINDINGS = [
        Binding("escape", "dismiss_false", "No"),
        Binding("n", "dismiss_false", "No"),
        Binding("y", "dismiss_true", "Yes"),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(self._prompt, id="modal-title")
            yield Static("y: yes · n/esc: no", id="modal-hint")

    def action_dismiss_true(self) -> None:
        self.dismiss(True)

    def action_dismiss_false(self) -> None:
        self.dismiss(False)


class FilePickerModal(ModalScreen[str | None]):
    """Ask for a file path to upload."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, prompt: str = "Upload file") -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(self._prompt, id="modal-title")
            yield Input(placeholder="Absolute or ~/ path to file", id="path")
            yield Static("enter to upload · esc cancel", id="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#path", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        v = event.value.strip()
        self.dismiss(v or None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class CategoryPickerModal(ModalScreen[dict | None]):
    """Pick a single category. Used when drilling in from the Categories tab."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, categories: list[dict]) -> None:
        super().__init__()
        self._cats = [c for c in categories if c.get("id")]
        self._by_id = {str(c["id"]): c for c in self._cats}

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static("Choose a category", id="modal-title")
            yield OptionList(
                *[Option(c.get("name", "?"), id=str(c["id"])) for c in self._cats],
                id="cats",
            )

    def on_mount(self) -> None:
        self.query_one("#cats", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        cat = self._by_id.get(str(event.option.id or ""))
        self.dismiss(cat)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)
