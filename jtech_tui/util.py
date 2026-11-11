from __future__ import annotations

import html
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone


def parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def time_ago(s: str | None) -> str:
    """Short relative time like ``3h``, ``2d``, ``1mo``."""
    t = parse_time(s)
    if t is None:
        return s or ""
    secs = int((datetime.now(timezone.utc) - t).total_seconds())
    if secs < 60:
        return "now"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    days = secs // 86400
    if days < 7:
        return f"{days}d"
    if days < 30:
        return f"{days // 7}w"
    if days < 365:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def local_time(s: str | None) -> str:
    t = parse_time(s)
    if t is None:
        return ""
    return t.astimezone().strftime("%b %-d %Y, %-I:%M %p")


_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "mo": 30 * 86400, "y": 365 * 86400}


def parse_duration(s: str) -> int | None:
    """``10m``, ``1h``, ``2d`` as seconds."""
    m = re.fullmatch(r"(\d+)\s*(mo|m|h|d|w|y)", s.strip().lower())
    return int(m.group(1)) * _UNITS[m.group(2)] if m else None


def parse_when(s: str) -> datetime | None:
    """``30m``, ``12h``, ``2d``, ``1w``, ``3mo``, ``1y``, ``forever`` or a date like ``2026-11-01``."""
    s = s.strip().lower()
    if not s:
        return None
    if s in ("forever", "permanent", "never"):
        return datetime(3000, 1, 1, tzinfo=timezone.utc)
    m = re.fullmatch(r"(\d+)\s*(mo|m|h|d|w|y)", s)
    if m:
        return datetime.now(timezone.utc) + timedelta(seconds=int(m.group(1)) * _UNITS[m.group(2)])
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()


# /t/slug/123/4 or /t/123/4; a slug always has a non-digit, so /t/123/4 is topic 123, post 4.
_TOPIC_PATH = re.compile(r"/t/(?:[^/]*[^\d/][^/]*/)?(\d+)(?:/(\d+))?")


def internal_target(url: str, base_url: str) -> tuple[str, object, int | None] | None:
    """Classify a forum link: ``("topic", id, post_number)``, ``("user", name, None)`` or None if external.

    The forum's own host and its parent domain (jtechforums.org for forums.jtechforums.org) count as internal.
    """
    from urllib.parse import urlparse

    base_host = urlparse(base_url).hostname or ""
    u = urlparse(url)
    host = u.hostname or ""
    if host and host != base_host and not base_host.endswith("." + host):
        return None
    path = u.path
    m = _TOPIC_PATH.match(path)
    if m:
        return "topic", int(m.group(1)), int(m.group(2)) if m.group(2) else None
    m = re.match(r"/u/([^/?#]+)", path)
    if m:
        return "user", m.group(1), None
    return None


def topic_id_from(s: str) -> int | None:
    """Topic id from ``1234`` or a pasted ``…/t/slug/1234/5`` link."""
    s = s.strip()
    if s.isdigit():
        return int(s)
    m = _TOPIC_PATH.search(s)
    return int(m.group(1)) if m else None


def topic_title(d: dict) -> str:
    """Plain topic title; Discourse's ``fancy_title`` is HTML-escaped (``&ldquo;`` etc.)."""
    return (
        d.get("title")
        or html.unescape(d.get("fancy_title") or "")
        or (d.get("data") or {}).get("topic_title")
        or ""
    )


def truncate(s: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    return s[: max(0, width - 1)] + "…"


def copy_to_clipboard(text: str) -> bool:
    for cmd in (
        ["termux-clipboard-set"],
        ["pbcopy"],
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "-ib"],
        ["clip.exe"],
    ):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True, timeout=5)
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


def open_url(url: str) -> None:
    for cmd in (["termux-open-url", url], ["xdg-open", url], ["open", url]):
        if shutil.which(cmd[0]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except Exception:  # noqa: BLE001
                continue
    import webbrowser

    webbrowser.open(url)
