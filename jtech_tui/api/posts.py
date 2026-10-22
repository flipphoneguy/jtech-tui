from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .base import BaseClient

LIKE = 2


class PostsMixin(BaseClient):
    def post_detail(self, post_id: int) -> dict:
        """Full post JSON, including ``raw``."""
        return self.get(f"/posts/{post_id}.json")

    def post_raw(self, post_id: int) -> str:
        return self.post_detail(post_id).get("raw") or ""

    def reply(
        self,
        topic_id: int,
        raw: str,
        reply_to_post_number: int | None = None,
        extra: dict | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"topic_id": topic_id, "raw": raw}
        if reply_to_post_number:
            payload["reply_to_post_number"] = reply_to_post_number
        if extra:
            payload.update(extra)
        return self.post("/posts.json", payload)

    def edit_post(self, post_id: int, raw: str, edit_reason: str = "") -> dict:
        body: dict[str, Any] = {"raw": raw}
        if edit_reason:
            body["edit_reason"] = edit_reason
        return self.put(f"/posts/{post_id}.json", {"post": body})

    def delete_post(self, post_id: int) -> None:
        self.delete(f"/posts/{post_id}.json")

    def recover_post(self, post_id: int) -> None:
        self.put(f"/posts/{post_id}/recover.json")

    def revision(self, post_id: int, number: int | str = "latest") -> dict:
        return self.get(f"/posts/{post_id}/revisions/{number}.json")

    # --- reactions ---

    def toggle_reaction(self, post_id: int, reaction: str) -> dict:
        """Toggle via discourse-reactions. Returns the updated post (reactions, current_user_reaction…)."""
        return self.put(
            f"/discourse-reactions/posts/{post_id}/custom-reactions/{quote(reaction, safe='')}/toggle.json"
        )

    def reaction_users(self, post_id: int) -> list[tuple[str, list[str]]]:
        d = self.get(f"/discourse-reactions/posts/{post_id}/reactions-users.json")
        groups: list[tuple[str, list[str]]] = []
        for item in d.get("reaction_users") or []:
            rid = item.get("id") or ""
            users = [u["username"] for u in item.get("users") or [] if u.get("username")]
            if rid:
                groups.append((rid, users))
        return groups

    def like_users(self, post_id: int) -> list[str]:
        d = self.get("/post_action_users.json", params={"id": post_id, "post_action_type_id": LIKE})
        return [u["username"] for u in d.get("post_action_users") or [] if u.get("username")]

    # --- flags / actions ---

    def act(self, post_id: int, action_type: int, message: str = "", flag_topic: bool = False) -> dict:
        payload: dict[str, Any] = {
            "id": post_id,
            "post_action_type_id": action_type,
            "flag_topic": flag_topic,
        }
        if message:
            payload["message"] = message
        return self.post("/post_actions.json", payload)

    def undo_act(self, post_id: int, action_type: int) -> dict:
        return self.delete(f"/post_actions/{post_id}.json", params={"post_action_type_id": action_type})

    # --- bookmarks ---

    def bookmark_post(self, post_id: int, name: str = "", reminder_at: str = "") -> dict:
        payload: dict[str, Any] = {"bookmarkable_id": post_id, "bookmarkable_type": "Post"}
        if name:
            payload["name"] = name
        if reminder_at:
            payload["reminder_at"] = reminder_at
        return self.post("/bookmarks.json", payload)

    def delete_bookmark(self, bookmark_id: int) -> None:
        self.delete(f"/bookmarks/{bookmark_id}.json")

    # --- solved ---

    def accept_answer(self, post_id: int) -> dict:
        return self.post("/solution/accept", {"id": post_id})

    def unaccept_answer(self, post_id: int) -> dict:
        return self.post("/solution/unaccept", {"id": post_id})

    # --- polls ---

    def poll_vote(self, post_id: int, poll_name: str, options: list[str]) -> dict:
        return self.put("/polls/vote", {"post_id": post_id, "poll_name": poll_name, "options": options})

    def poll_unvote(self, post_id: int, poll_name: str) -> dict:
        return self.delete("/polls/vote", {"post_id": post_id, "poll_name": poll_name})
