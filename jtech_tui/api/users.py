from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .base import BaseClient


class UsersMixin(BaseClient):
    def current_user(self) -> dict:
        """The logged-in user, or ``{}`` when the session is anonymous."""
        d = self.get("/session/current.json")
        return d.get("current_user") or {}

    def user(self, username: str) -> dict:
        return self.get(f"/u/{quote(username)}.json")

    def user_summary(self, username: str) -> dict:
        return self.get(f"/u/{quote(username)}/summary.json")

    def user_actions(self, username: str, filters: str = "4,5", offset: int = 0) -> list[dict]:
        d = self.get("/user_actions.json", params={
            "username": username,
            "filter": filters,
            "offset": offset,
        })
        return d.get("user_actions") or []

    def follow(self, username: str) -> None:
        self.put(f"/follow/{quote(username)}.json")

    def unfollow(self, username: str) -> None:
        self.delete(f"/follow/{quote(username)}.json")

    def set_status(self, description: str, emoji: str = "", ends_at: str = "") -> None:
        status: dict[str, Any] = {"description": description}
        if emoji:
            status["emoji"] = emoji
        if ends_at:
            status["ends_at"] = ends_at
        self.put("/user-status.json", status)

    def clear_status(self) -> None:
        self.delete("/user-status.json")

    # --- notifications ---

    def notifications(self, offset: int = 0, filter_: str = "") -> dict:
        params: dict[str, Any] = {"limit": 60, "recent": "true"}
        if offset:
            params["offset"] = offset
        if filter_:
            params["filter"] = filter_
        return self.get("/notifications.json", params=params)

    def mark_notification_read(self, notification_id: int | None = None) -> None:
        self.put("/notifications/mark-read.json", {"id": notification_id} if notification_id else {})

    # --- gamification ---

    def leaderboard(self, leaderboard_id: int, period: str = "all") -> dict:
        return self.get(f"/leaderboard/{leaderboard_id}.json", params={"period": period})

    # --- emoji ---

    def emojis(self) -> dict[str, list[dict]]:
        return self.get("/emojis.json")

    def emoji_aliases(self) -> dict[str, list[str]]:
        return self.get("/emojis/search-aliases.json")
