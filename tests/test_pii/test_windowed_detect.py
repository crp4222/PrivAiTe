from __future__ import annotations

import sys
import types

import pytest

from privaite.pii.detector_base import (
    hf_pipeline_extract,
    resolve_torch_device,
    windowed_ner_detect,
)


def _extract(result, chunk):
    """Minimal extract: result is a dict with label/score/start/end (chunk-relative)."""
    if result.get("skip"):
        return None
    start, end = result["start"], result["end"]
    return (result["label"], result["score"], start, end, chunk[start:end])


@pytest.mark.asyncio
async def test_maps_label_and_builds_entity():
    def predict(chunk):
        return [{"label": "M", "score": 0.9, "start": 0, "end": 4}]

    out = await windowed_ner_detect(
        "MARK rest",
        predict=predict,
        extract=_extract,
        label_mapping={"M": "PERSON"},
        score_threshold=0.5,
        source="test",
    )
    assert len(out) == 1
    e = out[0]
    assert (e.entity_type, e.text, e.start, e.end, e.source) == (
        "PERSON",
        "MARK",
        0,
        4,
        "test",
    )
    assert e.score == 0.9


@pytest.mark.asyncio
async def test_below_threshold_unmapped_and_none_are_skipped():
    def predict(chunk):
        return [
            {"label": "M", "score": 0.2, "start": 0, "end": 4},  # below threshold
            {"label": "UNKNOWN", "score": 0.9, "start": 0, "end": 4},  # unmapped
            {"skip": True},  # extract -> None
        ]

    out = await windowed_ner_detect(
        "MARK",
        predict=predict,
        extract=_extract,
        label_mapping={"M": "PERSON"},
        score_threshold=0.5,
        source="test",
    )
    assert out == []


@pytest.mark.asyncio
async def test_same_span_in_overlapping_windows_is_deduped():
    # Long enough to window (max_chars < len), no spaces so chunks are fixed-size;
    # "MARK" sits in the overlap of the first two 300-char windows (overlap 200),
    # so it is found twice and offset back to the same (150, 154) span -> one entity.
    text = "x" * 150 + "MARK" + "y" * 350

    def predict(chunk):
        i = chunk.find("MARK")
        return [{"label": "M", "score": 0.9, "start": i, "end": i + 4}] if i != -1 else []

    out = await windowed_ner_detect(
        text,
        predict=predict,
        extract=_extract,
        label_mapping={"M": "PERSON"},
        score_threshold=0.5,
        source="test",
        max_chars=300,
    )
    assert len(out) == 1
    assert (out[0].start, out[0].end, out[0].text) == (150, 154, "MARK")


def test_hf_pipeline_extract_uses_word_stripped_with_span_fallback():
    # word present: stripped and returned as span text.
    label, score, start, end, span = hf_pipeline_extract(
        {"entity_group": "PER", "score": 0.8, "start": 2, "end": 6, "word": " Bob "},
        "hi Bob!",
    )
    assert (label, score, start, end, span) == ("PER", 0.8, 2, 6, "Bob")

    # word absent: falls back to the raw chunk span.
    _, _, _, _, span2 = hf_pipeline_extract(
        {"entity_group": "PER", "score": 0.8, "start": 3, "end": 6}, "hi Bob!"
    )
    assert span2 == "Bob"


@pytest.mark.asyncio
async def test_mlmodel_detector_maps_and_labels_its_source():
    from privaite.config.schema import MLModelDetectorConfig
    from privaite.pii.detector_mlmodel import MLModelDetector

    detector = MLModelDetector(
        MLModelDetectorConfig(label_mapping={"PER": "PERSON"}, score_threshold=0.5)
    )
    detector._classifier = lambda chunk: [
        {"entity_group": "PER", "score": 0.9, "start": 3, "end": 6, "word": "Bob"}
    ]

    out = await detector.detect("hi Bob")

    assert len(out) == 1
    assert out[0].entity_type == "PERSON"
    assert out[0].source == "mlmodel"


@pytest.mark.asyncio
async def test_mlmodel_detector_requires_initialization():
    from privaite.config.schema import MLModelDetectorConfig
    from privaite.pii.detector_mlmodel import MLModelDetector

    with pytest.raises(RuntimeError, match="not initialized"):
        await MLModelDetector(MLModelDetectorConfig()).detect("text")


def _fake_torch(mps: bool, cuda: bool) -> types.ModuleType:
    torch = types.ModuleType("torch")
    torch.backends = types.SimpleNamespace(  # type: ignore[attr-defined]
        mps=types.SimpleNamespace(is_available=lambda: mps)
    )
    torch.cuda = types.SimpleNamespace(is_available=lambda: cuda)  # type: ignore[attr-defined]
    return torch


@pytest.mark.parametrize(
    "mps,cuda,expected",
    [(True, False, "mps"), (False, True, "cuda"), (False, False, "cpu")],
)
def test_resolve_torch_device_auto(monkeypatch, mps, cuda, expected):
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(mps, cuda))
    assert resolve_torch_device("auto") == expected


def test_resolve_torch_device_passes_explicit_through():
    assert resolve_torch_device("cpu") == "cpu"
