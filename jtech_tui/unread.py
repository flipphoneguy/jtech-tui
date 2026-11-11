"""Real unread counts for topic lists.

The site counts unread as highest post number minus last read, so posts split off into another topic or deleted still count. For topics with holes in their numbering we ask which posts are really there, in the background, under a request budget, and keep the answers on disk.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from typing import Callable

from .api import ApiError, Client, RateLimited
from .site import cache_dir

WORKERS = 4
# Leaves the rest of the site's ~50 per 10s for everything else the app does.
BUDGET = 30
WINDOW = 10.0
MAX_ENTRIES = 3000


def _key(topic: dict) -> list[int]:
    return [topic["last_read_post_number"], topic.get("highest_post_number") or 0, topic.get("posts_count") or 0]


def may_be_off(topic: dict) -> bool:
    """Read before, has unread, and the post numbers have holes somewhere."""
    last = topic.get("last_read_post_number")
    high = topic.get("highest_post_number") or 0
    if not (isinstance(last, int) and 0 < last < high):
        return False
    return high > (topic.get("posts_count") or 0)


class UnreadFixer:
    def __init__(self, host: str, client: Callable[[], Client]) -> None:
        self._file = cache_dir(host) / "unread.json"
        self._client = client
        self._lock = threading.Lock()
        # topic id -> [last read, highest, posts count, real unread]
        self._cache: dict[str, list[int]] = {}
        try:
            data = json.loads(self._file.read_text())
            if isinstance(data, dict):
                self._cache = data
        except (OSError, ValueError):
            pass
        self._dirty = False
        self._sent: deque[float] = deque()
        self._pause_until = 0.0
        self._queue: queue.Queue[tuple[dict, Callable[[dict], None]]] = queue.Queue()
        self._threads: list[threading.Thread] = []

    def apply(self, topics: list[dict]) -> list[dict]:
        """Fill in counts we already know. Returns the topics still to look up."""
        todo = []
        with self._lock:
            for t in topics:
                if not may_be_off(t):
                    continue
                hit = self._cache.get(str(t.get("id")))
                if hit and hit[:3] == _key(t):
                    t["real_unread"] = hit[3]
                else:
                    todo.append(t)
        return todo

    def fix(self, topics: list[dict], done: Callable[[dict], None]) -> None:
        """Look these up in the background, in order; ``done(topic)`` runs on a worker thread after each one."""
        if not topics:
            return
        if not self._threads:
            for _ in range(WORKERS):
                th = threading.Thread(target=self._work, daemon=True)
                th.start()
                self._threads.append(th)
        for t in topics:
            self._queue.put((t, done))

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            text = json.dumps(self._cache)
        tmp = self._file.with_suffix(".tmp")
        try:
            tmp.write_text(text)
            tmp.replace(self._file)
        except OSError:
            pass

    # --- workers ---

    def _work(self) -> None:
        while True:
            topic, done = self._queue.get()
            try:
                if self._lookup(topic):
                    try:
                        done(topic)
                    except Exception:  # noqa: BLE001 - the app may be closing
                        pass
            finally:
                self._queue.task_done()
            if self._queue.unfinished_tasks == 0:
                self.save()

    def _lookup(self, topic: dict) -> bool:
        if not self.apply([topic]):
            # Another load asked for it first.
            return "real_unread" in topic
        tid = topic["id"]
        for _ in range(2):
            self._take()
            try:
                ids = self._client().post_ids_after(tid, topic["last_read_post_number"])
            except RateLimited as e:
                with self._lock:
                    self._pause_until = max(self._pause_until, time.monotonic() + max(1.0, e.wait))
                continue
            except ApiError:
                return False
            topic["real_unread"] = len(ids)
            with self._lock:
                self._cache.pop(str(tid), None)
                self._cache[str(tid)] = [*_key(topic), len(ids)]
                while len(self._cache) > MAX_ENTRIES:
                    self._cache.pop(next(iter(self._cache)))
                self._dirty = True
            return True
        return False

    def _take(self) -> None:
        """Wait for room in the budget and count one request against it."""
        while True:
            with self._lock:
                now = time.monotonic()
                while self._sent and now - self._sent[0] > WINDOW:
                    self._sent.popleft()
                wait = self._pause_until - now
                if wait <= 0 and len(self._sent) < BUDGET:
                    self._sent.append(now)
                    return
                if wait <= 0:
                    wait = WINDOW - (now - self._sent[0])
            time.sleep(max(0.05, wait))
