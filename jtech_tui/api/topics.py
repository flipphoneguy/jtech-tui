from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .base import BaseClient

# Discourse returns at most this many posts per chunk.
CHUNK = 20


class TopicsMixin(BaseClient):
    # --- lists ---

    def topic_list(self, path: str, page: int = 0, params: dict | None = None) -> dict:
        """Fetch any ``/…/l/…`` style list. Returns the raw response (topic_list + users)."""
        q = dict(params or {})
        if page:
            q["page"] = page
        return self.get(f"{path}.json", params=q or None)

    def feed(self, feed: str, page: int = 0) -> dict:
        return self.topic_list(f"/{feed}", page)

    def category_feed(self, category: dict, feed: str = "latest", page: int = 0) -> dict:
        slug = category.get("slug") or "-"
        return self.topic_list(f"/c/{quote(slug)}/{category['id']}/l/{feed}", page)

    def tag_feed(self, tag: str, page: int = 0) -> dict:
        return self.topic_list(f"/tag/{quote(tag)}", page)

    def categories(self) -> list[dict]:
        """Every category the user can see, subcategories included."""
        d = self.get("/categories.json", params={"include_subcategories": "true"})
        cats = (d.get("category_list") or {}).get("categories") or []
        # Lazy-loaded sites leave some subcategories out; fetch them by id.
        have = {c["id"] for c in cats}
        missing = sorted({i for c in cats for i in c.get("subcategory_ids") or []} - have)
        if missing:
            found = self.get("/categories/find.json", params=[("ids[]", i) for i in missing])
            cats += found.get("categories") or []
        return cats

    def private_messages(self, username: str, box: str = "") -> dict:
        suffix = f"-{box}" if box else ""
        return self.get(f"/topics/private-messages{suffix}/{quote(username)}.json")

    def group_messages(self, username: str, group: str) -> dict:
        return self.get(f"/topics/private-messages-group/{quote(username)}/{quote(group)}.json")

    def dismiss_new(self, category_id: int | None = None) -> None:
        payload: dict[str, Any] = {"dismiss_topics": True, "dismiss_posts": True}
        if category_id:
            payload["category_id"] = category_id
        self.put("/topics/reset-new", payload)

    def dismiss_topic_ids(self, topic_ids: list[int]) -> None:
        """Dismiss only these topics: new ones stop being new, unread replies are marked read."""
        if not topic_ids:
            return
        for op in ("dismiss_topics", "dismiss_posts"):
            self.put("/topics/bulk", {"topic_ids": topic_ids, "operation": {"type": op}})

    # --- a single topic ---

    def topic(self, tid: int, near_post: int | None = None, deleted: bool = False) -> dict:
        """First chunk of a topic, or the chunk around ``near_post``. ``deleted`` includes deleted posts (staff only)."""
        params = {"show_deleted": "true"} if deleted else None
        if near_post and near_post > 1:
            return self.get(f"/t/{tid}/{near_post}.json", params=params)
        return self.get(f"/t/{tid}.json", params=params)

    def posts_by_ids(self, tid: int, ids: list[int]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), CHUNK):
            chunk = ids[i:i + CHUNK]
            d = self.get(f"/t/{tid}/posts.json", params=[("post_ids[]", p) for p in chunk])
            out.extend((d.get("post_stream") or {}).get("posts") or [])
        return out

    def post_ids_after(self, tid: int, post_number: int) -> list[int]:
        """Ids of the posts you can see after ``post_number``. Doesn't count as a topic view."""
        return self.get(f"/t/{tid}/post_ids.json", params={"post_number": post_number}).get("post_ids") or []

    def mark_read(self, tid: int, timings: dict[int, int], topic_ms: int) -> None:
        """Report reading time in ms per post number and for the topic; the server uses the total to auto-track."""
        if not timings:
            return
        self.post("/topics/timings", {
            "topic_id": tid,
            "topic_time": topic_ms,
            "timings": {str(pn): ms for pn, ms in timings.items()},
        })

    def set_topic_notification_level(self, tid: int, level: int) -> None:
        self.request("POST", f"/t/{tid}/notifications", data={"notification_level": level})

    def create_topic(
        self,
        title: str,
        raw: str,
        category_id: int | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"title": title, "raw": raw}
        if category_id:
            payload["category"] = category_id
        if tags:
            payload["tags"] = tags
        return self.post("/posts.json", payload)

    def send_pm(self, title: str, raw: str, recipients: list[str]) -> dict:
        return self.post("/posts.json", {
            "title": title,
            "raw": raw,
            "archetype": "private_message",
            "target_recipients": ",".join(recipients),
        })

    # --- search ---

    def user_search(self, term: str) -> list[dict]:
        return self.get("/u/search/users.json", params={"term": term}).get("users") or []

    def tag_search(self, q: str) -> list[dict]:
        return self.get("/tags/filter/search.json", params={"q": q}).get("results") or []

    def topic_search(self, term: str) -> list[dict]:
        return self.get("/search/query.json", params={"term": term, "type_filter": "topic"}).get("topics") or []

    def search(self, query: str, page: int = 1) -> dict:
        params: dict[str, Any] = {"q": query}
        if page > 1:
            params["page"] = page
        return self.get("/search.json", params=params)
