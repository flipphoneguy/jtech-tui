"""MessageBus client: the long-poll channel the Discourse web app uses for live updates."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Callable

import requests

HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Dont-Chunk": "true",
    "X-SILENCE-LOGGER": "true",
    "Accept": "application/json",
}

Callback = Callable[[dict], None]


class LiveBus:
    """Runs one long poll at a time in a background thread and dispatches messages on the UI thread.

    Changing subscriptions starts a new poll generation; the old request is left to finish on its own and its results are dropped, so nothing blocks. Messages are deduplicated by id per channel.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.client_id = uuid.uuid4().hex
        self._subs: dict[str, tuple[int, list[Callback]]] = {}
        self._lock = threading.Lock()
        self._gen = 0
        self._seq = 0
        self._stopped = False

    # --- subscriptions ---

    def subscribe(self, channel: str, callback: Callback, last_id: int | None = -1) -> None:
        with self._lock:
            cur = self._subs.get(channel)
            if cur is not None:
                cur[1].append(callback)
                return
            self._subs[channel] = (-1 if last_id is None else int(last_id), [callback])
        self._restart()

    def unsubscribe(self, channel: str, callback: Callback | None = None) -> None:
        with self._lock:
            cur = self._subs.get(channel)
            if cur is None:
                return
            if callback is not None and callback in cur[1]:
                cur[1].remove(callback)
            if callback is None or not cur[1]:
                del self._subs[channel]
        self._restart()

    def stop(self) -> None:
        self._stopped = True
        self._gen += 1

    # --- polling ---

    def _restart(self) -> None:
        if self._stopped:
            return
        self._gen += 1
        gen = self._gen
        threading.Thread(target=self._loop, args=(gen,), daemon=True, name=f"messagebus-{gen}").start()

    def _loop(self, gen: int) -> None:
        backoff = 2.0
        while gen == self._gen and not self._stopped:
            with self._lock:
                channels = {ch: last for ch, (last, _) in self._subs.items()}
            if not channels:
                return
            self._seq += 1
            data = {ch: str(last) for ch, last in channels.items()}
            data["__seq"] = str(self._seq)
            client = self.app.client
            try:
                r = client.session.post(
                    client.url(f"/message-bus/{self.client_id}/poll"),
                    data=data,
                    headers=HEADERS,
                    timeout=60,
                )
            except requests.RequestException:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if gen != self._gen:
                return
            if r.status_code != 200:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            backoff = 2.0
            try:
                messages = r.json()
            except ValueError:
                continue
            for m in messages if isinstance(messages, list) else []:
                self._dispatch(m)

    def _dispatch(self, m: dict) -> None:
        ch = m.get("channel")
        mid = m.get("message_id")
        if ch == "/__status":
            # Server tells us current positions; adopt them so we only get new messages.
            with self._lock:
                for name, pos in (m.get("data") or {}).items():
                    if name in self._subs:
                        _, cbs = self._subs[name]
                        self._subs[name] = (int(pos), cbs)
            return
        with self._lock:
            cur = self._subs.get(ch) if ch else None
            if cur is None or not isinstance(mid, int) or mid <= cur[0]:
                return
            self._subs[ch] = (mid, cur[1])
            callbacks = list(cur[1])
        for cb in callbacks:
            try:
                self.app.call_from_thread(cb, m.get("data") or {})
            except Exception:  # noqa: BLE001
                pass
