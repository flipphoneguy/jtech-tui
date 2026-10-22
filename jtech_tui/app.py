from __future__ import annotations

import argparse
import sys

from textual import work
from textual.app import App
from textual.binding import Binding

from .api import ApiError, Client, RateLimited, Unauthorized
from .config import Config
from .site import Site
from .util import open_url


class JtechApp(App):
    CSS_PATH = "styles.tcss"
    TITLE = "jtech forums"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("?", "show_bindings_help", "Help", show=False),
    ]

    def __init__(self, starting_feed: str | None = None) -> None:
        super().__init__()
        self.cfg: Config = Config.load()
        if starting_feed:
            self.cfg.default_feed = starting_feed
        self.client: Client = self._make_client(self.cfg.session_cookie)
        self.site = Site(self.client.host)
        self.site.load_cache()

    def _make_client(self, cookie: str) -> Client:
        return Client(self.cfg.forum_url, cookie, on_cookie_change=self._cookie_rotated)

    def _cookie_rotated(self, cookie: str) -> None:
        # Discourse rotates _t every few minutes; keep the newest so restarts stay logged in.
        self.cfg.session_cookie = cookie
        try:
            self.cfg.save()
        except OSError:
            pass

    def action_show_bindings_help(self) -> None:
        from .screens.smart_footer import BindingsHelpModal, collect_screen_bindings

        self.push_screen(BindingsHelpModal(collect_screen_bindings(self.screen, self)))

    def open_url(self, url: str, *, new_tab: bool = True) -> None:
        open_url(url)

    def on_mount(self) -> None:
        from .screens import LoginScreen, MainScreen

        if self.cfg.session_cookie:
            self.push_screen(MainScreen())
            self.refresh_site()
        else:
            self.push_screen(LoginScreen())

    @work(thread=True, exclusive=True, group="site")
    def refresh_site(self) -> None:
        """Verify the session and refresh forum metadata in the background."""
        try:
            user = self.client.current_user()
        except Unauthorized:
            self.call_from_thread(self.reauth)
            return
        except ApiError:
            user = None
        if user is not None and not user:
            self.call_from_thread(self.reauth)
            return
        if user:
            self.site.user = user
            if not self.cfg.username:
                self.cfg.username = user.get("username", "")
                self.cfg.save()
        try:
            self.site.fetch(self.client, full=not self.site.cache_is_fresh())
        except ApiError:
            return
        self.call_from_thread(self._site_ready)

    def _site_ready(self) -> None:
        for screen in self.screen_stack:
            hook = getattr(screen, "on_site_ready", None)
            if hook:
                hook()

    def report_error(self, e: Exception, what: str = "") -> None:
        """Show an API error. Call on the main thread."""
        if isinstance(e, Unauthorized):
            self.reauth()
            return
        prefix = f"{what} failed: " if what else ""
        if isinstance(e, RateLimited):
            self.notify(f"{prefix}{e}", severity="warning")
            return
        self.notify(f"{prefix}{e}", severity="error")

    def reauth(self) -> None:
        from .screens import LoginScreen

        if getattr(self, "_reauthing", False):
            return
        self.cfg.session_cookie = ""
        if self.cfg.password and self.cfg.username:
            self._reauthing = True
            self.notify("Session expired, reconnecting…", severity="warning")
            self._silent_reauth()
            return
        self.cfg.save()
        self.client = self._make_client("")
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.switch_screen(LoginScreen())
        self.notify("Session expired, please sign in again.", severity="warning")

    @work(thread=True, exclusive=True, group="reauth")
    def _silent_reauth(self) -> None:
        client = self._make_client("")
        try:
            user = client.login(self.cfg.username, self.cfg.password)
        except Exception:  # noqa: BLE001
            self._reauthing = False
            self.cfg.save()
            self.client = client
            self.call_from_thread(self._show_login_after_failed_reauth)
            return
        self.client = client
        self.cfg.session_cookie = client.session_cookie()
        self.cfg.save()
        if user:
            self.site.user = {**self.site.user, **user}
        self._reauthing = False
        self.call_from_thread(self.notify, "Reconnected.")
        self.refresh_site()

    def _show_login_after_failed_reauth(self) -> None:
        from .screens import LoginScreen

        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.switch_screen(LoginScreen())
        self.notify("Reconnect failed, please sign in again.", severity="warning")


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
