"""Suggestion sources for SuggestBox. Each returns a blocking fetch function."""

from __future__ import annotations

from rich.text import Text

from .widgets.suggest_box import Fetch, Suggestion


def _label(main: str, extra: str = "") -> Text:
    t = Text(main)
    if extra:
        t.append(f"  {extra}", style="dim")
    return t


def _user_items(app, term: str, at: bool) -> list[Suggestion]:
    out: list[Suggestion] = []
    for u in app.client.user_search(term.lstrip("@")):
        name = u.get("username") or ""
        if name:
            out.append(("@" + name if at else name, _label(name, u.get("name") or "")))
    return out


def users(app, *, at: bool = False) -> Fetch:
    return lambda term: _user_items(app, term, at) if term.lstrip("@") else []


def audience(app) -> Fetch:
    """Whisper audiences: @users, group names and badge:Name."""
    badges: list[dict] = []

    def fetch(term: str) -> list[Suggestion]:
        if term.lower().startswith("badge:"):
            q = term[6:].strip().lower()
            if not badges:
                badges.extend(app.client.badges())
            return [
                (f"badge:{b['name']}", _label(b["name"], "badge"))
                for b in badges
                if q in (b.get("name") or "").lower()
            ]
        bare = term.lstrip("@").lower()
        if not bare:
            return []
        groups: list[Suggestion] = []
        if not term.startswith("@"):
            groups = [
                (g["name"], _label(g["name"], "group"))
                for g in app.site.raw.get("groups") or []
                if g.get("name", "").lower().startswith(bare)
            ]
        return groups + _user_items(app, bare, at=True)

    return fetch


def tags(app) -> Fetch:
    def fetch(term: str) -> list[Suggestion]:
        q = term.lstrip("#")
        if not q:
            return []
        return [(t["name"], _label(t["name"], str(t.get("count") or ""))) for t in app.client.tag_search(q)]

    return fetch


def topics(app) -> Fetch:
    """Topic titles that fill in the topic number. Numbers, links and ``new:`` are left alone."""

    def fetch(term: str) -> list[Suggestion]:
        t = term.strip()
        if t.isdigit() or t.lower().startswith(("new:", "http")):
            return []
        return [(str(x["id"]), _label(x.get("title") or "", f"#{x['id']}")) for x in app.client.topic_search(t)]

    return fetch
