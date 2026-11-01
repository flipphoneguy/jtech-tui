"""Solved answers (A, J) and poll voting (V) for the thread screen."""

from __future__ import annotations

from textual import work

from ..api import ApiError
from ..render import inline_text
from .composer import ChecklistModal, ConfirmModal, MenuModal


class SolvedPollsMixin:
    """Mixed into ThreadScreen ahead of ThreadModMixin."""

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        e = self.stream.current if self._data else None
        if action == "accept":
            return e is not None and bool(e.post.get("can_accept_answer") or e.post.get("can_unaccept_answer"))
        if action == "jump_solution":
            return bool(self._data.get("accepted_answers") or self._data.get("accepted_answer"))
        if action == "vote":
            return e is not None and bool(e.post.get("polls"))
        return super().check_action(action, parameters)

    def solution(self) -> dict | None:
        answers = self._data.get("accepted_answers") or []
        if answers:
            return answers[0]
        return self._data.get("accepted_answer") or None

    # --- solved ---

    def action_jump_solution(self) -> None:
        sol = self.solution()
        if not sol or not sol.get("post_number"):
            self.app.notify("This topic has no solution yet.")
            return
        self.jump_to(int(sol["post_number"]))

    def action_accept(self) -> None:
        e = self._current()
        if e is None:
            return
        p = e.post
        if p.get("can_unaccept_answer") and p.get("accepted_answer"):
            self.app.push_screen(
                ConfirmModal(f"Unmark post #{e.number} as the solution?"),
                lambda ok: ok and self._accept_worker(e.id, False),
            )
        elif p.get("can_accept_answer"):
            old = self.solution()
            q = f"Mark post #{e.number} by @{p.get('username')} as the solution?"
            if old and old.get("post_number") != e.number:
                q += f" (replaces #{old['post_number']})"
            self.app.push_screen(ConfirmModal(q), lambda ok: ok and self._accept_worker(e.id, True))
        else:
            self.app.notify("You can't mark a solution here.", severity="warning")

    @work(thread=True, group="solved")
    def _accept_worker(self, post_id: int, accept: bool) -> None:
        client = self.app.client
        old = self.solution() or {}
        try:
            if accept:
                client.accept_answer(post_id)
            else:
                client.unaccept_answer(post_id)
            fresh = client.topic(
                self._tid,
                near_post=self.stream.current.number if self.stream.current else None,
                deleted=self.app.site.is_staff,
            )
            ids = [post_id] + ([old["id"]] if old.get("id") and old["id"] != post_id else [])
            posts = client.posts_by_ids(self._tid, ids)
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Solution")
            return
        self.app.call_from_thread(self._after_accept, fresh, posts, accept)

    def _after_accept(self, fresh: dict, posts: list[dict], accept: bool) -> None:
        self._data = {**self._data, **{k: v for k, v in fresh.items() if k != "post_stream"}}
        for p in posts:
            self.stream.update_post(p)
        self._update_header()
        self.app.notify("Marked as the solution." if accept else "Solution removed.")

    # --- polls ---

    def action_vote(self) -> None:
        e = self._current()
        if e is None:
            return
        polls = e.post.get("polls") or []
        if not polls:
            self.app.notify("No poll in this post.")
            return
        if len(polls) == 1:
            self._vote_in(e, polls[0])
            return
        items = [(str(i), p.get("name") or f"Poll {i + 1}") for i, p in enumerate(polls)]
        self.app.push_screen(
            MenuModal("Which poll?", items),
            lambda c: c is not None and self._vote_in(e, polls[int(c)]),
        )

    def _vote_in(self, e, poll: dict) -> None:
        name = poll.get("name") or "poll"
        if poll.get("status") == "closed":
            self.app.notify("This poll is closed.")
            return
        if poll.get("type") == "ranked_choice":
            self.app.notify("Ranked-choice polls open in the browser.")
            self.app.open_url(self._post_url(e.number))
            return
        mine = set((e.post.get("polls_votes") or {}).get(name) or [])
        site, base = self.app.site, self.app.client.base_url
        options = poll.get("options") or []
        if poll.get("type") == "multiple":
            lo, hi = int(poll.get("min") or 1), int(poll.get("max") or len(options))
            items = [(o["id"], inline_text(o.get("html") or "", site, base).plain, o["id"] in mine) for o in options]

            def check(ids: list[str]) -> str | None:
                if not ids:
                    return None if mine else f"Pick at least {lo}."
                return None if lo <= len(ids) <= hi else f"Pick between {lo} and {hi}."

            def picked(ids: list[str] | None) -> None:
                if ids is None:
                    return
                self._vote_worker(e.id, name, ids)

            hint = f"pick {lo}–{hi} · space toggle · enter vote · untick all to remove your vote"
            self.app.push_screen(ChecklistModal("Vote", items, hint, validate=check), picked)
            return
        items = [
            (o["id"], ("● " if o["id"] in mine else "○ ") + inline_text(o.get("html") or "", site, base).plain)
            for o in options
        ]
        if mine:
            items.append(("__remove", "✕ Remove my vote"))

        def chosen(oid: str | None) -> None:
            if oid == "__remove":
                self._vote_worker(e.id, name, [])
            elif oid and oid not in mine:
                self._vote_worker(e.id, name, [oid])

        self.app.push_screen(MenuModal("Vote", items), chosen)

    @work(thread=True, group="poll-vote")
    def _vote_worker(self, post_id: int, name: str, options: list[str]) -> None:
        client = self.app.client
        try:
            if options:
                client.poll_vote(post_id, name, options)
            else:
                client.poll_unvote(post_id, name)
            posts = client.posts_by_ids(self._tid, [post_id])
        except ApiError as e:
            self.app.call_from_thread(self.app.report_error, e, "Vote")
            return
        if posts:
            self.app.call_from_thread(self.stream.update_post, posts[0])
        self.app.call_from_thread(self.app.notify, "Vote saved." if options else "Vote removed.")
