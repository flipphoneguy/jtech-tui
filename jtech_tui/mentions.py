"""Finding @mentions in markdown and checking they're real users or groups."""

from __future__ import annotations

import re

# Code and quotes aren't the writer's own mentions, so they're left alone.
_SKIP = re.compile(r"```.*?```|`[^`\n]*`|\[quote[^\]]*\].*?\[/quote\]", re.S | re.I)
_MENTION = re.compile(r"(?<![\w@`/])@([\w.\-]*)")


def _spans(body: str) -> list[tuple[int, int, str]]:
    """(start, end, name) for each mention outside code and quotes. A lone ``@`` has an empty name."""
    skip = [m.span() for m in _SKIP.finditer(body)]
    out = []
    for m in _MENTION.finditer(body):
        if any(a <= m.start() < b for a, b in skip):
            continue
        name = m.group(1).rstrip(".-")
        out.append((m.start(), m.start() + 1 + len(name), name))
    return out


def unknown_mentions(client, body: str) -> list[str]:
    """Names that aren't users or groups, in order; a lone ``@`` shows up as ``""`` once per use."""
    spans = _spans(body)
    names = list(dict.fromkeys(n for _, _, n in spans if n))
    valid: set[str] = set()
    if names:
        d = client.get("/composer/mentions.json", params=[("names[]", n) for n in names])
        valid = {u.lower() for u in d.get("users") or []} | {g.lower() for g in d.get("groups") or {}}
    valid |= {"here", "all"}
    out: list[str] = []
    for _, _, n in spans:
        if n == "" or (n.lower() not in valid and n not in out):
            out.append(n)
    return out


def replace_mention(body: str, old: str, new: str, skip: int = 0) -> str:
    """Swap ``@old`` for ``@new`` everywhere outside code and quotes. For a lone ``@``, only the one after ``skip`` that were left alone."""
    hits = [(s, e) for s, e, name in _spans(body) if name == old]
    if not old:
        hits = hits[skip:skip + 1]
    # Back to front so earlier offsets stay right.
    for start, end in reversed(hits):
        body = body[:start] + "@" + new + body[end:]
    return body
