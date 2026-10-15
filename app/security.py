from __future__ import annotations

import collections
import json
import threading
import time

from fastapi import Depends, HTTPException, Request


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def check(self, caller: str) -> None:
        now = time.time()
        with self._lock:
            q = self._hits[caller]
            while q and q[0] < now - 60:
                q.popleft()
            if len(q) >= self.per_minute:
                raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": "60"})
            q.append(now)


class AuditLog:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def write(self, caller: str, event: str, **data) -> None:
        line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "caller": caller, "event": event, **data}, default=str)
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def current_caller(request: Request) -> str:
    """Authenticate via X-API-Key and apply the per-caller rate limit."""
    app_state = request.app.state
    key = request.headers.get("X-API-Key", "")
    caller = app_state.settings.api_keys.get(key)
    if not caller:
        raise HTTPException(401, "Invalid or missing API key")
    app_state.limiter.check(caller)
    return caller


Caller = Depends(current_caller)
