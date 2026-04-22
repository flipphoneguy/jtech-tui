from __future__ import annotations

import argparse
import sys

from textual.app import App
from textual.binding import Binding

from .api import Client
from .config import Config
from .screens import LoginScreen, MainScreen


class JtechApp(App):
    CSS_PATH = "styles.tcss"
    TITLE = "jtech forums"
    SUB_TITLE = "discourse · python · textual"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("?", "show_bindings_help", "Help"),
    ]

    def action_show_bindings_help(self) -> None:
        from .screens.smart_footer import BindingsHelpModal, collect_screen_bindings
        self.push_screen(BindingsHelpModal(collect_screen_bindings(self.screen, self)))

    def __init__(self, starting_feed: str | None = None) -> None:
        super().__init__()
        self.cfg: Config = Config.load()
        if starting_feed:
            self.cfg.default_feed = starting_feed
        self.client: Client = Client(
            self.cfg.forum_url,
            session_cookie=self.cfg.session_cookie,
            cookies=self.cfg.cookies,
        )

    def _has_session(self) -> bool:
        return bool(self.cfg.cookies) or bool(self.cfg.session_cookie)

    def on_mount(self) -> None:
        if self._has_session():
            self.push_screen(MainScreen())
        else:
            self.push_screen(LoginScreen())

    def save_session(self) -> None:
        """Persist the current cookie jar so the session survives restarts."""
        self.cfg.cookies = self.client.dump_cookies()
        # Keep the legacy `_t` scalar in sync so older code paths still work.
        self.cfg.session_cookie = self.client.session_cookie()
        self.cfg.save()

    def reauth(self) -> None:
        # Called from worker threads via call_from_thread. We clear the cookie
        # synchronously so no further requests use it, but defer the screen
        # swap one tick so any InvokeLater messages queued by the outgoing
        # screen's widgets drain first. Without this, Textual's message pump
        # forwards those callbacks to self.app.screen mid-transition and
        # raises ScreenStackError("No screens on stack").
        self.cfg.session_cookie = ""
        self.cfg.cookies = []
        self.cfg.save()
        self.client = Client(self.cfg.forum_url, "")
        self.call_later(self._reauth_swap)

    def _reauth_swap(self) -> None:
        # Already on LoginScreen — nothing to do.
        if self.screen_stack and isinstance(self.screen, LoginScreen):
            return
        # Pop user-pushed screens. Textual keeps an internal `_default`
        # screen at the bottom of the stack; never switch_screen off it —
        # it has no result callback and switch_screen will
        # `IndexError: pop from empty list` trying to invoke one.
        while self.screen_stack:
            top = self.screen_stack[-1]
            if getattr(top, "id", None) == "_default":
                break
            self.pop_screen()
        self.push_screen(LoginScreen())
        self.notify("Session expired — please sign in again.", severity="warning")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="jtech", description="jtech forums TUI")
    ap.add_argument(
        "--feed",
        choices=["latest", "new", "top", "unseen", "categories", "messages", "notifications"],
        help="starting tab",
    )
    return ap.parse_args(argv)


def run() -> None:
    args = _parse_args(sys.argv[1:])
    JtechApp(starting_feed=args.feed).run()


if __name__ == "__main__":
    run()
