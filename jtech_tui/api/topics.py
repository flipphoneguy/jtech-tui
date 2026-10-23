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

    # --- a single topic ---

    def topic(self, tid: int, near_post: int | None = None) -> dict:
        """First chunk of a topic, or the chunk around ``near_post``."""
        if near_post and near_post > 1:
            return self.get(f"/t/{tid}/{near_post}.json")
        return self.get(f"/t/{tid}.json")

    def posts_by_ids(self, tid: int, ids: list[int]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), CHUNK):
            chunk = ids[i:i + CHUNK]
            d = self.get(f"/t/{tid}/posts.json", params=[("post_ids[]", p) for p in chunk])
            out.extend((d.get("post_stream") or {}).get("posts") or [])
        return out

    def mark_read(self, tid: int, post_numbers: list[int], ms_each: int = 5000) -> None:
        if not post_numbers:
            return
        self.post("/topics/timings", {
            "topic_id": tid,
            "topic_time": min(len(post_numbers) * ms_each, 60000),
            "timings": {str(pn): ms_each for pn in post_numbers},
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

    def search(self, query: str, page: int = 1) -> dict:
        params: dict[str, Any] = {"q": query}
        if page > 1:
            params["page"] = page
        return self.get("/search.json", params=params)
