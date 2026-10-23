from __future__ import annotations

from .base import (
    ApiError,
    Forbidden,
    NotFound,
    RateLimited,
    Unauthorized,
)
from .jtech import JtechMixin, whisper_fields
from .mod import ModMixin
from .posts import PostsMixin
from .topics import TopicsMixin
from .users import UsersMixin


class Client(TopicsMixin, PostsMixin, UsersMixin, ModMixin, JtechMixin):
    """Discourse client for jtech forums. Every method is blocking; call from workers."""


__all__ = [
    "ApiError",
    "Client",
    "Forbidden",
    "NotFound",
    "RateLimited",
    "Unauthorized",
    "whisper_fields",
]
