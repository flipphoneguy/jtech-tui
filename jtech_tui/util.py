from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone


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
