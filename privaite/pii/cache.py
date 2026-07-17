from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable

from privaite.pii.entity import PIIEntity

# One cached detection span: (start, end, entity_type, score, source).
# The matched value is deliberately NOT part of this record: callers rebuild
# the full PIIEntity against the live request text. Keeping values out of a
# structure that outlives the request is the point of this shape, not an
# optimization (see the threat model section of the README).
CachedSpan = tuple[int, int, str, float, str]


def spans_from_entities(entities: Iterable[PIIEntity]) -> tuple[CachedSpan, ...]:
    """Project merged entities down to value-free span metadata for storage."""
    return tuple((e.start, e.end, e.entity_type, e.score, e.source) for e in entities)


def entities_from_spans(spans: Iterable[CachedSpan], text: str) -> list[PIIEntity]:
    """Rebuild full entities against the LIVE request text.

    The cache key is derived from this exact text, so the stored offsets are
    valid by construction and ``text[start:end]`` reproduces the value the
    detectors originally matched.
    """
    return [
        PIIEntity(
            entity_type=etype,
            text=text[start:end],
            start=start,
            end=end,
            score=score,
            source=source,
        )
        for start, end, etype, score, source in spans
    ]


class DetectionCache:
    """Bounded LRU + TTL cache for merged PII detection results.

    What it stores, and what it refuses to store:

    - Keys are keyed BLAKE2b digests of (detector fingerprint, language, leaf
      text). The 16-byte key salt comes from ``os.urandom`` at construction,
      is never logged and never leaves the process, so the key set cannot be
      used as a precomputable index of known documents.
    - Values are span metadata only (offsets, entity types, scores, detector
      sources). No leaf text, no matched values, no anonymized output, no
      mapping state is ever stored here.

    Anonymized text and placeholder numbering are per-request mapping state
    and must never be cached; only the deterministic detection result is.

    Thread-safe: the engine is async, but both in-process integrations may
    drive it from several event loops or threads, so every read and write of
    the entry map happens under one lock.
    """

    def __init__(
        self,
        max_entries: int,
        ttl_seconds: float,
        *,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("detection cache max_entries must be >= 1")
        if ttl_seconds <= 0:
            raise ValueError("detection cache ttl_seconds must be > 0")
        self._max_entries = max_entries
        self._ttl = float(ttl_seconds)
        self._time = time_fn
        self._salt = os.urandom(16)
        self._lock = threading.Lock()
        self._entries: OrderedDict[bytes, tuple[float, tuple[CachedSpan, ...]]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def key(self, text: str, language: str, fingerprint: bytes) -> bytes:
        digest = hashlib.blake2b(key=self._salt, digest_size=32)
        digest.update(fingerprint)
        digest.update(b"\x00")
        digest.update(language.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(text.encode("utf-8"))
        return digest.digest()

    def get(self, key: bytes) -> tuple[CachedSpan, ...] | None:
        now = self._time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            stored_at, spans = entry
            if now - stored_at > self._ttl:
                del self._entries[key]
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return spans

    def put(self, key: bytes, spans: tuple[CachedSpan, ...]) -> None:
        now = self._time()
        with self._lock:
            self._entries[key] = (now, spans)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
