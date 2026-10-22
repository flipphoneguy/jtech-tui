from __future__ import annotations

from typing import Any

from .base import BaseClient


class ModMixin(BaseClient):
    """Staff endpoints. The server enforces permissions; the UI only offers what ``can_*`` flags allow."""

    # --- topic ---

    def topic_status(self, tid: int, status: str, enabled: bool, until: str = "") -> dict:
        """``status`` is one of closed, archived, visible, pinned, pinned_globally."""
        payload: dict[str, Any] = {"status": status, "enabled": "true" if enabled else "false"}
        if until:
            payload["until"] = until
        return self.put(f"/t/{tid}/status", payload)

    def update_topic(
        self,
        tid: int,
        *,
        title: str | None = None,
        category_id: int | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if category_id is not None:
            payload["category_id"] = category_id
        if tags is not None:
            payload["tags"] = tags
        return self.put(f"/t/-/{tid}.json", payload)

    def delete_topic(self, tid: int) -> None:
        self.delete(f"/t/{tid}.json")

    def recover_topic(self, tid: int) -> None:
        self.put(f"/t/{tid}/recover")

    def slow_mode(self, tid: int, seconds: int, until: str = "") -> dict:
        payload: dict[str, Any] = {"seconds": seconds}
        if until:
            payload["enabled_until"] = until
        return self.put(f"/t/{tid}/slow_mode", payload)

    def topic_timer(self, tid: int, status_type: str, time: str, based_on_last_post: bool = False) -> dict:
        """``status_type``: close, open, delete, reminder, bump, publish_to_category, delete_replies."""
        return self.post(f"/t/{tid}/timer", {
            "status_type": status_type,
            "time": time,
            "based_on_last_post": based_on_last_post,
        })

    def move_posts(
        self,
        tid: int,
        post_ids: list[int],
        *,
        destination_topic_id: int | None = None,
        title: str = "",
        category_id: int | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"post_ids": post_ids}
        if destination_topic_id:
            payload["destination_topic_id"] = destination_topic_id
        else:
            payload["title"] = title
            if category_id:
                payload["category_id"] = category_id
        return self.post(f"/t/{tid}/move-posts", payload)

    def merge_topic(self, tid: int, destination_topic_id: int) -> dict:
        return self.post(f"/t/{tid}/merge-topic", {"destination_topic_id": destination_topic_id})

    def change_owner(self, tid: int, post_ids: list[int], username: str) -> dict:
        return self.post(f"/t/{tid}/change-owner", {"username": username, "post_ids": post_ids})

    # --- post ---

    def set_wiki(self, post_id: int, wiki: bool) -> dict:
        return self.put(f"/posts/{post_id}/wiki", {"wiki": wiki})

    def set_locked(self, post_id: int, locked: bool) -> dict:
        return self.put(f"/posts/{post_id}/locked", {"locked": locked})

    def set_notice(self, post_id: int, notice: str) -> dict:
        return self.put(f"/posts/{post_id}/notice", {"notice": notice})

    # --- review queue ---

    def reviewables(self, status: str = "pending", offset: int = 0) -> dict:
        params: dict[str, Any] = {"status": status}
        if offset:
            params["offset"] = offset
        return self.get("/review.json", params=params)

    def review_count(self) -> int:
        return int(self.get("/review/count.json").get("count") or 0)

    def perform_review(self, reviewable_id: int, action: str, version: int) -> dict:
        return self.put(f"/review/{reviewable_id}/perform/{action}", params={"version": version})

    # --- users ---

    def admin_user(self, user_id: int) -> dict:
        return self.get(f"/admin/users/{user_id}.json")

    def silence_user(self, user_id: int, until: str, reason: str, message: str = "") -> dict:
        payload: dict[str, Any] = {"silenced_till": until, "reason": reason}
        if message:
            payload["message"] = message
        return self.put(f"/admin/users/{user_id}/silence", payload)

    def unsilence_user(self, user_id: int) -> dict:
        return self.put(f"/admin/users/{user_id}/unsilence")

    def suspend_user(self, user_id: int, until: str, reason: str, message: str = "") -> dict:
        payload: dict[str, Any] = {"suspend_until": until, "reason": reason}
        if message:
            payload["message"] = message
        return self.put(f"/admin/users/{user_id}/suspend", payload)

    def unsuspend_user(self, user_id: int) -> dict:
        return self.put(f"/admin/users/{user_id}/unsuspend")

    def user_notes(self, user_id: int) -> list[dict]:
        d = self.get("/user_notes", params={"user_id": user_id})
        return d.get("user_notes") or []

    def add_user_note(self, user_id: int, raw: str) -> dict:
        return self.post("/user_notes", {"user_note": {"raw": raw, "user_id": user_id}})

    def staff_action_logs(self, offset: int = 0) -> dict:
        params: dict[str, Any] = {}
        if offset:
            params["offset"] = offset
        return self.get("/admin/logs/staff_action_logs.json", params=params or None)
