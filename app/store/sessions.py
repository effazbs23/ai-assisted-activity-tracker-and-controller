"""Conversation context: recent turns, resolved entities and the pending action, per caller+session."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Session:
    messages: list[dict] = field(default_factory=list)
    entities: dict[str, dict] = field(default_factory=dict)
    pending_action_id: str | None = None
    touched: float = field(default_factory=time.time)


class SessionStore:
    def __init__(self, ttl: int, max_messages: int):
        self.ttl, self.max_messages = ttl, max_messages
        self._items: dict[tuple[str, str], Session] = {}
        self._lock = threading.Lock()

    def get(self, caller: str, session_id: str) -> Session:
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._items.items() if v.touched < now - self.ttl]:
                del self._items[k]
            s = self._items.setdefault((caller, session_id), Session())
            s.touched = now
            return s

    def add(self, session: Session, role: str, content: str) -> None:
        session.messages.append({"role": role, "content": content[:2000]})
        del session.messages[:-self.max_messages]
