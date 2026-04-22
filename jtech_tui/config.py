from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://forums.jtechforums.org"


@dataclass
class Config:
    forum_url: str = DEFAULT_URL
    default_feed: str = "latest"
    # Legacy: just the `_t` cookie value. Kept for migration from older configs.
    session_cookie: str = ""
    username: str = ""
    # Full cookie jar dump: [{name, value, domain, path, expires, secure}, ...].
    # Persisting the whole jar (not just `_t`) preserves _forum_session and any
    # other cookies Discourse issues, so the saved session actually survives.
    cookies: list[dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def path() -> Path:
        return Path.home() / ".config" / "jtech-tui" / "config.json"

    @classmethod
    def load(cls) -> "Config":
        p = cls.path()
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return cls()
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        p.write_text(json.dumps(asdict(self), indent=2))
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
