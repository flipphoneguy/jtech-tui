# jtech-tui

A terminal client for [jtech forums](https://forums.jtechforums.org), built with [Textual](https://textual.textualize.io/). It speaks the regular Discourse API, so most of it works on other Discourse sites too, but it's made for jtech and knows about jtech's own plugin (whispers, REQ-PM, mod tools).

Read and reply to threads, react, search, follow people, swap contact cards with REQ-PM, and, if you're on staff, moderate: all from a shell, with updates arriving live.

> **Note on provenance.** A large portion of this project was written by an AI coding assistant working against the public Discourse API. Expect rough edges and bugs. Please file issues with reproduction steps and I'll fix what I can.

## Highlights

- **Live updates.** The app listens on the same MessageBus channel the website uses. New replies show up in the thread you're reading within a second or two, the notification count updates by itself, and Latest/Unseen show how many new topics arrived. A small toast pops up for a few seconds when a notification comes in.
- **A thread view that stays put.** Threads open on your first unread post and stay there while older posts load above. Posts are drawn line by line, so long threads stay fast.
- **Posts look like posts.** Quotes show who wrote them, link previews show the site and title, code is highlighted, images show their name and size, spoilers stay hidden until you reveal them, and dates appear in your own timezone.
- **Polls and solutions.** Vote in polls and see the results, mark a reply as the solution, and jump straight to a topic's solution.
- **Everything the server allows, nothing it doesn't.** Reactions, flag types, notification types and categories come from the forum itself. Edit, delete and every mod action are offered only when the server says you can do them.
- **jtech's custom emoji.** `meh` and `jtechforums` render as small colored badges instead of `:meh:`.
- **Moderation, for staff.** A Staff tab (review queue, staff notes, moderators inbox, staff log), post and topic menus, jtech whispers, and a user moderation screen. None of it appears for regular accounts.
- **Profiles and REQ-PM.** Profiles with activity, top topics, badges and followers; follow people; set your status; request and share contact details.
- **Compose in `$EDITOR`.** Topics, replies, quotes, whispers and edits all open in your editor as `.md`. If posting fails, your text is kept and comes back the next time you reply.

## Requirements

- Python 3.10+
- A jtech (or other Discourse) account with a username and password
- For copying: `termux-clipboard-set`, `pbcopy`, `wl-copy`, `xclip`, `xsel` or `clip.exe`
- `$EDITOR` (or `$VISUAL`) for writing; falls back to `nano`

## Install

```sh
git clone https://github.com/flipphoneguy/jtech-tui
cd jtech-tui
pip install -e .
```

## Run

```sh
jtech                   # opens your default tab
jtech --feed unseen     # start on a specific tab
```

`--feed` takes `latest`, `unseen`, `categories`, `messages` or `notifications`.

The first launch asks for your username and password, plus a 2FA code if your account has one. Tick "Remember me" to let the app sign in again by itself when the session runs out.

## Main screen

Tabs: **Latest**, **Unseen** (new topics plus topics with unread replies), **Categories** (as a tree), **Messages**, **Notifications**, **Staff** (staff only) and **Search**.

Each topic shows ✓ solved, 📌 pinned, 🔒 closed and 🔥 hot markers, with a second line for category, tags, last poster and likes.

| Key | Action |
|---|---|
| `tab` / `shift+tab`, `←` / `→` on the tab bar | Switch tabs |
| `↓` from the tab bar, `↑` at the top of a list | Move between the tab bar and the list |
| `j` / `k`, `g` / `G` | Down / up, top / bottom |
| `n` / `p` | Next / previous unread topic |
| `enter` | Open |
| `f` | Filter Latest or Unseen by category or tag; on Search, open the filter menu |
| `w` | Watch, track, normal or mute the highlighted topic |
| `D` | Dismiss everything on Unseen |
| `/` | Search |
| `N` / `M` | New topic / new private message |
| `P` | Your profile |
| `L` | Leaderboard |
| `U` | Upload a file (its markdown goes to the clipboard) |
| `R` | Reload |
| `?` | All keys for the current screen |
| `ctrl+q` | Quit |

Scrolling to the bottom of a list loads the next page.

**Search.** Type a query and press enter. `ctrl+f` (or `f` from the results) opens a filter menu for category, user, tag, date range, titles only, solved or unsolved, and sort order. It writes the site's search syntax into the box (`#phones @dov tags:android after:2026-01-01 order:latest`), so you can also type it directly. Opening a result lands on the matching post. In the search box, `↑` goes to the tab bar and `↓` to the results.

## Thread view

| Key | Action |
|---|---|
| `j` / `k` | Next / previous post (scrolls within long posts first) |
| `space` / `b` | Page down / up |
| `g` / `G` | First / last post of the topic |
| `#` | Jump to a post number |
| `/` | Find in this topic |
| `p` | Go to the post this one replies to |
| `r` / `t` | Reply to the post / to the topic |
| `Q` | Quote reply |
| `enter` or `+` | React (numbers 1–9 pick in the menu; choosing your own reaction removes it) |
| `V` | Vote in the post's poll; vote again to change it or remove it |
| `A` | Mark or unmark the post as the solution (when you're allowed) |
| `J` | Go to the solution |
| `l` | Who reacted |
| `L` | Links in the post (forum links open in the app, others in the browser) |
| `y` | Copy the post's markdown |
| `Y` | Copy menu: post link, any code block, or any link |
| `o` | Open the post in the browser |
| `u` | Author's profile |
| `x` | Collapse or expand a long post |
| `S` | Reveal spoilers and expand hidden sections |
| `E` / `D` | Edit / delete (when allowed; editing someone else's post asks for a reason) |
| `!` | Flag |
| `a` | Turn live updates for this thread on or off |
| `e` | Open the loaded posts in `$EDITOR`, read-only |
| `U` | Upload a file |
| `R` | Reload |
| `esc` / `q` | Back |

The header shows the category, tags, state (closed, unlisted, and for solved topics which post is the solution) and where you are, like `166/426`. Keys that don't apply to the selected post, like `V` on a post without a poll, are left out of the footer.

**Polls** show their results as bars. Single-choice polls vote from a menu; multiple-choice polls use a checklist with your current picks ticked (untick everything to remove your vote). Ranked-choice polls open in the browser.

## People

Open a profile with `u` on a post, `P` for your own, or from any list of users.

| Key | Action |
|---|---|
| `←` / `→` | Activity, Topics, Replies, Badges, Followers, Following, Bio |
| `enter` | Open the post, topic, badge or user |
| `f` | Follow / unfollow |
| `c` | REQ-PM with this user (on your own profile: your REQ-PM inbox) |
| `s` | Set or clear your status (your own profile) |
| `m` | Moderate this user (staff) |
| `o` | Open in the browser |

**REQ-PM** (jtech's contact exchange). On someone's page: `r` asks for their details (you can tick which kinds), `s` sends yours (you pick which), `x` cancels your request, `d` declines theirs. Your inbox has Requests, Contacts, Shared with, My requests and My card; `enter` on a contact copies it.

## For staff

Everything here appears only when you're signed in as staff, and each item only when the server allows it.

**Staff tab** (`←` / `→` switch sections): **Review** (the review queue), **Notes** (jtech's staff notes and events feed), **Mod inbox** (the moderators group messages) and **Log** (staff actions). The tab badge counts pending reviews plus unread notes, live.

In a review item, `enter` shows the available actions. Ones that need a form on the website (suspend, silence, delete user) open the website instead.

**In a thread**

| Key | Action |
|---|---|
| `m` | Post menu: edit with reason, delete or recover, turn into a whisper or back, change the whisper's audience, add someone to it, see who's read it, pin to the bottom of the topic, wiki, change owner, move to another topic, edit history, flags, user tools |
| `M` | Topic menu: close, pin (in category or globally, until a date), unlist, archive, title, category, tags, slow mode, timers, footer message, merge, delete or recover |
| `w` | Whisper reply. Asks who it's for first (`@user, moderators, badge:Name`; empty means staff only). Whispers show a 🔒 label and their audience. |

**User moderation** (`m` on a profile, or from the post menu): state, penalty history, flags and staff notes, with `n` add note, `s` silence, `S` suspend, `L` lift a penalty.

## Leaderboard

`←` / `→` change the period (all time, yearly, quarterly, monthly, weekly, daily). `enter` opens a profile.

## Configuration

`~/.config/jtech-tui/config.json` (mode `0600`):

| Field | Default | Purpose |
|---|---|---|
| `forum_url` | `https://forums.jtechforums.org` | The Discourse site |
| `default_feed` | `latest` | Tab to open when `--feed` isn't given |
| `session_cookie` | | Your session; kept up to date as the forum rotates it |
| `username` | | Your username |
| `password` | | Only saved with "Remember me", in plain text |

Forum data that rarely changes (categories, emoji, notification types) is cached in `~/.cache/jtech-tui/<host>/` and refreshed in the background.

To use another Discourse site, change `forum_url` and clear `session_cookie`.

## Layout

```
jtech_tui/
├── api/              Discourse client: requests, errors, endpoints (topics, posts, users, mod, jtech plugin)
├── site.py           What the forum and your account allow; emoji names; disk cache
├── live.py           MessageBus long polling
├── render/           Post HTML to terminal text
├── widgets/          Thread view and list widgets
├── screens/          Main tabs, thread, profile, REQ-PM, staff, review, search, login, dialogs
├── editor.py         $EDITOR round trip
└── styles.tcss       Styling
```

## Known limitations

- Images aren't drawn; they appear as a line with their name and size, and open in the browser.
- Remember-me stores the password in plain text (the file is `0600`).
- Following, REQ-PM requests and shares, flagging and review actions notify other people, so they haven't been exercised much on the live forum yet.

## Contributing

Issues and pull requests are welcome. For bugs, please include the forum URL (if public), `python --version`, `pip show textual`, the traceback if there is one, and steps to reproduce.

## License

GPL-3. See `LICENSE`.
