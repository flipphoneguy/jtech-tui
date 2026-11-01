from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..complete import users
from ..widgets.suggest_box import Fetch, SuggestBox


class ReactionModal(ModalScreen[str | None]):
    """Pick a reaction to toggle on a post. Returns the reaction id or None.

    ``reactions`` comes from the topic's ``valid_reactions``; ``mine`` is the reaction the user already has on the post (selecting it again removes it).
    """

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, reactions: list[str], emoji, mine: str | None = None) -> None:
        super().__init__()
        self._reactions = reactions
        self._emoji = emoji
        self._mine = mine

    def compose(self) -> ComposeResult:
        opts: list[Option] = []
        for i, rid in enumerate(self._reactions):
            label = Text(f"{i + 1 if i < 9 else ' '}  ", style="dim")
            label.append_text(self._emoji.rich(rid))
            label.append(f"  {rid.replace('_', ' ')}")
            if rid == self._mine:
                label.append("   ✓ yours, select to remove", style="bold")
            opts.append(Option(label, id=rid))
        with Vertical(id="modal-box"):
            yield Static("React to post", id="modal-title")
            yield OptionList(*opts, id="react-opts")
            yield Static("enter or 1-9 toggle · esc cancel", id="modal-hint")

    def on_mount(self) -> None:
        opts = self.query_one("#react-opts", OptionList)
        opts.focus()
        if self._mine in self._reactions:
            opts.highlighted = self._reactions.index(self._mine)

    def on_key(self, event) -> None:
        if event.character and event.character.isdigit() and event.character != "0":
            i = int(event.character) - 1
            if i < len(self._reactions):
                event.stop()
                self.dismiss(self._reactions[i])

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

    def __init__(self, groups: list[tuple[str, list[str]]], emoji) -> None:
        super().__init__()
        self._groups = groups
        self._emoji = emoji

    def compose(self) -> ComposeResult:
        body = Text()
        for i, (rid, users) in enumerate(self._groups):
            if i:
                body.append("\n\n")
            body.append_text(self._emoji.rich(rid))
            body.append(f"  {rid.replace('_', ' ')} ({len(users)})\n", style="bold")
            body.append("  " + (", ".join(f"@{u}" for u in users) or "(hidden)"))
        with Vertical(id="modal-box"):
            yield Static("Reactions", id="modal-title")
            with VerticalScroll(id="help-scroll"):
                yield Static(body if self._groups else "(no reactions)")
            yield Static("esc · q to close", id="modal-hint")

    def action_close(self) -> None:
        self.dismiss(None)


class CopyMenuModal(ModalScreen[tuple[str, int] | None]):
    """Pick what to copy from a post. Returns ('link', 0), ('code', i) or ('url', i)."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, code_previews: list[str], links: list[tuple[str, str]] | None = None) -> None:
        super().__init__()
        self._code_previews = code_previews
        self._links = links or []

    def compose(self) -> ComposeResult:
        opts: list[Option] = [Option("🔗  Copy post link", id="link:0")]
        for i, preview in enumerate(self._code_previews):
            label = preview.strip().splitlines()[0] if preview.strip() else "(empty)"
            if len(label) > 48:
                label = label[:47] + "…"
            opts.append(Option(f"⧉  Copy code #{i + 1}: {label}", id=f"code:{i}"))
        for i, (text, url) in enumerate(self._links):
            label = url if len(url) <= 52 else url[:51] + "…"
            opts.append(Option(f"↗  {label}", id=f"url:{i}"))
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
            yield SuggestBox(
                users(self.app), placeholder="Recipient usernames (comma separated)", input_id="to", seps=","
            )
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


class MenuModal(ModalScreen[str | None]):
    """Pick one action. ``items`` are ``(id, label)``; an id of ``""`` is a non-selectable heading."""

    BINDINGS = [Binding("escape,q", "dismiss_none", "Cancel")]

    def __init__(self, title: str, items: list[tuple[str, str]], hint: str = "enter select · esc cancel") -> None:
        super().__init__()
        self._title = title
        self._items = items
        self._hint = hint

    def compose(self) -> ComposeResult:
        opts: list[Option] = []
        for oid, label in self._items:
            if oid:
                opts.append(Option(label, id=oid))
            else:
                opts.append(Option(Text(label, style="bold dim"), disabled=True))
        with Vertical(id="modal-box"):
            yield Static(self._title, id="modal-title")
            yield OptionList(*opts, id="menu-opts")
            yield Static(self._hint, id="modal-hint")

    def on_mount(self) -> None:
        opts = self.query_one("#menu-opts", OptionList)
        opts.focus()
        for i, (oid, _) in enumerate(self._items):
            if oid:
                opts.highlighted = i
                break

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class ChecklistModal(ModalScreen[list[str] | None]):
    """Tick several items. ``items`` are ``(id, label, checked)``. Returns the ticked ids or None."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Cancel"),
        Binding("enter", "confirm", "Confirm", priority=True),
    ]

    def __init__(
        self,
        title: str,
        items: list[tuple[str, str, bool]],
        hint: str = "space toggle · enter confirm · esc cancel",
        validate=None,
    ) -> None:
        super().__init__()
        self._title = title
        self._items = items
        self._hint = hint
        # Optional check on the ticked ids; returns an error message to keep the dialog open.
        self._validate = validate

    def compose(self) -> ComposeResult:
        from textual.widgets import SelectionList

        with Vertical(id="modal-box"):
            yield Static(self._title, id="modal-title")
            yield SelectionList[str](*[(label, oid, checked) for oid, label, checked in self._items], id="checks")
            yield Static(self._hint, id="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#checks").focus()

    def action_confirm(self) -> None:
        from textual.widgets import SelectionList

        picked = list(self.query_one("#checks", SelectionList).selected)
        error = self._validate(picked) if self._validate else None
        if error:
            self.app.notify(error, severity="warning")
            return
        self.dismiss(picked)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class PromptModal(ModalScreen[str | None]):
    """Ask for one line of text. Returns the text (possibly empty) or None on cancel."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(
        self,
        title: str,
        placeholder: str = "",
        value: str = "",
        hint: str = "",
        *,
        complete: Fetch | None = None,
        seps: str = "",
        min_chars: int = 1,
    ) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._value = value
        self._hint = hint or "enter confirm · esc cancel"
        self._complete = complete
        self._seps = seps
        self._min_chars = min_chars

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Static(self._title, id="modal-title")
            if self._complete is None:
                yield Input(value=self._value, placeholder=self._placeholder, id="prompt")
            else:
                yield SuggestBox(
                    self._complete,
                    value=self._value,
                    placeholder=self._placeholder,
                    input_id="prompt",
                    seps=self._seps,
                    min_chars=self._min_chars,
                )
            yield Static(self._hint, id="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class TextModal(ModalScreen[None]):
    """Read-only scrollable text (edit history, flags, user info)."""

    BINDINGS = [Binding("escape,q", "close", "Close")]

    def __init__(self, title: str, body) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box", classes="wide"):
            yield Static(self._title, id="modal-title")
            with VerticalScroll(id="text-scroll"):
                yield Static(self._body)
            yield Static("esc · q close", id="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#text-scroll").focus()

    def action_close(self) -> None:
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
