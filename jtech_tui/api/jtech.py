from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .base import BaseClient

MOD = "/discourse-mod-categories"


def whisper_fields(user_ids: list[int], group_ids: list[int], badge_ids: list[int]) -> dict[str, Any]:
    """Extra params that turn a new post into a jtech-tools whisper."""
    return {
        "mod_whisper": "true",
        "mod_whisper_target_user_ids": user_ids,
        "mod_whisper_target_group_ids": group_ids,
        "mod_whisper_target_badge_ids": badge_ids,
    }


class JtechMixin(BaseClient):
    """Endpoints from the jtech-tools plugin (whispers, topic tools, notes feed)."""

    def set_post_whisper(
        self,
        post_id: int,
        armed: bool,
        user_ids: list[int] | None = None,
        group_ids: list[int] | None = None,
        badge_ids: list[int] | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"mod_whisper": "true" if armed else "false"}
        if armed:
            payload["mod_whisper_target_user_ids"] = user_ids or []
            payload["mod_whisper_target_group_ids"] = group_ids or []
            payload["mod_whisper_target_badge_ids"] = badge_ids or []
        return self.put(f"{MOD}/post/{post_id}/whisper", payload)

    def add_whisper_participant(self, topic_id: int, post_id: int, username: str) -> dict:
        return self.post(f"{MOD}/topic/{topic_id}/whisper-participant", {"post_id": post_id, "username": username})

    def post_viewers(self, post_id: int) -> dict:
        return self.get(f"{MOD}/post/{post_id}/viewers")

    def update_topic_tools(self, topic_id: int, **fields: Any) -> dict:
        """footer_message, pinned_post_id (0 to clear), reply_prompt, require_reply_approval…"""
        return self.put(f"{MOD}/topic/{topic_id}", fields)

    def notes_feed(self) -> list[dict]:
        return self.get(f"{MOD}/notes-feed").get("notes") or []

    def notes_feed_seen(self) -> None:
        self.post(f"{MOD}/notes-feed/seen")

    def badges(self) -> list[dict]:
        return self.get("/badges.json").get("badges") or []

    def user_id(self, username: str) -> int | None:
        u = self.get(f"/u/{quote(username)}.json").get("user") or {}
        return u.get("id")
