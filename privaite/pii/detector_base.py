from __future__ import annotations

from abc import ABC, abstractmethod

from privaite.pii.entity import PIIEntity


class PIIDetector(ABC):
    @abstractmethod
    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        ...

    @abstractmethod
    async def initialize(self) -> None:
        ...

    async def shutdown(self) -> None:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        ...


def chunk_text(
    text: str, max_chars: int = 1500, overlap: int = 200
) -> list[tuple[int, str]]:
    """Split ``text`` into (offset, chunk) windows for detectors whose models
    silently truncate long inputs (the HF token-classification pipeline caps at
    model_max_length, so PII past that point was simply invisible). Chunks
    overlap so an entity sitting on a boundary is fully inside at least one
    window; callers dedupe on (start, end, type) after offsetting. Cuts prefer
    whitespace so words are not split mid-entity more than necessary."""
    if len(text) <= max_chars:
        return [(0, text)]

    chunks: list[tuple[int, str]] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            cut = text.rfind(" ", start + max_chars - overlap, end)
            if cut > start:
                end = cut
        chunks.append((start, text[start:end]))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks
