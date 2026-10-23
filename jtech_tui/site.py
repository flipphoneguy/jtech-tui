from __future__ import annotations

import json
import os
import time
import unicodedata
from pathlib import Path
from typing import Any

from rich._emoji_codes import EMOJI as _RICH_EMOJI
from rich.text import Text

_SKIN_TONES = {f"t{i}": chr(0x1F3FB + i - 2) for i in range(2, 7)}

# Old shortnames people still type that aren't in the forum's emoji list.
_LEGACY = {
    "slight_smile": "🙂",
    "slightly_smiling_face": "🙂",
    "slight_frown": "🙁",
    "rofl": "🤣",
    "upside_down": "🙃",
    "thinking": "🤔",
    "hugs": "🤗",
    "nerd": "🤓",
    "face_with_rolling_eyes": "🙄",
    "rolling_eyes": "🙄",
    "ballot_box": "🗳️",
}

# jtech's custom emoji drawn as colored text, close to the real images.
_CUSTOM_BADGES: dict[str, list[tuple[str, str]]] = {
    "meh": [("M", "bold #1a1a1a on #f4a6a6"), ("E", "bold #1a1a1a on #a8e0a0"), ("H", "bold #1a1a1a on #a9c4f2")],
    "jtechforums": [("JF", "bold #ffffff on #0d2b5e")],
    "ai_post": [("🤖", "")],
}

# Reaction list used before the first topic tells us the real one.
DEFAULT_REACTIONS = ["+1", "heart"]


def cache_dir(host: str) -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    p = Path(base) / "jtech-tui" / host
    p.mkdir(parents=True, exist_ok=True)
    return p


class Emoji:
    """Resolve Discourse emoji names (``fire``, ``+1:t3``) to characters."""

    def __init__(self) -> None:
        self.aliases: dict[str, list[str]] = {}
        self.custom: dict[str, str] = {}
        self._memo: dict[str, str | None] = {}

    def load(self, emojis: dict[str, list[dict]] | None, aliases: dict[str, list[str]] | None) -> None:
        if aliases:
            self.aliases = aliases
        if emojis:
            self.custom = {e["name"]: e.get("url", "") for e in emojis.get("default") or [] if e.get("name")}
        self._memo.clear()

    def _resolve(self, name: str) -> str | None:
        if name in _RICH_EMOJI:
            return _RICH_EMOJI[name]
        if name in _LEGACY:
            return _LEGACY[name]
        # Aliases first: a bare Unicode name lookup can hit a plain symbol (``ballot_box`` is ☐ in Unicode but 🗳 on Discourse).
        aliases = self.aliases.get(name, [])
        for a in aliases:
            k = a.replace(" ", "_")
            if k in _RICH_EMOJI:
                return _RICH_EMOJI[k]
        for cand in [name.replace("_", " "), *aliases]:
            try:
                ch = unicodedata.lookup(cand)
            except KeyError:
                continue
            if ord(ch[0]) >= 0x1F000 or unicodedata.category(ch[0]) == "So":
                return ch
        return None

    def char(self, name: str) -> str | None:
        """Unicode for a name, or None for custom/unknown emoji."""
        name = name.strip(":")
        if name in self._memo:
            return self._memo[name]
        base, _, tone = name.partition(":")
        ch = self._resolve(base)
        if ch and tone in _SKIN_TONES:
            ch += _SKIN_TONES[tone]
        self._memo[name] = ch
        return ch

    def text(self, name: str) -> str:
        """Best plain string: the character, or ``:name:`` for custom ones."""
        return self.char(name) or f":{name.strip(':')}:"

    def rich(self, name: str) -> Text:
        """Styled version for display; custom jtech emoji become colored badges."""
        bare = name.strip(":")
        badge = _CUSTOM_BADGES.get(bare)
        if badge:
            return Text.assemble(*badge)
        ch = self.char(bare)
        if ch:
            return Text(ch)
        return Text(f":{bare}:", style="dim")


class Site:
    """Everything the UI needs to know about the forum and the logged-in user.

    Loaded once at startup (from disk cache first, then refreshed). Replaces the lists the old client hardcoded: notification types, reactions, flag types.
    """

    CACHE_TTL = 24 * 3600

    def __init__(self, host: str) -> None:
        self.host = host
        self.emoji = Emoji()
        self.raw: dict[str, Any] = {}
        self.user: dict[str, Any] = {}
        self.categories: dict[int, dict] = {}
        self.notification_types: dict[int, str] = {}
        self.post_types: dict[str, int] = {"regular": 1, "moderator_action": 2, "small_action": 3, "whisper": 4}
        self.flag_types: list[dict] = []
        self.reactions: list[str] = list(DEFAULT_REACTIONS)
        self.main_reaction = "+1"
        self.loaded = False

    # --- user / permissions ---

    @property
    def username(self) -> str:
        return self.user.get("username") or ""

    @property
    def is_staff(self) -> bool:
        return bool(self.user.get("staff"))

    @property
    def is_mod(self) -> bool:
        return bool(self.user.get("moderator") or self.user.get("admin"))

    @property
    def is_admin(self) -> bool:
        return bool(self.user.get("admin"))

    @property
    def can_review(self) -> bool:
        return bool(self.user.get("can_review"))

    @property
    def can_whisper(self) -> bool:
        return bool(self.user.get("whisperer"))

    def in_group(self, name: str) -> bool:
        return any(g.get("name") == name for g in self.user.get("groups") or [])

    # --- lookups ---

    def notification_name(self, type_id: int | None) -> str:
        if type_id is None:
            return "?"
        return self.notification_types.get(type_id, f"type {type_id}").replace("_", " ")

    def category(self, cid: int | None) -> dict:
        return self.categories.get(cid or 0) or {}

    def category_label(self, cid: int | None) -> str:
        c = self.category(cid)
        if not c:
            return ""
        parent = self.category(c.get("parent_category_id"))
        return f"{parent['name']} › {c['name']}" if parent else c.get("name", "")

    def set_reactions(self, valid: list[str] | None) -> None:
        if valid:
            self.reactions = list(valid)

    # --- loading ---

    def _cache_file(self) -> Path:
        return cache_dir(self.host) / "site.json"

    def load_cache(self) -> bool:
        try:
            data = json.loads(self._cache_file().read_text())
        except (OSError, ValueError):
            return False
        self._apply(data)
        return True

    def cache_is_fresh(self) -> bool:
        try:
            return time.time() - self._cache_file().stat().st_mtime < self.CACHE_TTL
        except OSError:
            return False

    def save_cache(self, data: dict) -> None:
        try:
            self._cache_file().write_text(json.dumps(data))
        except OSError:
            pass

    def fetch(self, client, *, full: bool = True) -> None:
        """Blocking refresh. ``full=False`` skips the large, rarely changing emoji lists."""
        cats = client.categories()
        # Lazy-loaded sites leave some subcategories out of /categories.json; fetch them by id.
        have = {c["id"] for c in cats}
        missing = sorted({i for c in cats for i in c.get("subcategory_ids") or []} - have)
        if missing:
            found = client.get("/categories/find.json", params=[("ids[]", i) for i in missing])
            cats += found.get("categories") or []
        data: dict[str, Any] = {"site": client.get("/site.json"), "categories": cats}
        if full:
            data["emojis"] = client.emojis()
            data["aliases"] = client.emoji_aliases()
        else:
            try:
                old = json.loads(self._cache_file().read_text())
                data["emojis"] = old.get("emojis")
                data["aliases"] = old.get("aliases")
            except (OSError, ValueError):
                pass
        if self.reactions != DEFAULT_REACTIONS:
            data["reactions"] = self.reactions
        if self.user:
            data["user"] = self.user
        self._apply(data)
        self.save_cache(data)

    def _apply(self, data: dict) -> None:
        site = data.get("site") or {}
        self.raw = site
        self.notification_types = {v: k for k, v in (site.get("notification_types") or {}).items()}
        self.post_types = site.get("post_types") or self.post_types
        self.flag_types = [
            t for t in site.get("post_action_types") or [] if t.get("is_flag") or t.get("name_key") != "like"
        ]
        cats: dict[int, dict] = {}
        for c in site.get("categories") or []:
            cats[c["id"]] = c
        for c in data.get("categories") or []:
            cats[c["id"]] = c
            for sub in c.get("subcategory_list") or []:
                sub.setdefault("parent_category_id", c["id"])
                cats[sub["id"]] = sub
        if cats:
            self.categories = cats
        self.emoji.load(data.get("emojis"), data.get("aliases"))
        if data.get("reactions"):
            self.reactions = list(data["reactions"])
        if data.get("user") and not self.user:
            self.user = data["user"]
        self.loaded = True
