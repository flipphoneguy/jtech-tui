from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote, urlparse

import requests


class Unauthorized(Exception):
    """Raised when the server returns 401/403."""


NOTIFICATION_TYPES = {
    1: "mentioned",
    2: "replied",
    3: "quoted",
    4: "edited",
    5: "liked",
    6: "private message",
    7: "invited to PM",
    8: "invitee accepted",
    9: "posted",
    10: "moved post",
    11: "linked",
    12: "granted badge",
    13: "invited to topic",
    14: "custom",
    15: "group mentioned",
    16: "group message summary",
    17: "watching first post",
    18: "topic reminder",
    19: "liked consolidated",
    20: "post approved",
    25: "bookmark reminder",
}


class Client:
    def __init__(
        self,
        base_url: str,
        session_cookie: str = "",
        cookies: list[dict] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "jtech-tui/0.1 (+python-textual)"
        self._reactions_cache: list[str] | None = None
        self._has_reactions_plugin: bool | None = None
        if cookies:
            self.load_cookies(cookies)
        elif session_cookie:
            # Legacy config path: only `_t` was persisted. Set it host-scoped.
            host = urlparse(self.base_url).hostname or ""
            self.session.cookies.set("_t", session_cookie, domain=host, path="/")
        self._username: str | None = None

    def dump_cookies(self) -> list[dict]:
        """Serialize the session cookie jar for on-disk persistence.

        Saves name/value/domain/path/expires/secure so a restored session
        actually behaves like the one the server handed us — including
        `_forum_session` and any other cookies Discourse issues, not just
        `_t`.
        """
        out: list[dict] = []
        for c in self.session.cookies:
            out.append({
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path or "/",
                "expires": c.expires,
                "secure": bool(c.secure),
            })
        return out

    def load_cookies(self, cookies: list[dict]) -> None:
        """Restore cookies previously produced by dump_cookies()."""
        for c in cookies:
            name = c.get("name")
            value = c.get("value")
            if not name or value is None:
                continue
            kwargs: dict = {
                "domain": c.get("domain") or "",
                "path": c.get("path") or "/",
            }
            if c.get("expires") is not None:
                kwargs["expires"] = c["expires"]
            if c.get("secure"):
                kwargs["secure"] = True
            self.session.cookies.set(name, value, **kwargs)

    def _url(self, path: str) -> str:
        return self.base_url + path

    def _check(self, r: requests.Response) -> None:
        # Only 401 means "your session is not valid" — 403 is an authorization
        # decision on a valid session (e.g. staff-only endpoint, category
        # permissions) and must NOT trigger a reauth logout.
        if r.status_code == 401:
            raise Unauthorized()
        if r.status_code >= 400:
            try:
                j = r.json()
                errs = j.get("errors") or ([j["error"]] if j.get("error") else [])
            except Exception:
                errs = []
            msg = errs[0] if errs else f"HTTP {r.status_code}"
            raise RuntimeError(msg)

    def _csrf(self) -> str:
        # CSRF tokens are tied to the session and stable for its lifetime on
        # Discourse, so a process-lifetime cache avoids one round-trip per POST.
        cached = getattr(self, "_csrf_cache", "")
        if cached:
            return cached
        r = self.session.get(self._url("/session/csrf.json"), timeout=15)
        self._check(r)
        token = r.json().get("csrf", "")
        self._csrf_cache = token
        return token

    def _get_json(self, path: str) -> dict:
        r = self.session.get(
            self._url(path),
            headers={"Accept": "application/json"},
            timeout=20,
        )
        self._check(r)
        return r.json()

    def _post_json(self, path: str, payload: dict) -> dict:
        csrf = self._csrf()
        r = self.session.post(
            self._url(path),
            json=payload,
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
                "Accept": "application/json",
            },
            timeout=30,
        )
        self._check(r)
        try:
            return r.json()
        except ValueError:
            return {}

    # --- auth ---
    def login(self, username: str, password: str) -> str:
        csrf = self._csrf()
        r = self.session.post(
            self._url("/session"),
            json={"login": username, "password": password},
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
            },
            timeout=30,
        )
        if r.status_code != 200:
            try:
                err = r.json().get("error") or (r.json().get("errors") or [None])[0]
            except Exception:
                err = None
            raise RuntimeError(err or f"login failed: HTTP {r.status_code}")
        try:
            data = r.json()
            self._username = (data.get("user") or {}).get("username")
        except ValueError:
            pass
        return self.session.cookies.get("_t", "") or ""

    def session_cookie(self) -> str:
        return self.session.cookies.get("_t", "") or ""

    def current_username(self) -> str:
        if self._username:
            return self._username
        try:
            d = self._get_json("/session/current.json")
            self._username = (d.get("current_user") or {}).get("username", "") or ""
        except Exception:
            self._username = ""
        return self._username or ""

    # --- feeds ---
    def feed(self, feed: str) -> list[dict]:
        topics, _ = self.feed_page(feed, 0)
        return topics

    def feed_page(self, feed: str, page: int = 0) -> tuple[list[dict], str | None]:
        """Return (topics, more_topics_url) for a feed page. page is 0-based."""
        suffix = f"?page={page}" if page > 0 else ""
        d = self._get_json(f"/{feed}.json{suffix}")
        tl = d.get("topic_list") or {}
        return (tl.get("topics") or []), tl.get("more_topics_url")

    def categories(self) -> list[dict]:
        d = self._get_json("/categories.json")
        return ((d.get("category_list") or {}).get("categories") or [])

    def category_topics(self, slug: str, cid: int) -> list[dict]:
        d = self._get_json(f"/c/{slug}/{cid}.json")
        return ((d.get("topic_list") or {}).get("topics") or [])

    # --- threads / posts ---
    def thread(self, tid: int, near_post: int | None = None) -> dict:
        """Fetch the initial chunk of a thread.

        Pass ``near_post`` to load the 20 posts centred around that post
        number (Discourse returns them via ``/t/{id}/{post_number}.json``).
        Omit or pass ``None`` / ``1`` to get the first 20 posts.
        """
        if near_post and near_post > 1:
            return self._get_json(f"/t/{tid}/{near_post}.json")
        return self._get_json(f"/t/{tid}.json")

    def thread_fill_missing(
        self, tid: int, missing_ids: list[int], max_workers: int = 6
    ) -> list[dict]:
        """Fetch posts in parallel chunks of 20. Returns them in arbitrary order."""
        if not missing_ids:
            return []
        chunks = [missing_ids[i:i + 20] for i in range(0, len(missing_ids), 20)]

        def fetch(chunk: list[int]) -> list[dict]:
            qs = "&".join(f"post_ids[]={pid}" for pid in chunk)
            try:
                d = self._get_json(f"/t/{tid}/posts.json?{qs}")
            except Exception:  # noqa: BLE001
                return []
            return ((d.get("post_stream") or {}).get("posts") or [])

        out: list[dict] = []
        from concurrent.futures import ThreadPoolExecutor
        workers = min(max_workers, max(1, len(chunks)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for got in ex.map(fetch, chunks):
                out.extend(got)
        return out

    def post_raw(self, post_id: int) -> str:
        """Fetch a post's raw markdown (useful for quote-reply)."""
        try:
            d = self._get_json(f"/posts/{post_id}.json")
        except Exception:  # noqa: BLE001
            return ""
        return d.get("raw") or ""

    def edit_post(self, post_id: int, raw: str, edit_reason: str | None = None) -> None:
        csrf = self._csrf()
        payload: dict = {"post": {"raw": raw}}
        if edit_reason:
            payload["post"]["edit_reason"] = edit_reason
        r = self.session.put(
            self._url(f"/posts/{post_id}.json"),
            json=payload,
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": csrf,
                "Accept": "application/json",
            },
            timeout=30,
        )
        self._check(r)

    def delete_post(self, post_id: int) -> None:
        csrf = self._csrf()
        r = self.session.delete(
            self._url(f"/posts/{post_id}"),
            headers={
                "X-CSRF-Token": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            },
            timeout=20,
        )
        self._check(r)

    # --- users ---
    def user_profile(self, username: str) -> dict:
        return self._get_json(f"/u/{quote(username)}.json")

    def user_actions(self, username: str, filters: str = "4,5", limit: int = 30) -> list[dict]:
        d = self._get_json(
            f"/user_actions.json?username={quote(username)}&filter={filters}&limit={limit}"
        )
        return d.get("user_actions") or []

    # --- leaderboard (discourse-gamification plugin) ---
    def default_leaderboard_id(self) -> int | None:
        """Return the site-default gamification board id, if the plugin is on.

        Discourse exposes this as a top-level ``default_gamification_leaderboard_id``
        key in ``/site.json`` (not under ``site_settings``).
        """
        try:
            d = self._get_json("/site.json")
        except Exception:  # noqa: BLE001
            return None
        val = d.get("default_gamification_leaderboard_id")
        if val is None:
            val = (d.get("site_settings") or {}).get(
                "default_gamification_leaderboard_id"
            )
        try:
            return int(val) if val not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            return None

    def leaderboard(self, leaderboard_id: int, period: str = "all") -> dict:
        """Fetch one leaderboard with scores for the given period.

        Response fields we use: ``users`` (list of {username, avatar_template,
        total_score, position}), ``personal`` (current user's row), and
        ``leaderboard`` (id, name, period_filter_disabled)."""
        return self._get_json(
            f"/leaderboard/{leaderboard_id}.json?period={quote(period)}"
        )

    # --- reaction users ---
    def post_action_users(self, post_id: int, post_action_type_id: int = 2) -> list[str]:
        """Return usernames who performed the given post action (2 = like)."""
        try:
            d = self._get_json(
                f"/post_action_users.json?id={post_id}&post_action_type_id={post_action_type_id}"
            )
        except Exception:  # noqa: BLE001
            return []
        users = d.get("post_action_users") or []
        return [u.get("username") for u in users if u.get("username")]

    def reaction_users(self, post_id: int, reaction_id: str) -> list[str]:
        """Return usernames who reacted with ``reaction_id`` (plugin endpoint)."""
        try:
            d = self._get_json(
                f"/discourse-reactions/posts/{post_id}/reactions-users.json?"
                f"reaction_value={quote(reaction_id)}"
            )
        except Exception:  # noqa: BLE001
            return []
        users = d.get("reaction_users") or d.get("users") or []
        return [u.get("username") for u in users if u.get("username")]

    # --- uploads ---
    def upload(self, file_path: str, upload_type: str = "composer") -> dict:
        csrf = self._csrf()
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f)}
            data = {"type": upload_type, "synchronous": "true"}
            r = self.session.post(
                self._url("/uploads.json"),
                files=files,
                data=data,
                headers={
                    "X-CSRF-Token": csrf,
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json",
                },
                timeout=120,
            )
        self._check(r)
        try:
            return r.json()
        except ValueError:
            return {}

    def mark_read(self, topic_id: int, post_numbers: list[int]) -> None:
        """Mark post numbers as read via Discourse's timings endpoint.

        Fire-and-forget — any exception is swallowed so this never blocks the UI.
        """
        if not post_numbers:
            return
        timings = {str(pn): 5000 for pn in post_numbers}
        try:
            csrf = self._csrf()
            self.session.post(
                self._url("/topics/timings"),
                json={
                    "topic_id": topic_id,
                    "topic_time": min(len(post_numbers) * 5000, 60000),
                    "timings": timings,
                },
                headers={
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRF-Token": csrf,
                    "Accept": "application/json",
                },
                timeout=15,
            )
        except Exception:  # noqa: BLE001
            pass

    def reply(
        self,
        topic_id: int,
        raw: str,
        reply_to_post_number: int | None = None,
    ) -> None:
        payload: dict = {"topic_id": topic_id, "raw": raw}
        if reply_to_post_number:
            payload["reply_to_post_number"] = reply_to_post_number
        self._post_json("/posts", payload)

    def create_topic(self, title: str, raw: str, category_id: int) -> None:
        self._post_json("/posts", {
            "title": title,
            "raw": raw,
            "category": category_id,
        })

    # --- PMs ---
    def private_messages(self, username: str) -> list[dict]:
        d = self._get_json(f"/topics/private-messages/{quote(username)}.json")
        return ((d.get("topic_list") or {}).get("topics") or [])

    def send_pm(self, title: str, raw: str, recipients: list[str]) -> None:
        self._post_json("/posts", {
            "title": title,
            "raw": raw,
            "archetype": "private_message",
            "target_recipients": ",".join(recipients),
        })

    # --- notifications ---
    def notifications(self) -> list[dict]:
        d = self._get_json("/notifications.json")
        return d.get("notifications") or []

    # --- search ---
    def search(self, query: str) -> dict:
        return self._get_json(f"/search.json?q={quote(query)}")

    # --- reactions ---
    def supported_reactions(self) -> list[str]:
        """Return the reaction ids the server accepts.

        Probes the Discourse Reactions plugin. If the plugin isn't installed,
        returns ``["heart"]`` so users still get the core "like" action (which
        toggle_reaction falls back to via /post_actions.json). Cached.
        """
        if self._reactions_cache is not None:
            return self._reactions_cache

        def _extract(data) -> list[str]:
            if isinstance(data, list):
                out: list[str] = []
                for item in data:
                    if isinstance(item, dict):
                        rid = item.get("id") or item.get("reaction") or item.get("name")
                        if rid:
                            out.append(str(rid))
                    elif item:
                        out.append(str(item))
                return out
            if isinstance(data, str):
                for delim in ("|", ","):
                    if delim in data:
                        return [s.strip() for s in data.split(delim) if s.strip()]
                return [data.strip()] if data.strip() else []
            return []

        _KEYS = (
            "discourse_reactions_enabled_reactions",
            "enabled_reactions",
            "reactions",
            "reaction_list",
        )

        def _find(obj, depth: int = 0) -> list[str]:
            if depth > 5 or obj is None:
                return []
            if isinstance(obj, dict):
                for k in _KEYS:
                    if k in obj:
                        ids = _extract(obj[k])
                        if ids:
                            return ids
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        hit = _find(v, depth + 1)
                        if hit:
                            return hit
            elif isinstance(obj, list):
                for item in obj:
                    hit = _find(item, depth + 1)
                    if hit:
                        return hit
            return []

        plugin_detected = False
        result: list[str] = []
        for path in (
            "/discourse-reactions/reactions.json",
            "/discourse_reactions/reactions.json",
        ):
            try:
                d = self._get_json(path)
            except Exception:  # noqa: BLE001
                continue
            plugin_detected = True
            result = _find(d)
            if result:
                break
        if not result:
            try:
                d = self._get_json("/site.json")
                hits = _find(d)
                if hits:
                    result = hits
                    plugin_detected = True
            except Exception:  # noqa: BLE001
                pass

        self._has_reactions_plugin = plugin_detected
        if not result:
            # Plugin not detected — show only the core "like" (thumbs up).
            result = ["+1"]

        self._reactions_cache = result
        return result

    _LIKE_IDS = ("+1", "heart", "like", "thumbsup", "thumbs_up")

    def _toggle_core_like(self, post_id: int) -> dict:
        """Toggle the core Discourse 'like' action on a post."""
        csrf = self._csrf()
        headers = {
            "X-CSRF-Token": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
        }
        r = self.session.post(
            self._url("/post_actions.json"),
            json={"id": post_id, "post_action_type_id": 2, "flag_topic": False},
            headers={**headers, "Content-Type": "application/json"},
            timeout=20,
        )
        if 200 <= r.status_code < 300:
            try:
                return r.json()
            except ValueError:
                return {}
        # Already liked? Try removing the like.
        r2 = self.session.delete(
            self._url(f"/post_actions/{post_id}?post_action_type_id=2"),
            headers=headers,
            timeout=20,
        )
        if 200 <= r2.status_code < 300:
            try:
                return r2.json()
            except ValueError:
                return {}
        self._check(r)
        return {}

    def toggle_reaction(self, post_id: int, reaction_id: str) -> dict:
        """Toggle a reaction on a post.

        Uses the Discourse Reactions plugin endpoint when available. For
        like-ish ids (+1/heart/like/thumbsup) always uses the core
        /post_actions endpoint — it's universal and was confirmed to work.
        """
        if reaction_id in self._LIKE_IDS:
            return self._toggle_core_like(post_id)

        if self._has_reactions_plugin is None:
            self.supported_reactions()
        if not self._has_reactions_plugin:
            # Plugin not installed; we can't apply a custom reaction.
            raise RuntimeError(
                f"Reaction '{reaction_id}' not available — Reactions plugin not detected."
            )

        csrf = self._csrf()
        headers = {
            "X-CSRF-Token": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
        }
        url = self._url(
            f"/discourse-reactions/posts/{post_id}/custom-reactions/{quote(reaction_id)}/toggle.json"
        )
        r = self.session.put(url, headers=headers, timeout=20)
        self._check(r)
        try:
            return r.json()
        except ValueError:
            return {}
