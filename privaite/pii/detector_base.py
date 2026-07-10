from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from privaite.pii.entity import PIIEntity


class PIIDetector(ABC):
    @abstractmethod
    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]: ...

    @abstractmethod
    async def initialize(self) -> None: ...

    async def shutdown(self) -> None:
        pass

    @property
    @abstractmethod
    def name(self) -> str: ...


def chunk_text(text: str, max_chars: int = 1500, overlap: int = 200) -> list[tuple[int, str]]:
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


# One raw model result parsed to (label, score, chunk_start, chunk_end, span_text),
# or None to skip it. Positions are relative to the chunk; the loop offsets them.
NerResult = tuple[str, float, int, int, str]


async def windowed_ner_detect(
    text: str,
    *,
    predict: Callable[[str], list[Any]],
    extract: Callable[[Any, str], NerResult | None],
    label_mapping: dict[str, str],
    score_threshold: float,
    source: str,
    max_chars: int = 1500,
) -> list[PIIEntity]:
    """Shared windowed detection loop for the ML NER detectors (BERT, GLiNER, the
    HF pipeline). Each has the same shape: window the text so a model that truncates
    long inputs still sees all of it, run the model per window, then threshold, map
    the label, offset the span back to the full text, dedupe on (start, end, type),
    and build a PIIEntity. ``predict(chunk)`` runs the model on one window (offloaded
    to a thread); ``extract(result, chunk)`` turns one raw result into
    (label, score, start, end, span_text) or None to skip it."""
    entities: list[PIIEntity] = []
    seen: set[tuple[int, int, str]] = set()
    for offset, chunk in chunk_text(text, max_chars=max_chars):
        results = await asyncio.to_thread(predict, chunk)
        for result in results:
            parsed = extract(result, chunk)
            if parsed is None:
                continue
            label, score, start, end, span_text = parsed
            if score < score_threshold:
                continue
            mapped_type = label_mapping.get(label)
            if not mapped_type:
                continue
            start += offset
            end += offset
            key = (start, end, mapped_type)
            if key in seen:
                continue
            seen.add(key)
            entities.append(
                PIIEntity(
                    entity_type=mapped_type,
                    text=span_text,
                    start=start,
                    end=end,
                    score=float(score),
                    source=source,
                )
            )
    return entities


def hf_pipeline_extract(result: dict, chunk: str) -> NerResult:
    """Parse one Hugging Face token-classification result (aggregation_strategy
    "simple"): entity_group/score/start/end, word stripped, falling back to the raw
    chunk span. Shared by the BERT and generic ML pipeline detectors."""
    start = result.get("start", 0)
    end = result.get("end", 0)
    word = result.get("word", chunk[start:end])
    return (
        result.get("entity_group", ""),
        result.get("score", 0.0),
        start,
        end,
        word.strip(),
    )


def resolve_torch_device(device_str: str) -> str:
    """Map "auto" to the best available torch device (mps, then cuda, else cpu) and
    pass any explicit device string through. Shared by the torch ``.to(device)``
    detectors (GLiNER, the ML model); the HF-pipeline device format differs and
    stays local to that detector."""
    if device_str == "auto":
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
        return "cpu"
    return device_str


class HFPipelineDetector(PIIDetector):
    """Shared ``detect()`` for the Hugging Face token-classification detectors (BERT
    NER and the generic ML pipeline). Both build a ``self._classifier`` pipeline in
    initialize() and differ only in how that pipeline is loaded, so detection itself
    (window, run, threshold, map, offset, dedupe) lives here once. ``source`` is the
    detector's own name."""

    config: Any
    _classifier: Any

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        if self._classifier is None:
            raise RuntimeError(f"{type(self).__name__} not initialized")
        return await windowed_ner_detect(
            text,
            predict=self._classifier,
            extract=hf_pipeline_extract,
            label_mapping=self.config.label_mapping,
            score_threshold=self.config.score_threshold,
            source=self.name,
        )
