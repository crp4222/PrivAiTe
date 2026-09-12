"""Bounded ONNX prediction reuse during one scrub operation, never across users.

Keys hash the exact model inputs with a fresh salt. Values are token detection
labels and scores, not input token IDs, text, logits, offsets or reversible maps.
Offsets are always taken from the current text. Nested engine/gateway calls share
one scope; concurrent requests have separate scopes, including in worker threads.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine
from contextvars import ContextVar
from functools import wraps
from typing import Any, ParamSpec, TypeVar

TokenPredictions = tuple[tuple[int, ...], tuple[float, ...]]


class WindowCache:
    def __init__(self, max_windows: int = 128) -> None:
        if max_windows < 1:
            raise ValueError("max_windows must be positive")
        self._max_windows = max_windows
        self._salt = os.urandom(16)
        self._entries: OrderedDict[bytes, TokenPredictions] = OrderedDict()
        self._lock = threading.Lock()
        self._closed = False

    def key(self, session: object, inputs: list[tuple[str, tuple[int, ...], bytes]]) -> bytes:
        digest = hashlib.blake2b(key=self._salt, digest_size=32)
        digest.update(str(id(session)).encode("ascii"))
        for name, shape, raw in inputs:
            # Length framing avoids ambiguous concatenations; callers use int64.
            for part in (name.encode("utf-8"), repr(shape).encode("ascii"), raw):
                digest.update(len(part).to_bytes(8, "big"))
                digest.update(part)
        return digest.digest()

    def get(self, key: bytes) -> TokenPredictions | None:
        with self._lock:
            result = self._entries.get(key)
            if result is not None:
                self._entries.move_to_end(key)
            return result

    def put(self, key: bytes, predictions: TokenPredictions) -> None:
        with self._lock:
            # Cancelling an await does not stop asyncio.to_thread. A late worker
            # must not repopulate a cache after the request's finally block.
            if self._closed:
                return
            self._entries[key] = predictions
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_windows:
                self._entries.popitem(last=False)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._entries.clear()


_current: ContextVar[WindowCache | None] = ContextVar("privaite_window_cache", default=None)


def current_window_cache() -> WindowCache | None:
    return _current.get()


_P = ParamSpec("_P")
_R = TypeVar("_R")


def inference_request(call: Callable[_P, Awaitable[_R]]) -> Callable[_P, Coroutine[Any, Any, _R]]:
    """Share transient predictions until the outermost async scrub call finishes."""

    @wraps(call)
    async def scoped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        if current_window_cache() is not None:
            return await call(*args, **kwargs)
        cache = WindowCache()
        token = _current.set(cache)
        try:
            return await call(*args, **kwargs)
        finally:
            cache.close()
            _current.reset(token)

    return scoped
