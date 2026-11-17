from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

DEFAULT_URL = "https://forums.jtechforums.org"


@dataclass
class Config:
    forum_url: str = DEFAULT_URL
    default_feed: str = "latest"
    session_cookie: str = ""
    username: str = ""
    password: str = ""
    images: str = "auto"

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
