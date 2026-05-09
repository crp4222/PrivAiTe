from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class SessionStats:
    pii_count: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    request_count: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    @property
    def total_pii(self) -> int:
        return sum(self.pii_count.values())


class PIITracker:
    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._sessions: dict[str, SessionStats] = {}
        self._lock = Lock()
        self._ttl = ttl_seconds

    def record(self, session_id: str, entity_types: dict[str, int]) -> SessionStats:
        with self._lock:
            self._evict_expired()

            if session_id not in self._sessions:
                self._sessions[session_id] = SessionStats()

            stats = self._sessions[session_id]
            stats.request_count += 1
            stats.last_seen = time.time()
            for entity_type, count in entity_types.items():
                stats.pii_count[entity_type] += count

            return stats

    def get(self, session_id: str) -> SessionStats | None:
        with self._lock:
            return self._sessions.get(session_id)

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [
            sid for sid, s in self._sessions.items()
            if now - s.last_seen > self._ttl
        ]
        for sid in expired:
            del self._sessions[sid]
