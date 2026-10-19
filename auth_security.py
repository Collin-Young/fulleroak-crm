"""Authentication helpers for the local CRM web application."""

from __future__ import annotations

import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from vumber_client import _protect, _unprotect


ROLES = ("owner", "manager", "agent", "viewer")
WRITE_ROLES = {"owner", "manager", "agent"}
ADMIN_ROLES = {"owner", "manager"}


def load_or_create_secret(path: Path) -> bytes:
    configured = os.environ.get("CRM_SECRET_KEY", "")
    if configured:
        return configured.encode("utf-8")
    if path.exists():
        return _unprotect(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(_protect(secret))
    temporary.replace(path)
    return secret


class LoginRateLimiter:
    """Small per-process limiter suitable for this localhost deployment."""

    def __init__(self, attempts: int = 5, window_seconds: int = 15 * 60) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def allowed(self, key: str) -> bool:
        now = time.monotonic()
        failures = self._failures[key]
        while failures and failures[0] <= now - self.window_seconds:
            failures.popleft()
        return len(failures) < self.attempts

    def fail(self, key: str) -> None:
        self._failures[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
