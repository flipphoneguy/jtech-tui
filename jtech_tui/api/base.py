from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

USER_AGENT = "jtech-tui/0.2.0 (+https://github.com/flipphoneguy/jtech-tui)"


class ApiError(RuntimeError):
    """Any non-2xx response that isn't an auth problem."""

    def __init__(self, message: str, status: int = 0, error_type: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type


class Unauthorized(ApiError):
    """The session is gone (logged out, expired, or blocked before reaching Discourse)."""


class Forbidden(ApiError):
    """Logged in, but not allowed to do this."""


class NotFound(ApiError):
    pass


class RateLimited(ApiError):
    def __init__(self, message: str, wait: float) -> None:
        super().__init__(message, 429, "rate_limit")
        self.wait = wait


def _error_message(r: requests.Response) -> tuple[str, str]:
    try:
        j = r.json()
    except ValueError:
        return f"HTTP {r.status_code}", ""
    if not isinstance(j, dict):
        return f"HTTP {r.status_code}", ""
    errs = j.get("errors") or ([j["error"]] if j.get("error") else [])
    msg = str(errs[0]) if errs else f"HTTP {r.status_code}"
    return msg, str(j.get("error_type") or "")


class BaseClient:
    """HTTP core shared by every endpoint mixin.

    Thread-safe enough for Textual workers: requests.Session is shared, the CSRF token is cached behind a lock, and cookie rotation is reported through ``on_cookie_change`` so the caller can persist the newest ``_t``.
    """

    def __init__(
        self,
        base_url: str,
        session_cookie: str = "",
        on_cookie_change: Callable[[str], None] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.host = urlparse(self.base_url).hostname or ""
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=12)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._csrf_token = ""
        self._csrf_lock = threading.Lock()
        self._on_cookie_change = on_cookie_change
        self._last_cookie = session_cookie
        if session_cookie:
            self.session.cookies.set("_t", session_cookie, domain=self.host)

    def url(self, path: str) -> str:
        return path if path.startswith("http") else self.base_url + path

    # --- cookies / csrf ---

    def session_cookie(self) -> str:
        for c in self.session.cookies:
            if c.name == "_t" and c.value:
                return c.value
        return ""

    def _note_cookie(self) -> None:
        cur = self.session_cookie()
        if cur and cur != self._last_cookie:
            self._last_cookie = cur
            if self._on_cookie_change:
                try:
                    self._on_cookie_change(cur)
                except Exception:  # noqa: BLE001
                    pass

    def csrf(self, refresh: bool = False) -> str:
        with self._csrf_lock:
            if self._csrf_token and not refresh:
                return self._csrf_token
            r = self.session.get(
                self.url("/session/csrf.json"),
                headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
                timeout=15,
            )
            self._raise_for(r)
            self._csrf_token = r.json().get("csrf", "")
            return self._csrf_token

    # --- request core ---

    def _raise_for(self, r: requests.Response) -> None:
        if r.status_code < 400:
            return
        ctype = r.headers.get("Content-Type", "")
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("Retry-After") or 0)
            except ValueError:
                wait = 0.0
            if not wait:
                try:
                    wait = float((r.json().get("extras") or {}).get("wait_seconds") or 5)
                except Exception:  # noqa: BLE001
                    wait = 5.0
            raise RateLimited(f"Rate limited, retry in {int(wait)}s", wait)
        if r.status_code in (401, 403) and "json" not in ctype:
            # Cloudflare answers for Discourse when the session is bad.
            raise Unauthorized("Not logged in", r.status_code, "not_logged_in")
        msg, etype = _error_message(r)
        if r.status_code == 401 or etype == "not_logged_in":
            raise Unauthorized(msg, r.status_code, etype)
        if r.status_code == 403:
            raise Forbidden(msg, 403, etype)
        if r.status_code == 404:
            raise NotFound(msg, 404, etype)
        raise ApiError(msg, r.status_code, etype)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        timeout: float = 20,
        retries: int = 2,
    ) -> Any:
        """Send a request and return parsed JSON (or ``{}`` for empty bodies)."""
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        write = method.upper() != "GET"
        attempt = 0
        while True:
            if write:
                headers["X-CSRF-Token"] = self.csrf()
            try:
                r = self.session.request(
                    method,
                    self.url(path),
                    params=params,
                    json=json,
                    data=data,
                    files=files,
                    headers=headers,
                    timeout=timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < retries and not write:
                    attempt += 1
                    time.sleep(0.5 * attempt)
                    continue
                raise ApiError(f"Network error: {e.__class__.__name__}") from e
            self._note_cookie()
            if write and r.status_code == 403 and "BAD CSRF" in r.text[:200] and attempt == 0:
                self.csrf(refresh=True)
                attempt += 1
                continue
            if r.status_code == 429 and attempt < retries:
                try:
                    self._raise_for(r)
                except RateLimited as e:
                    if e.wait <= 10:
                        attempt += 1
                        time.sleep(e.wait)
                        continue
                    raise
            if r.status_code >= 500 and attempt < retries and not write:
                attempt += 1
                time.sleep(0.5 * attempt)
                continue
            self._raise_for(r)
            if not r.content:
                return {}
            try:
                return r.json()
            except ValueError:
                return {}

    def get(self, path: str, params: Any = None, **kw: Any) -> Any:
        return self.request("GET", path, params=params, **kw)

    def post(self, path: str, json: Any = None, **kw: Any) -> Any:
        return self.request("POST", path, json=json, **kw)

    def put(self, path: str, json: Any = None, **kw: Any) -> Any:
        return self.request("PUT", path, json=json, **kw)

    def delete(self, path: str, json: Any = None, **kw: Any) -> Any:
        return self.request("DELETE", path, json=json, **kw)

    # --- auth ---

    def login(self, username: str, password: str, second_factor: str = "") -> dict:
        """Password login. Returns the ``user`` dict. Raises ApiError with a readable message."""
        payload: dict[str, Any] = {"login": username, "password": password}
        if second_factor:
            payload["second_factor_token"] = second_factor
            payload["second_factor_method"] = 1
        r = self.session.post(
            self.url("/session"),
            json=payload,
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": self.csrf(),
            },
            timeout=30,
        )
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200 or data.get("error"):
            if data.get("second_factor_required") or data.get("reason") == "invalid_second_factor":
                raise ApiError(
                    data.get("error") or "Two-factor code required",
                    r.status_code,
                    "second_factor_required",
                )
            msg = data.get("error") or (data.get("errors") or [None])[0]
            raise ApiError(msg or f"Login failed: HTTP {r.status_code}", r.status_code)
        # A fresh session needs a fresh token.
        self._csrf_token = ""
        self._note_cookie()
        return data.get("user") or {}

    def upload_file(self, path: str, upload_type: str = "composer") -> dict:
        with open(path, "rb") as f:
            return self.request(
                "POST",
                "/uploads.json",
                files={"file": (os.path.basename(path), f)},
                data={"type": upload_type, "synchronous": "true"},
                timeout=120,
                retries=0,
            )
