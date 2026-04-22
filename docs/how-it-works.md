# How jtech-tui works

A tour of the internals, aimed at contributors. If you just want to use the
client, read the top-level `README.md` instead.

## Stack

- **Textual** — reactive TUI framework. Every visible thing is a `Widget`;
  every interaction routes through the same message-pump pipeline.
- **requests** — HTTP. Discourse responds to a tiny slice of its JSON API
  backed by the user's `_t` session cookie.
- **Rich** — `Text` markup for inline color/bold in tables and the footer.
- **Python 3.10+** — no build step, no bundler, no JS.

## Process model

```
                 main thread (event loop)
                        │
       ┌────────────────┼────────────────┐
       ▼                ▼                ▼
   Screen stack    Message pump     Worker threads
   (LoginScreen,   (widget events,  (@work(thread=True),
    MainScreen,     key handling,    HTTP calls via
    ThreadScreen,   refreshes)       api.Client)
    modals…)
```

- The UI lives on the main thread. No blocking I/O is allowed there.
- Network calls run inside `textual.work(thread=True)` workers. When they
  finish, they marshal results back to the main thread with
  `self.app.call_from_thread(callback, …)`.
- The screen stack is how modals and sub-screens layer. `push_screen` adds
  a new top; `pop_screen` removes it; `switch_screen` swaps the top.

## Files at a glance

| File                          | Role                                                                 |
|-------------------------------|----------------------------------------------------------------------|
| `jtech_tui/app.py`            | `JtechApp` — CLI args, config load, session bootstrap, reauth flow.  |
| `jtech_tui/api.py`            | `Client` — thin Discourse wrapper with CSRF + cookie handling.       |
| `jtech_tui/config.py`         | `Config` dataclass backed by `~/.config/jtech-tui/config.json`.      |
| `jtech_tui/editor.py`         | `$EDITOR` round-trip through a temporary `.md` file.                 |
| `jtech_tui/styles.tcss`       | Textual CSS for every screen.                                        |
| `jtech_tui/screens/login.py`  | Username/password prompt → cookie.                                   |
| `jtech_tui/screens/main.py`   | Tabs for Latest / New / Top / Unseen / Categories / PMs / Notifs /Search. |
| `jtech_tui/screens/thread.py` | Thread view with reply, quote-reply, reactions, copy menu.           |
| `jtech_tui/screens/composer.py` | Modal composers: new topic, PM, file picker, search.               |
| `jtech_tui/screens/leaderboard.py` | Gamification plugin viewer.                                     |
| `jtech_tui/screens/user_profile.py` | User bio + recent activity.                                    |
| `jtech_tui/screens/smart_footer.py` | Footer that collapses overflow into "? all keys".              |

## Startup sequence

1. `jtech` CLI → `jtech_tui.app.run()` parses `--feed` and builds `JtechApp`.
2. `JtechApp.__init__` loads `Config`, creates an `api.Client` with the stored
   cookie (may be empty), and asks Textual to take over the terminal.
3. `on_mount` picks the starting screen:
   - Cookie present → `MainScreen` (feeds start loading in workers).
   - No cookie → `LoginScreen`.
4. Successful login writes the cookie to disk and `switch_screen`s to
   `MainScreen`.

## The reauth flow (and the bug it used to have)

Background: any worker that receives a `401 Unauthorized` from Discourse calls
`self.app.call_from_thread(self.app.reauth)`. Reauth's job is to:

1. Clear the in-memory and on-disk cookie so nothing keeps hitting the API
   with bad credentials.
2. Drop every screen above the current top (modals, thread view, …).
3. Show `LoginScreen` again.
4. Tell the user what just happened.

The historical implementation popped screens and called `switch_screen` inline:

```python
while len(self.screen_stack) > 1:
    self.pop_screen()
self.switch_screen(LoginScreen())
```

That raced with Textual's message pump. `MainScreen.on_mount` schedules
`call_after_refresh(self._focus_active_table)`, its `TabbedContent` and
`DataTable` children schedule their own refreshes, and every widget forwards
`InvokeLater` messages via `self.app.screen._invoke_later(…)`. If a queued
callback fires *after* we've torn MainScreen down but *before* the new
LoginScreen is fully attached, `self.app.screen` finds an empty stack and
raises:

```
ScreenStackError: No screens on stack
```

Fix (in `app.py`): keep step 1 synchronous (cookie wipe is safe to do right
away and prevents further bad requests), but defer the screen swap with
`self.call_later`. By the time the callback runs, the outgoing screen's
pending messages have drained, and the swap becomes atomic from the message
pump's perspective.

The swap itself has a second subtlety: Textual keeps an internal
`Screen(id="_default")` at the bottom of every app's stack. That screen was
never installed via `push_screen(..., callback=...)`, so its
`_result_callbacks` list is empty. `switch_screen` unconditionally calls
`_pop_result_callback()` on the outgoing top — which blows up with
`IndexError: pop from empty list` if the outgoing top is `_default`. So the
swap pops user-pushed screens down to `_default` (never including it) and
then `push_screen(LoginScreen())` on top of it:

```python
def reauth(self) -> None:
    self.cfg.session_cookie = ""
    self.cfg.save()
    self.client = Client(self.cfg.forum_url, "")
    self.call_later(self._reauth_swap)

def _reauth_swap(self) -> None:
    if self.screen_stack and isinstance(self.screen, LoginScreen):
        return
    while self.screen_stack:
        top = self.screen_stack[-1]
        if getattr(top, "id", None) == "_default":
            break
        self.pop_screen()
    self.push_screen(LoginScreen())
    self.notify("Session expired — please sign in again.", severity="warning")
```

## Data flow: loading a feed

```
user presses ← / → on tab bar
        │
        ▼
MainScreen._tab_changed  ← TabbedContent.TabActivated
        │
        ▼
MainScreen._load_for_tab(tab_id)
        │
        ▼  @work(thread=True, exclusive=True, group="feed")
MainScreen._load_feed(feed)
        │
        │   api.Client.feed_page(feed, page)  ← GET /{feed}.json
        │
        ▼  call_from_thread
MainScreen._populate_feed(feed, topics, has_more)
        │
        ▼
VimDataTable rows + cached in self._view_data so on_resize can re-render
without a second API hit.
```

`VimDataTable` is a `DataTable` subclass that adds vim bindings and posts a
`RequestMore` message when the user tries to step past the last row — that's
how infinite scroll works without a separate "load more" button.

## Data flow: reading a thread

- `MainScreen` routes row selection to `ThreadScreen(topic)`.
- `ThreadScreen.on_mount` starts a worker that calls
  `api.Client.topic(topic_id)` and then follows `post_stream.stream` to fetch
  any posts not returned in the first page.
- Posts render as a `VerticalScroll` of `Static` widgets, one per post, with
  markdown run through Rich. `[quote=…]` blocks are pre-processed into real
  blockquotes so Rich renders them natively.
- Reactions, if the `discourse-reactions` plugin is installed, come from a
  separate call; the picker lists only the ones the server accepts.

## Configuration

`Config` is a plain dataclass persisted to
`~/.config/jtech-tui/config.json` with mode `0600`. `Config.load` tolerates
missing files, malformed JSON, and unknown keys (it filters with
`dataclasses.fields`). `Config.save` atomically writes the whole document;
it's only called after login and on explicit user actions.

## Adding a new feature

1. If it needs data from Discourse, add a method to `api.Client`. Keep it
   thin — raw JSON in, raw JSON out. Translate errors into `Unauthorized`
   for 401s so the reauth flow catches them.
2. If it's a new screen, drop it in `jtech_tui/screens/` and register any
   bindings on the screen's `BINDINGS` list. `SmartFooter` reads those
   directly — no extra wiring needed.
3. If it's UI state only, prefer adding a reactive attribute on the screen
   over a global. Widgets watching reactives redraw automatically.
4. Always run network calls via `@work(thread=True)` and marshal results
   back with `call_from_thread`.

## Gotchas

- **Don't shadow `_bindings`** on a Textual widget — Textual uses that
  attribute name internally for binding resolution. `BindingsHelpModal` learned
  this the hard way and now uses `_binding_entries`.
- **`call_after_refresh` is post-layout, not post-network.** It fires once
  the current render flush finishes; it doesn't wait for workers.
- **`switch_screen` is not atomic vs. the message pump.** Use
  `call_later` around transitions that follow an external event (like the
  reauth flow) so queued messages drain first.
- **Never `switch_screen` off `Screen(id="_default")`.** Textual installs
  that screen at the bottom of the stack without a result callback, and
  `switch_screen` will raise `IndexError: pop from empty list` trying to
  invoke one. Pop user screens down to `_default` and `push_screen` on top
  of it instead.
- **Re-render on resize.** Tables cache their source data in
  `MainScreen._view_data` so column truncation can adapt when the terminal
  changes size without a re-fetch.
