"""Provider selection and windowed inference for the ONNX detector.

Two measured failure modes are pinned here. CoreML via device: "auto" covers
only 28 of 365 graph nodes, is slower than CPU at every input size, and leaks
compiled-model memory until the kernel kills the host process (fatal for the
in-process integrations). And a single full-sequence run costs quadratic time
and memory, while ``max_length`` used to silently forward everything past the
cap unscanned; long inputs are now covered whole by overlapping windows.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from privaite.config.schema import OnnxDetectorConfig
from privaite.pii.detector_onnx import (
    _WINDOW_OVERLAP,
    _WINDOW_TOKENS,
    ID2LABEL,
    OnnxPrivacyFilterDetector,
    _shared_run_length,
    _stitch_windows,
    decode_bioes_spans,
)


def _detector(**config_kwargs) -> OnnxPrivacyFilterDetector:
    return OnnxPrivacyFilterDetector(OnnxDetectorConfig(**config_kwargs))


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


def _fake_ort(available: list[str]) -> types.ModuleType:
    module = types.ModuleType("onnxruntime")
    module.get_available_providers = lambda: available  # type: ignore[attr-defined]
    return module


def test_auto_never_selects_coreml(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        _fake_ort(["CoreMLExecutionProvider", "CPUExecutionProvider"]),
    )
    assert _detector(device="auto")._get_providers() == ["CPUExecutionProvider"]


def test_auto_selects_cuda_when_available(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        _fake_ort(["CUDAExecutionProvider", "CPUExecutionProvider"]),
    )
    assert _detector(device="auto")._get_providers() == [
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_auto_prefers_cuda_even_with_coreml_present(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        _fake_ort(["CoreMLExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]),
    )
    assert _detector(device="auto")._get_providers() == [
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_auto_falls_back_to_cpu(monkeypatch):
    monkeypatch.setitem(sys.modules, "onnxruntime", _fake_ort(["CPUExecutionProvider"]))
    assert _detector(device="auto")._get_providers() == ["CPUExecutionProvider"]


@pytest.mark.parametrize(
    "device,expected",
    [
        ("cuda", ["CUDAExecutionProvider", "CPUExecutionProvider"]),
        ("coreml", ["CoreMLExecutionProvider", "CPUExecutionProvider"]),
        ("mps", ["CoreMLExecutionProvider", "CPUExecutionProvider"]),
        ("cpu", ["CPUExecutionProvider"]),
        ("something-unknown", ["CPUExecutionProvider"]),
    ],
)
def test_explicit_device_branches(device, expected):
    # Explicit coreml/mps stays a documented opt-in; unknown values fail safe
    # to CPU. None of these consult ort.get_available_providers().
    assert _detector(device=device)._get_providers() == expected


# ---------------------------------------------------------------------------
# Window geometry
# ---------------------------------------------------------------------------


def test_window_geometry_defaults_are_pinned():
    # 1024/128 is the measured setting (8192-token leaf: 154.7s full vs 6.9s
    # windowed, RSS bounded, span-identical on the captured corpus).
    assert (_WINDOW_TOKENS, _WINDOW_OVERLAP) == (1024, 128)
    assert _detector()._window_geometry() == (1024, 128)


def test_window_geometry_respects_smaller_max_length():
    # max_length now bounds the per-window sequence length, never coverage.
    assert _detector(max_length=512)._window_geometry() == (512, 64)
    assert _detector(max_length=16)._window_geometry() == (16, 2)


def test_window_geometry_caps_large_max_length():
    assert _detector(max_length=128000)._window_geometry() == (1024, 128)


# ---------------------------------------------------------------------------
# Window stitching
# ---------------------------------------------------------------------------


def _offs(start: int, count: int) -> list[tuple[int, int]]:
    return [(i, i + 1) for i in range(start, start + count)]


def test_shared_run_length_matches_expected_stride():
    prev, cur = _offs(0, 10), _offs(6, 10)
    assert _shared_run_length(prev, cur, 4) == 4


def test_shared_run_length_recovers_from_wrong_expected():
    # If the tokenizer ever produced a different overlap than requested, the
    # stitch must realign from the offsets instead of misaligning silently.
    prev, cur = _offs(0, 10), _offs(7, 10)  # actual shared run is 3
    assert _shared_run_length(prev, cur, 4) == 3


def test_shared_run_length_zero_when_disjoint():
    assert _shared_run_length(_offs(0, 5), _offs(10, 5), 4) == 0


def test_stitch_single_window_is_identity():
    row = (["O", "S-secret"], [0.9, 0.8], _offs(0, 2))
    assert _stitch_windows([row], 4) == row


def test_stitch_empty_rows():
    assert _stitch_windows([], 4) == ([], [], [])


def test_stitch_keeps_each_token_once_and_splits_overlap_at_midpoint():
    # Two windows of 10 tokens sharing 4: tokens 6..9. The first half of the
    # shared run (6, 7) must come from the earlier window, the second half
    # (8, 9) from the later one, where each has more surrounding context.
    row0 = (["a0"] * 10, [0.1] * 10, _offs(0, 10))
    row1 = (["b1"] * 10, [0.2] * 10, _offs(6, 10))
    labels, scores, offsets = _stitch_windows([row0, row1], 4)

    assert offsets == _offs(0, 16)  # every token exactly once, in order
    assert labels == ["a0"] * 8 + ["b1"] * 8
    assert scores == [0.1] * 8 + [0.2] * 8


def test_stitch_three_windows_full_coverage():
    rows = [
        (["a"] * 10, [0.1] * 10, _offs(0, 10)),
        (["b"] * 10, [0.2] * 10, _offs(6, 10)),
        (["c"] * 10, [0.3] * 10, _offs(12, 10)),
    ]
    labels, _, offsets = _stitch_windows(rows, 4)
    assert offsets == _offs(0, 22)
    assert labels == ["a"] * 8 + ["b"] * 6 + ["c"] * 8


def test_span_straddling_the_stitch_point_decodes_as_one_span():
    # A B-token kept from the earlier window and its E-token kept from the
    # later one must decode into a single merged span, not two fragments.
    text = "x" * 16
    row0 = (
        ["O"] * 6 + ["B-secret", "I-secret", "I-secret", "I-secret"],
        [0.9] * 10,
        _offs(0, 10),
    )
    row1 = (
        ["B-secret", "I-secret", "I-secret", "E-secret"] + ["O"] * 6,
        [0.9] * 10,
        _offs(6, 10),
    )
    labels, scores, offsets = _stitch_windows([row0, row1], 4)
    spans = decode_bioes_spans(labels, scores, offsets, text)
    assert [(s["start"], s["end"], s["entity_type"]) for s in spans] == [(6, 10, "secret")]


# ---------------------------------------------------------------------------
# Windowed _run_inference end to end (fake tokenizer + fake session)
# ---------------------------------------------------------------------------

_LABEL2ID = {label: idx for idx, label in ID2LABEL.items()}


class _CharTokenizer:
    """Character-level stand-in honoring the exact tokenizer contract that
    _run_inference relies on: truncation + stride overflow rows, offsets
    absolute into the original text, list-of-lists output."""

    def __call__(
        self,
        text: str,
        truncation: bool = True,
        max_length: int = 16,
        stride: int = 0,
        return_overflowing_tokens: bool = False,
        return_offsets_mapping: bool = False,
    ) -> dict:
        assert truncation and return_overflowing_tokens and return_offsets_mapping
        rows_ids: list[list[int]] = []
        rows_mask: list[list[int]] = []
        rows_off: list[list[tuple[int, int]]] = []
        start = 0
        while True:
            chunk = text[start : start + max_length]
            # One trailing zero-width token per row stands in for a special
            # (or padding) token; _run_window must filter it out.
            rows_ids.append([ord(ch) for ch in chunk] + [0])
            rows_mask.append([1] * (len(chunk) + 1))
            rows_off.append(
                [(start + i, start + i + 1) for i in range(len(chunk))]
                + [(start + len(chunk), start + len(chunk))]
            )
            if start + max_length >= len(text):
                break
            start += max_length - stride
        return {
            "input_ids": rows_ids,
            "attention_mask": rows_mask,
            "offset_mapping": rows_off,
        }


class _UppercaseRunSession:
    """Fake ONNX session labeling every uppercase run as a BIOES `secret`
    entity, from the window's own truncated point of view (a run cut by the
    window edge still gets B/E at the edge, like a real model would)."""

    def __init__(self, logit: float = 10.0) -> None:
        self._logit = logit

    def get_inputs(self):
        return [
            types.SimpleNamespace(name="input_ids"),
            types.SimpleNamespace(name="attention_mask"),
        ]

    def run(self, _output_names, feed):
        ids = feed["input_ids"][0]
        logits = np.full((1, len(ids), len(ID2LABEL)), -self._logit, dtype=np.float32)
        upper = [chr(int(t)).isupper() for t in ids]
        i = 0
        while i < len(ids):
            if not upper[i]:
                logits[0, i, _LABEL2ID["O"]] = self._logit
                i += 1
                continue
            j = i
            while j + 1 < len(ids) and upper[j + 1]:
                j += 1
            if i == j:
                logits[0, i, _LABEL2ID["S-secret"]] = self._logit
            else:
                logits[0, i, _LABEL2ID["B-secret"]] = self._logit
                logits[0, j, _LABEL2ID["E-secret"]] = self._logit
                for k in range(i + 1, j):
                    logits[0, k, _LABEL2ID["I-secret"]] = self._logit
            i = j + 1
        return [logits]


def _fake_onnx_detector(max_length: int) -> OnnxPrivacyFilterDetector:
    detector = _detector(
        max_length=max_length,
        label_mapping={"secret": "SECRET"},
        score_threshold=0.5,
    )
    detector._session = _UppercaseRunSession()
    detector._tokenizer = _CharTokenizer()
    return detector


@pytest.mark.asyncio
async def test_short_input_single_window_unchanged():
    detector = _fake_onnx_detector(max_length=64)
    text = "aaaa SECRET bbbb"
    entities = await detector.detect(text)
    assert [(e.start, e.end, e.entity_type, e.text) for e in entities] == [
        (5, 11, "SECRET", "SECRET")
    ]
    assert entities[0].source == "onnx"


@pytest.mark.asyncio
async def test_entity_straddling_a_window_boundary_is_one_span():
    # window 64, overlap 8 (64 // 8): windows cover [0, 64) and [56, ...).
    # The stitch midpoint is char 60; the entity sits at 57..62, fully inside
    # the shared run, so its tokens are taken from BOTH windows. It must come
    # back as exactly one span, not two fragments and not a duplicate.
    detector = _fake_onnx_detector(max_length=64)
    text = "a" * 57 + "SECRT" + "b" * 58
    entities = await detector.detect(text)
    assert [(e.start, e.end, e.text) for e in entities] == [(57, 62, "SECRT")]


@pytest.mark.asyncio
async def test_entity_longer_than_the_overlap_is_still_one_span():
    # No single window sees this run whole: the earlier window sees its start,
    # the later window its end. The global BIOES decode over the stitched
    # sequence must still merge it into one span.
    detector = _fake_onnx_detector(max_length=16)  # overlap 2
    text = "a" * 10 + "X" * 12 + "b" * 10
    entities = await detector.detect(text)
    assert [(e.start, e.end, e.text) for e in entities] == [(10, 22, "X" * 12)]


@pytest.mark.asyncio
async def test_entity_beyond_old_truncation_horizon_is_found():
    # With the old code, max_length truncated the encoding and everything past
    # it was forwarded unscanned. Now the tail is scanned: an entity far past
    # the per-window cap must be found, at absolute offsets.
    detector = _fake_onnx_detector(max_length=16)
    text = "a" * 100 + "SECRT" + "b" * 20
    entities = await detector.detect(text)
    assert [(e.start, e.end, e.text) for e in entities] == [(100, 105, "SECRT")]


@pytest.mark.asyncio
async def test_entity_duplicated_across_windows_is_not_duplicated():
    # An entity appearing once per window (outside any overlap) yields one
    # span per occurrence, and an occurrence inside an overlap yields one.
    detector = _fake_onnx_detector(max_length=16)  # step 14
    text = "aa" + "XY" + "c" * 24 + "ZW" + "d" * 10
    entities = await detector.detect(text)
    assert [(e.start, e.end, e.text) for e in entities] == [
        (2, 4, "XY"),
        (28, 30, "ZW"),
    ]


@pytest.mark.asyncio
async def test_empty_text_yields_no_entities():
    detector = _fake_onnx_detector(max_length=16)
    assert await detector.detect("") == []


@pytest.mark.asyncio
async def test_detect_requires_initialization():
    with pytest.raises(RuntimeError, match="not initialized"):
        await _detector().detect("text")


@pytest.mark.asyncio
async def test_score_threshold_and_label_mapping_still_apply():
    detector = _fake_onnx_detector(max_length=64)
    detector.config.label_mapping = {}  # unmapped type must be dropped
    assert await detector.detect("aaaa SECRET bbbb") == []


@pytest.mark.asyncio
async def test_below_threshold_span_is_dropped():
    detector = _fake_onnx_detector(max_length=64)
    # Weak logits: softmax confidence ~0.19, below the 0.5 threshold.
    detector._session = _UppercaseRunSession(logit=1.0)
    assert await detector.detect("aaaa SECRET bbbb") == []


@pytest.mark.asyncio
async def test_single_char_entity_uses_the_s_tag():
    detector = _fake_onnx_detector(max_length=64)
    entities = await detector.detect("aaa X bbb")
    assert [(e.start, e.end, e.text) for e in entities] == [(4, 5, "X")]


def test_decode_type_change_mid_span_starts_a_new_span():
    # An I-token whose type differs from the open span flushes the open span
    # and starts a new one of the new type.
    spans = decode_bioes_spans(
        ["B-secret", "I-private_email", "E-private_email"],
        [0.9, 0.9, 0.9],
        [(0, 1), (1, 2), (2, 3)],
        "abc",
    )
    assert [(s["start"], s["end"], s["entity_type"]) for s in spans] == [
        (0, 1, "secret"),
        (1, 3, "private_email"),
    ]


def test_name_property():
    assert _detector().name == "onnx"


@pytest.mark.asyncio
async def test_shutdown_clears_session_and_tokenizer():
    detector = _fake_onnx_detector(max_length=16)
    await detector.shutdown()
    assert detector._session is None
    assert detector._tokenizer is None


@pytest.mark.asyncio
async def test_initialize_creates_session_with_selected_providers(monkeypatch, tmp_path):
    # On a machine where CoreML is available, initialize() with device: "auto"
    # must hand the session CPU only, and load the tokenizer with
    # trust_remote_code left off.
    from privaite.pii import detector_onnx as module

    created: dict = {}

    fake_ort = _fake_ort(["CoreMLExecutionProvider", "CPUExecutionProvider"])
    fake_ort.SessionOptions = types.SimpleNamespace  # type: ignore[attr-defined]
    fake_ort.GraphOptimizationLevel = types.SimpleNamespace(  # type: ignore[attr-defined]
        ORT_ENABLE_ALL=99
    )

    def fake_session(path, sess_options=None, providers=None):
        created["providers"] = providers
        return "session"

    fake_ort.InferenceSession = fake_session  # type: ignore[attr-defined]

    fake_transformers = types.ModuleType("transformers")

    class _FakeAutoTokenizer:
        @staticmethod
        def from_pretrained(model_name, revision=None, trust_remote_code=False):
            created["tokenizer_args"] = (model_name, trust_remote_code)
            return "tokenizer"

    fake_transformers.AutoTokenizer = _FakeAutoTokenizer  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    monkeypatch.setattr(module, "download_onnx_model", lambda **kwargs: tmp_path / "model.onnx")

    detector = _detector(device="auto")
    await detector.initialize()

    assert created["providers"] == ["CPUExecutionProvider"]
    assert created["tokenizer_args"] == ("openai/privacy-filter", False)
    assert detector._session == "session"
    assert detector._tokenizer == "tokenizer"
