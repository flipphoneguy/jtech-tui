# jtech-tui

A terminal client for [jtech forums](https://forums.jtechforums.org) — and any
other Discourse-powered site (but heavily customized for jtech) — built with
[Textual](https://textual.textualize.io/).
Browse feeds, read threads, reply, react, search, check notifications, send
private messages, follow the gamification leaderboard, and much more — all
without leaving your shell.

> **Note on provenance.** A large portion of this project was written by an AI
> coding assistant working against the public Discourse API. Expect rough edges
> and bugs. Please file issues with reproduction steps and I'll fix what I can.

## Highlights

- **Full feed coverage** — Latest, New, Top, Unseen, Categories, Messages,
  Notifications and Search, each as its own tab.
- **Notification badge** — unread notification count appears as a `● N` pip on
  the Notifications tab label.
- **Jump between unread** — `n` / `p` on a feed walks forward/backward through
  topics with a `new` or `unread` state, in the list's display order.
- **Background prefetch on hover** — moving the cursor over a feed row starts a
  300 ms debounced thread fetch in the background so `enter` is instant. The
  cache caps at 10 threads and never blocks the foreground feed.
- **Proper thread view** — markdown rendering with syntax-highlighted code
  blocks, `[quote=…]` blocks converted to real blockquotes, per-post reaction
  summaries, a `#post_number` pill, and a `↱ replying to @user #N` breadcrumb.
- **Resume-at-last-read** — a topic you've opened before lands you where you
  stopped (first unread if there is one, otherwise the last post you saw),
  matching the web UI. Threads you've never opened start at the top.
- **Open at the right post from notifications** — tapping a notification opens
  the thread centered on the linked reply, not the first post.
- **Lazy-loaded posts** — long threads only render what you're reading; more
  posts stream in above/below as you scroll. Posts above the landing position
  start loading in the background the moment the thread opens, so scrolling
  back is seamless. Scroll position stays pinned on the post you're reading
  while new content mounts.
- **Auto-collapse for long posts** — posts over 40 lines fold into a short
  preview; `x` toggles.
- **Compose in `$EDITOR` as `.md`** — new topics, replies, quote-replies and
  PMs all drop you into your editor with proper markdown syntax highlighting.
- **Reactions** — picker is hardcoded to the five reactions jtech forums
  accepts (`ok_hand`, `man_shrugging`, `+1`, `folded_hands`, `laughing`).
  Counts update in place with no reload. `l` opens a per-reaction panel
  showing who reacted with what.
- **Leaderboard** — the gamification-plugin boards at `/leaderboard/<id>`.
  Period switches with `[` / `]` (daily, weekly, monthly, quarterly, yearly,
  all); cycle between boards with `<` / `>`.
- **Copy menu** — `Y` on a post picks between the post permalink and any code
  blocks in the post; no code blocks → link copied directly.
- **Open in browser** — `o` on a post hands off to `termux-open-url` /
  `xdg-open` / `open` / `webbrowser`, opening on the exact post.
- **Live refresh** — threads auto-poll every 30 s by default (`a` toggles).
  The poll targets the tail of the loaded range so a dead thread won't spam
  old posts into the view. Feeds refresh automatically when you back out of a
  thread.
- **Upload attachments** — `U` uploads a file via `/uploads.json` and copies
  the resulting markdown to your clipboard.
- **Remember me** — optional at login. Stores the password in plaintext in
  the config so the client can silently re-authenticate when the session
  cookie expires.
- **Silent reauth** — when a session expires mid-session, the client
  refreshes the cookie in the background (if remember-me is on) without
  kicking you to the login screen.
- **Vim-style navigation** — `j` / `k` / `g` / `G` everywhere. `↑` at the top
  of a list hands focus to the tab bar without teleporting.

## Requirements

- Python 3.10+
- A Discourse forum with a username + password login (the client stores the
  `_t` session cookie)
- A clipboard helper on `PATH` for yank / copy-link / copy-code:
  `termux-clipboard-set`, `pbcopy`, `wl-copy`, `xclip`, or `xsel`
- `$EDITOR` (or `$VISUAL`) set for compose — falls back to `nano`

## Install

```sh
git clone https://github.com/flipphoneguy/jtech-tui
cd jtech-tui
pip install -e .
```

## Run

```sh
jtech                 # opens your default feed
jtech --feed latest   # override the starting tab
```

Valid `--feed` values: `latest`, `new`, `top`, `unseen`, `categories`,
`messages`, `notifications`.

On first launch you'll be prompted for your forum URL, username and password.
Tick "Remember me" if you want the client to re-login automatically after a
cookie expiry. The session cookie (and optionally the password) is written to
`~/.config/jtech-tui/config.json` with mode `0600`. Subsequent launches skip
the login screen.

## Configuration

`~/.config/jtech-tui/config.json`:

| Field            | Default                              | Purpose                                         |
|------------------|--------------------------------------|-------------------------------------------------|
| `forum_url`      | `https://forums.jtechforums.org`     | Base URL of the Discourse site.                 |
| `default_feed`   | `latest`                             | Tab to open when `--feed` is not passed.        |
| `session_cookie` | *(empty)*                            | Discourse `_t` cookie. Cleared on reauth.       |
| `username`       | *(empty)*                            | Cached for ownership checks on edit/delete.    |
| `password`       | *(empty)*                            | Plaintext; only written if "Remember me" is on. |

Point the client at a different Discourse host by editing `forum_url` and
clearing `session_cookie` so you get a fresh login prompt.

## Keys

### Main screen

| Key                            | Action                                    |
|--------------------------------|-------------------------------------------|
| `tab` / `shift+tab`, `←` / `→` | Switch tabs                               |
| `↓` on a tab                   | Focus the current list                    |
| `↑` at the top of a list       | Back to the tab bar                       |
| `j` / `k`                      | Move cursor down / up                     |
| `g` / `G`                      | Jump to top / bottom of the list          |
| `n` / `p`                      | Next / previous unread topic in this feed |
| `enter`                        | Open the selected row                     |
| `/`                            | Open the search modal                     |
| `N`                            | New topic                                 |
| `M`                            | New private message                       |
| `U`                            | Upload a file                             |
| `L`                            | Open the leaderboard                      |
| `R`                            | Reload the current tab                    |
| `ctrl+q`                       | Quit                                      |
| `?`                            | Show the full key list for this screen    |

Reaching the bottom of a feed triggers a page fetch, so you get infinite-scroll
without a separate action. Moving the cursor over a row kicks off a background
prefetch of that thread so the next `enter` is instant.

### Thread view

| Key              | Action                                                       |
|------------------|--------------------------------------------------------------|
| `j` / `k`        | Next / previous post (scrolls within the post first if long) |
| `g` / `G`        | Jump to the first / last post in the full topic              |
| `enter`          | Open the reaction picker on the highlighted post             |
| `r`              | Reply (threaded under the highlighted post if any)           |
| `t`              | Reply to the topic (never threaded)                          |
| `Q`              | Quote-reply — opens `$EDITOR` with a `[quote=…]` prefilled   |
| `y`              | Copy the highlighted post's raw markdown                     |
| `Y`              | Copy menu — pick between post link and any code blocks       |
| `o`              | Open the highlighted post in your browser                    |
| `p`              | Jump to the post this one is replying to                     |
| `l`              | Show who reacted to the highlighted post (per reaction)      |
| `x`              | Toggle collapse on the highlighted post                      |
| `u`              | Open the author's profile                                    |
| `+` / `ctrl+r`   | React to the highlighted post                                |
| `E`              | Edit your own post (opens `$EDITOR`)                         |
| `D`              | Delete your own post (with confirmation)                     |
| `a`              | Toggle auto-refresh (on by default, polls every 30 s)        |
| `e`              | Open the full thread in `$EDITOR` as read-only `.md`         |
| `U`              | Upload a file and get its markdown on the clipboard          |
| `R`              | Hard-reload the thread                                       |
| `esc` / `q`      | Back to the previous screen                                  |

### Leaderboard

| Key         | Action                                   |
|-------------|------------------------------------------|
| `[` / `]`   | Previous / next period                   |
| `<` / `>`   | Previous / next leaderboard (when > 1)   |
| `enter`     | Open the selected user's profile         |
| `R`         | Reload                                   |
| `esc` / `q` | Back                                     |

## Architecture

```
jtech_tui/
├── api.py            # Thin Discourse client (requests.Session, CSRF, JSON)
├── app.py            # Textual App + argparse entrypoint + silent reauth
├── config.py         # ~/.config/jtech-tui/config.json (dataclass)
├── editor.py         # $EDITOR round-trip with a temporary .md file
├── styles.tcss       # Textual CSS
└── screens/
    ├── login.py         # Login modal (with Remember me)
    ├── main.py          # Tabbed top-level (feeds, categories, PMs, …)
    ├── thread.py        # Thread view, lazy posts, reactions, composer
    ├── composer.py      # Shared modal dialogs + hardcoded reaction list
    ├── leaderboard.py   # Gamification plugin viewer
    ├── user_profile.py  # User bio + recent activity
    └── smart_footer.py  # Footer that truncates to "? all keys"
```

All network calls run in `textual.work` threads so the UI never blocks; results
are marshalled back with `call_from_thread`.

## Known limitations

- **Gamification plugin required** for the leaderboard feature. Without it the
  screen shows "Load failed".
- **discourse-reactions plugin required** for reactions. Posts on sites
  without it will return an error when you try to react.
- **Images** render as placeholder markdown, not actual pixels — terminals
  generally can't display images without extra setup, so this is a deliberate
  omission.
- **Drafts** are not persisted: quitting `$EDITOR` without writing discards
  the buffer.
- **Remember me is plaintext.** The password is stored unencrypted in
  `config.json`. The file is `0600` but anyone with read access to your home
  dir can read it — enable this only on trusted machines.
- **Infinite scroll** is per-tab and resets on resume (so read state stays
  accurate after you come back from a thread).

## Contributing

Issues and pull requests welcome. If you're filing a bug, please include:

- The forum URL (if it's a public site)
- Output of `python --version` and `pip show textual`
- The traceback, if any, and the steps to reproduce

Run the app from source with `pip install -e .` then `jtech`. There are no
tests yet; adding some is a welcome contribution.

## License

GPL-3 See `LICENSE`
