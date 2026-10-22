from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Input, Label, Static

from ..api import ApiError


class LoginScreen(Screen):
    BINDINGS = [
        Binding("ctrl+q", "app.quit", "Quit", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="login-box"):
            yield Static("jtech forums · sign in", id="login-title")
            yield Input(placeholder="Username", id="username")
            yield Input(placeholder="Password", id="password", password=True)
            yield Input(placeholder="2FA code", id="otp", restrict=r"[0-9]*", max_length=8)
            yield Checkbox("Remember me", id="remember-me")
            yield Button("Log in", variant="primary", id="submit")
            yield Label("", id="login-error")

    def on_mount(self) -> None:
        cfg = self.app.cfg
        self.query_one("#otp", Input).display = False
        if cfg.username:
            self.query_one("#username", Input).value = cfg.username
        if cfg.password:
            self.query_one("#password", Input).value = cfg.password
            self.query_one("#remember-me", Checkbox).value = True
        self.query_one("#password" if cfg.username else "#username", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "username":
            self.query_one("#password", Input).focus()
        else:
            self._try_login()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self._try_login()

    def _try_login(self) -> None:
        if self._busy:
            return
        username = self.query_one("#username", Input).value.strip()
        password = self.query_one("#password", Input).value
        otp = self.query_one("#otp", Input).value.strip()
        if not username or not password:
            self._set_error("enter username and password")
            return
        self._busy = True
        self._set_error("signing in…")
        self._do_login(username, password, otp)

    def _set_error(self, msg: str) -> None:
        self.query_one("#login-error", Label).update(msg)

    @work(thread=True, exclusive=True)
    def _do_login(self, username: str, password: str, otp: str) -> None:
        client = self.app._make_client("")
        try:
            user = client.login(username, password, otp)
        except ApiError as e:
            self.app.call_from_thread(self._on_login_failed, str(e), e.error_type)
            return
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self._on_login_failed, str(e), "")
            return
        self.app.call_from_thread(self._on_login_ok, client, user, username, password)

    def _on_login_failed(self, err: str, error_type: str) -> None:
        self._busy = False
        self._set_error(err)
        if error_type == "second_factor_required":
            otp = self.query_one("#otp", Input)
            otp.display = True
            otp.value = ""
            otp.focus()
            return
        pwd = self.query_one("#password", Input)
        pwd.value = ""
        pwd.focus()

    def _on_login_ok(self, client, user: dict, username: str, password: str) -> None:
        from .main import MainScreen

        app = self.app
        remember = self.query_one("#remember-me", Checkbox).value
        app.client = client
        app.cfg.session_cookie = client.session_cookie()
        app.cfg.username = user.get("username") or username
        app.cfg.password = password if remember else ""
        app.cfg.save()
        app.switch_screen(MainScreen())
        app.refresh_site()
