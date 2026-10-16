"""In-memory pending actions (two-step writes) with TTL, caller binding and idempotency."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class PendingAction:
    id: str
    caller: str
    kind: str
    payload: dict[str, Any]
    expires_at: float
    idempotency_key: str | None = None
    result: dict[str, Any] | None = None
    status: str = "pending"  # pending | done | cancelled


class ActionStore:
    def __init__(self, ttl: int):
        self.ttl = ttl
        self._items: dict[str, PendingAction] = {}
        self._idem: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def create(self, caller: str, kind: str, payload: dict, idempotency_key: str | None = None) -> PendingAction:
        with self._lock:
            if idempotency_key and (caller, idempotency_key) in self._idem:
                return self._items[self._idem[(caller, idempotency_key)]]
            action = PendingAction(uuid.uuid4().hex, caller, kind, payload, time.time() + self.ttl, idempotency_key)
            self._items[action.id] = action
            if idempotency_key:
                self._idem[(caller, idempotency_key)] = action.id
            return action

    def get(self, action_id: str, caller: str) -> PendingAction | None:
        with self._lock:
            a = self._items.get(action_id)
            if not a or a.caller != caller:
                return None
            if a.status == "pending" and a.expires_at < time.time():
                a.status = "cancelled"
            return a
