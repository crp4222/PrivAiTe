import importlib.util

import pytest

from privaite.config.schema import GlinerDetectorConfig
from privaite.pii.detector_gliner import GlinerDetector


class _FakeGlinerModel:
    """Stand-in for a loaded GLiNER model, so the wrapper logic (label mapping,
    offsets, threshold, dedupe) is tested without torch or the gliner package."""

    def __init__(self, results: list[dict]) -> None:
        self._results = results

    def predict_entities(self, text: str, labels: list[str], threshold: float = 0.5) -> list[dict]:
        return [dict(r) for r in self._results]


@pytest.mark.asyncio
async def test_detect_maps_labels_offsets_and_filters_threshold():
    text = "Marie Dupont wrote to bob@acme.com"
    detector = GlinerDetector(GlinerDetectorConfig(enabled=True, score_threshold=0.5))
    detector._model = _FakeGlinerModel([
        {"label": "person", "start": 0, "end": 12, "score": 0.95},
        {"label": "email", "start": 22, "end": 34, "score": 0.9},
        {"label": "person", "start": 0, "end": 5, "score": 0.2},   # below threshold -> dropped
        {"label": "job title", "start": 0, "end": 4, "score": 0.9},  # unmapped label -> dropped
    ])

    entities = await detector.detect(text, "en")

    by_type = {(e.entity_type, e.text) for e in entities}
    assert ("PERSON", "Marie Dupont") in by_type
    assert ("EMAIL_ADDRESS", "bob@acme.com") in by_type
    assert len(entities) == 2  # low-score and unmapped were filtered
    for e in entities:
        assert text[e.start:e.end] == e.text  # offsets point at the real substring
        assert e.source == "gliner"


@pytest.mark.asyncio
async def test_detect_requires_initialization():
    detector = GlinerDetector(GlinerDetectorConfig(enabled=True))
    with pytest.raises(RuntimeError, match="not initialized"):
        await detector.detect("some text", "en")


@pytest.mark.asyncio
@pytest.mark.skipif(
    importlib.util.find_spec("gliner") is not None,
    reason="gliner installed: initialize would load the real model instead of failing",
)
async def test_initialize_without_gliner_fails_closed_with_hint():
    detector = GlinerDetector(GlinerDetectorConfig(enabled=True))
    with pytest.raises(RuntimeError, match=r"privaite\[gliner\]"):
        await detector.initialize()
