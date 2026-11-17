"""Images through textual-image, an optional extra (``pip install 'jtech-tui[images]'``)."""

from __future__ import annotations

import io
import os
import threading
from collections import OrderedDict
from typing import Any

MODES = ("auto", "kitty", "sixel", "halfblock", "off")

_widget_cls: Any = None
# "kitty", "sixel" or "halfblock" once set up; "off" when turned off; "missing" without the extra.
kind = "off"

_cache: OrderedDict[str, bytes] = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_SIZE = 24


def setup(mode: str) -> None:
    """Pick the renderer. Call before the app starts, since detection talks to the terminal."""
    global _widget_cls, kind
    if mode == "off":
        kind = "off"
        return
    try:
        import textual_image.renderable as tir
        import textual_image.widget as tiw
    except ImportError:
        kind = "missing"
        return
    if mode == "kitty":
        _widget_cls, kind = tiw.TGPImage, "kitty"
    elif mode == "sixel":
        _widget_cls, kind = tiw.SixelImage, "sixel"
    elif mode == "halfblock" or tir.Image not in (tir.SixelImage, tir.TGPImage):
        _widget_cls, kind = tiw.HalfcellImage, "halfblock"
    else:
        _widget_cls, kind = tiw.Image, "sixel" if tir.Image is tir.SixelImage else "kitty"


def available() -> bool:
    return _widget_cls is not None


def graphics() -> bool:
    """Real pixels, not half blocks."""
    return kind in ("kitty", "sixel")


def unavailable_message() -> str:
    if kind == "off":
        return "Images are off. Set \"images\" in config.json or run with --images auto."
    cmd = "pip install 'jtech-tui[images]'"
    if "com.termux" in os.environ.get("PREFIX", ""):
        cmd = f"pkg install python-pillow, then {cmd}"
    return f"Images need an extra: {cmd}"


def widget(**kw):
    return _widget_cls(**kw)


def fetch(client, url: str):
    """Download and open an image (first frame for GIFs). Runs in a worker thread."""
    from PIL import Image

    if url.startswith("/"):
        url = client.base_url + url
    with _cache_lock:
        data = _cache.get(url)
        if data is not None:
            _cache.move_to_end(url)
    if data is None:
        r = client.session.get(url, timeout=30)
        r.raise_for_status()
        data = r.content
        with _cache_lock:
            _cache[url] = data
            while len(_cache) > _CACHE_SIZE:
                _cache.popitem(last=False)
    img = Image.open(io.BytesIO(data))
    img.load()
    return img
