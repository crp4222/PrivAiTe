"""The PrivAiTe MiniLM detector: the window geometry it was trained with (256
tokens framed by its special tokens, 64 shared), coverage of every token,
decoding and label mapping, the Core ML backend's guards and file layout, and
model files read from the cache without asking the Hub when the revision is
pinned. A fake tokenizer and fake logits stand in for the model."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from privaite.config.schema import MiniLMDetectorConfig, PIIConfig
from privaite.pii import detector_minilm
from privaite.pii.detector_minilm import (
    WINDOW_OVERLAP,
    WINDOW_TOKENS,
    MiniLMDetector,
    download_minilm_files,
    materialize_package,
)
from privaite.pii.engine import PIIEngine, PIIProcessingError

PINNED = "0123456789abcdef0123456789abcdef01234567"
LABELS = {0: "O", 1: "B-PERSON", 2: "I-PERSON", 3: "B-SECRET", 4: "I-SECRET", 5: "B-CONTACT"}
CLS, PAD, SEP = 900, 901, 902


class _WordTokenizer:
    """One token per whitespace-separated word, offsets into the text."""

    cls_token_id, pad_token_id, sep_token_id = CLS, PAD, SEP

    def __init__(self) -> None:
        self.vocab: dict[str, int] = {}
        self.words: dict[int, str] = {}

    def __call__(
        self, text, add_special_tokens=True, truncation=True, return_offsets_mapping=False
    ):
        assert add_special_tokens is False and truncation is False
        ids, offsets, pos = [], [], 0
        for word in text.split():
            start = text.index(word, pos)
            pos = start + len(word)
            token = self.vocab.setdefault(word, len(self.vocab) + 1)
            self.words[token] = word
            ids.append(token)
            offsets.append((start, pos))
        return {"input_ids": ids, "offset_mapping": offsets}


class _RuleLogits:
    """Logits per token from the word: "Name*" is a person (Name2 continues
    one), "KEY*" a secret, "/path" a contact; everything else outside."""

    def __init__(self, tokenizer: _WordTokenizer) -> None:
        self.tokenizer = tokenizer
        self.calls: list[list[int]] = []

    def __call__(self, ids: list[int]) -> np.ndarray:
        self.calls.append(list(ids))
        out = np.zeros((len(ids), len(LABELS)), dtype=np.float32)
        for i, token in enumerate(ids):
            word = self.tokenizer.words.get(token, "")
            label = 0
            if word.startswith("Name2"):
                label = 2
            elif word.startswith("Name"):
                label = 1
            elif word.startswith("KEY"):
                label = 3
            elif word.startswith("/"):
                label = 5
            out[i, label] = 10.0
        return out


def _detector(**config) -> tuple[MiniLMDetector, _RuleLogits]:
    detector = MiniLMDetector(MiniLMDetectorConfig(enabled=True, **config))
    tokenizer = _WordTokenizer()
    logits = _RuleLogits(tokenizer)
    detector._tokenizer = tokenizer
    detector._id2label = dict(LABELS)
    detector._logits = logits
    detector._runner = object()
    return detector, logits


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def test_window_geometry_is_the_trained_one():
    assert (WINDOW_TOKENS, WINDOW_OVERLAP) == (256, 64)


@pytest.mark.asyncio
async def test_each_window_is_framed_by_the_special_tokens_and_fits_the_model():
    detector, logits = _detector()
    text = " ".join(f"w{i}" for i in range(1000))

    await detector.detect(text)

    assert len(logits.calls) > 1
    for call in logits.calls:
        assert call[0] == CLS and call[-1] == SEP
        assert len(call) <= WINDOW_TOKENS


@pytest.mark.asyncio
async def test_every_token_is_scanned_and_windows_share_the_overlap():
    detector, logits = _detector()
    text = " ".join(f"w{i}" for i in range(1000))

    await detector.detect(text)

    contents = [call[1:-1] for call in logits.calls]
    covered = [token for content in contents for token in content]
    assert set(covered) == set(detector._tokenizer.vocab.values())
    for left, right in zip(contents, contents[1:], strict=False):
        assert left[-WINDOW_OVERLAP:] == right[:WINDOW_OVERLAP]


@pytest.mark.asyncio
async def test_a_name_across_a_window_edge_is_one_span():
    detector, _ = _detector()
    words = [f"w{i}" for i in range(400)]
    words[253:255] = ["NameA", "Name2B"]  # the first window holds 254 tokens
    text = " ".join(words)

    entities = await detector.detect(text)

    assert [(e.text, e.entity_type) for e in entities] == [("NameA Name2B", "PERSON")]


@pytest.mark.asyncio
async def test_a_name_in_the_overlap_is_reported_once():
    detector, _ = _detector()
    words = [f"w{i}" for i in range(400)]
    words[220] = "NameX"  # inside the 64 tokens both windows see
    text = " ".join(words)

    entities = await detector.detect(text)

    assert [e.text for e in entities] == ["NameX"]


@pytest.mark.asyncio
async def test_empty_and_blank_text_yield_nothing():
    detector, logits = _detector()

    assert await detector.detect("") == []
    assert await detector.detect("   \n ") == []
    assert logits.calls == []


@pytest.mark.asyncio
async def test_an_encoding_with_missing_offsets_is_refused():
    detector, _ = _detector()

    class Broken(_WordTokenizer):
        def __call__(self, text, **kwargs):
            encoding = super().__call__(text, **kwargs)
            encoding["offset_mapping"] = encoding["offset_mapping"][:-1]
            return encoding

    detector._tokenizer = Broken()
    with pytest.raises(RuntimeError, match="Inconsistent MiniLM"):
        await detector.detect("a b c")


@pytest.mark.asyncio
async def test_text_the_tokenizer_skipped_blocks_instead_of_passing_unscanned():
    detector, _ = _detector()

    class Skipping(_WordTokenizer):
        def __call__(self, text, **kwargs):
            encoding = super().__call__(text, **kwargs)
            return {key: rows[:-1] for key, rows in encoding.items()}

    detector._tokenizer = Skipping()
    with pytest.raises(RuntimeError, match="coverage"):
        await detector.detect("hello NameA secret")


@pytest.mark.asyncio
async def test_identical_windows_are_computed_once_per_scrub():
    detector, logits = _detector()
    block = " ".join(f"w{i}" for i in range(254))

    await detector.detect(block)
    await detector.detect(block)
    calls_without_scope = len(logits.calls)

    from privaite.pii.window_cache import inference_request

    @inference_request
    async def scrub() -> None:
        await detector.detect(block)
        await detector.detect(block)

    logits.calls.clear()
    await scrub()

    assert calls_without_scope == 2
    assert len(logits.calls) == 1


@pytest.mark.asyncio
async def test_detect_requires_initialization():
    with pytest.raises(RuntimeError, match="not initialized"):
        await MiniLMDetector(MiniLMDetectorConfig()).detect("text")


# ---------------------------------------------------------------------------
# Decoding and mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_types_follow_the_label_mapping_and_unmapped_types_are_dropped():
    detector, _ = _detector(label_mapping={"PERSON": "PERSON"})

    entities = await detector.detect("NameA sent KEY123 to /home/x")

    assert [(e.text, e.entity_type, e.source) for e in entities] == [("NameA", "PERSON", "minilm")]


@pytest.mark.asyncio
async def test_default_mapping_uses_privaite_names_where_they_exist():
    mapping = MiniLMDetectorConfig().label_mapping
    assert mapping["ADDRESS"] == "LOCATION"
    assert mapping["DATE"] == mapping["DATE_OF_BIRTH"] == "DATE_TIME"
    assert mapping["PERSON"] == "PERSON" and mapping["SECRET"] == "SECRET"
    assert len(mapping) == 15


@pytest.mark.asyncio
async def test_a_contact_or_secret_keeps_its_leading_separator():
    """A path or a secret may start with a slash: snapping must not trim it."""
    detector, _ = _detector()

    entities = await detector.detect("go to /home/marie now")

    assert [e.text for e in entities] == ["/home/marie"]


@pytest.mark.asyncio
async def test_score_threshold_drops_unsure_spans():
    detector, logits = _detector(score_threshold=0.99)
    original = logits.__call__

    def unsure(ids):
        out = original(ids)
        out[out == 10.0] = 1.0  # the right label, but far from certain
        return out

    detector._logits = unsure
    assert await detector.detect("NameA") == []
    detector.config.score_threshold = 0.0
    assert [e.text for e in await detector.detect("NameA")] == ["NameA"]


def test_default_threshold_is_the_evaluated_argmax():
    assert MiniLMDetectorConfig().score_threshold == 0.0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_built_in_model_is_pinned_to_a_commit():
    config = MiniLMDetectorConfig()
    assert config.model_name == "crp4222/privaite-pii-minilm"
    assert config.revision is not None and len(config.revision) == 40
    assert config.backend == "onnx"
    assert config.enabled is False


def test_unknown_backend_is_refused_at_load():
    with pytest.raises(ValidationError):
        MiniLMDetectorConfig(backend="tensorrt")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _fake_loading(monkeypatch, tmp_path, *, cached: bool = True) -> dict:
    """onnxruntime, transformers and the hub, faked: initialize() runs offline."""
    seen: dict = {"tokenizer": [], "downloads": [], "sessions": []}
    config = tmp_path / "config.json"
    config.write_text('{"id2label": {"0": "O", "1": "B-PERSON", "2": "I-PERSON"}}')
    model = tmp_path / "model.onnx"
    model.write_bytes(b"onnx")

    transformers = types.ModuleType("transformers")

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(name, **kwargs):
            seen["tokenizer"].append(kwargs)
            if kwargs.get("local_files_only") and not cached:
                raise OSError("not cached")
            return _WordTokenizer()

    transformers.AutoTokenizer = AutoTokenizer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", transformers)

    ort = types.ModuleType("onnxruntime")
    ort.SessionOptions = types.SimpleNamespace  # type: ignore[attr-defined]
    ort.GraphOptimizationLevel = types.SimpleNamespace(ORT_ENABLE_ALL=99)  # type: ignore[attr-defined]

    class Session:
        def __init__(self, path, sess_options=None, providers=None):
            seen["sessions"].append((path, providers))

        def run(self, outputs, feed):
            return [np.zeros((1, feed["input_ids"].shape[1], 3), dtype=np.float32)]

    ort.InferenceSession = Session  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)

    def files(repo, filenames, cache_dir=None, revision=None):
        seen["downloads"].append(filenames)
        return [config if name == "config.json" else model for name in filenames]

    monkeypatch.setattr(detector_minilm, "download_minilm_files", files)
    return seen


@pytest.mark.asyncio
async def test_onnx_backends_load_their_own_file_on_cpu(monkeypatch, tmp_path):
    for backend, filename in (("onnx", "onnx/model.onnx"), ("onnx_int8", "onnx/model_int8.onnx")):
        seen = _fake_loading(monkeypatch, tmp_path)
        detector = MiniLMDetector(MiniLMDetectorConfig(enabled=True, backend=backend))

        await detector.initialize()

        assert seen["downloads"][-1] == (filename,)
        assert seen["sessions"][0][1] == ["CPUExecutionProvider"]
        assert detector._id2label == {0: "O", 1: "B-PERSON", 2: "I-PERSON"}


@pytest.mark.asyncio
async def test_a_cached_tokenizer_is_read_without_asking_the_hub(monkeypatch, tmp_path):
    seen = _fake_loading(monkeypatch, tmp_path, cached=True)

    await MiniLMDetector(MiniLMDetectorConfig(enabled=True)).initialize()

    assert [c.get("local_files_only", False) for c in seen["tokenizer"]] == [True]


@pytest.mark.asyncio
async def test_a_tokenizer_missing_from_the_cache_is_downloaded(monkeypatch, tmp_path):
    seen = _fake_loading(monkeypatch, tmp_path, cached=False)

    await MiniLMDetector(MiniLMDetectorConfig(enabled=True)).initialize()

    assert [c.get("local_files_only", False) for c in seen["tokenizer"]] == [True, False]


@pytest.mark.asyncio
async def test_a_moving_revision_always_asks_the_hub(monkeypatch, tmp_path):
    seen = _fake_loading(monkeypatch, tmp_path, cached=True)

    await MiniLMDetector(MiniLMDetectorConfig(enabled=True, revision="main")).initialize()

    assert [c.get("local_files_only", False) for c in seen["tokenizer"]] == [False]


@pytest.mark.asyncio
async def test_the_long_text_tokenizer_limit_is_lifted(monkeypatch, tmp_path):
    _fake_loading(monkeypatch, tmp_path)
    detector = MiniLMDetector(MiniLMDetectorConfig(enabled=True))

    await detector.initialize()

    assert detector._tokenizer.model_max_length == sys.maxsize


@pytest.mark.asyncio
async def test_a_model_that_cannot_predict_fails_at_startup(monkeypatch, tmp_path):
    _fake_loading(monkeypatch, tmp_path)

    class Failing:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, outputs, feed):
            raise RuntimeError("model not compiled")

    sys.modules["onnxruntime"].InferenceSession = Failing  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="not compiled"):
        await MiniLMDetector(MiniLMDetectorConfig(enabled=True)).initialize()


@pytest.mark.asyncio
async def test_coreml_is_refused_off_macos(monkeypatch, tmp_path):
    _fake_loading(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(RuntimeError, match="needs macOS"):
        await MiniLMDetector(MiniLMDetectorConfig(enabled=True, backend="coreml")).initialize()


@pytest.mark.asyncio
async def test_coreml_without_coremltools_names_the_extra(monkeypatch, tmp_path):
    _fake_loading(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "coremltools", None)
    monkeypatch.setattr(detector_minilm, "materialize_package", lambda files, package: tmp_path)

    with pytest.raises(RuntimeError, match=r"privaite\[ane\]"):
        await MiniLMDetector(MiniLMDetectorConfig(enabled=True, backend="coreml")).initialize()


@pytest.mark.asyncio
async def test_coreml_pads_each_window_to_the_fixed_input_and_masks_the_padding(
    monkeypatch, tmp_path
):
    _fake_loading(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(detector_minilm, "materialize_package", lambda files, package: tmp_path)
    feeds: list[dict] = []

    class MLModel:
        def __init__(self, path, compute_units=None):
            self.units = compute_units

        def predict(self, feed):
            feeds.append(feed)
            return {"logits": np.zeros((1, WINDOW_TOKENS, 3), dtype=np.float32)}

    coremltools = types.ModuleType("coremltools")
    coremltools.models = types.SimpleNamespace(MLModel=MLModel)  # type: ignore[attr-defined]
    coremltools.ComputeUnit = types.SimpleNamespace(CPU_AND_NE="ane")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "coremltools", coremltools)
    detector = MiniLMDetector(MiniLMDetectorConfig(enabled=True, backend="coreml"))

    await detector.initialize()
    await detector.detect("one two three")

    feed = feeds[-1]
    assert feed["input_ids"].shape == feed["attention_mask"].shape == (1, WINDOW_TOKENS)
    assert feed["input_ids"][0, 0] == CLS and feed["input_ids"][0, 4] == SEP
    assert feed["attention_mask"][0].tolist() == [1] * 5 + [0] * (WINDOW_TOKENS - 5)
    assert set(feed["input_ids"][0, 5:].tolist()) == {PAD}
    assert detector._runner.units == "ane"


# ---------------------------------------------------------------------------
# Model files
# ---------------------------------------------------------------------------


def _hub(monkeypatch, cache: dict[str, str]) -> list[str]:
    import huggingface_hub

    downloaded: list[str] = []

    def lookup(repo_id, filename, cache_dir=None, revision=None):
        return cache.get(filename)

    def download(repo_id, filename, cache_dir=None, revision=None):
        downloaded.append(filename)
        return str(Path(cache_dir) / Path(filename).name)

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", lookup)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    return downloaded


def test_pinned_files_in_the_cache_are_read_without_asking_the_hub(monkeypatch, tmp_path):
    downloaded = _hub(monkeypatch, {"onnx/model.onnx": str(tmp_path / "model.onnx")})

    paths = download_minilm_files("repo", ("onnx/model.onnx",), str(tmp_path), PINNED)

    assert paths == [tmp_path / "model.onnx"] and downloaded == []


def test_pinned_files_missing_from_the_cache_are_downloaded(monkeypatch, tmp_path):
    downloaded = _hub(monkeypatch, {})

    download_minilm_files("repo", ("config.json", "onnx/model.onnx"), str(tmp_path), PINNED)

    assert downloaded == ["config.json", "onnx/model.onnx"]


def test_a_moving_revision_downloads_even_when_cached(monkeypatch, tmp_path):
    downloaded = _hub(monkeypatch, {"onnx/model.onnx": str(tmp_path / "model.onnx")})

    download_minilm_files("repo", ("onnx/model.onnx",), str(tmp_path), "main")

    assert downloaded == ["onnx/model.onnx"]


def _hub_snapshot(tmp_path: Path) -> list[Path]:
    """The Core ML package as the hub cache lays it out: symlinks to blobs."""
    repo = tmp_path / "models--x--y"
    blobs = repo / "blobs"
    blobs.mkdir(parents=True)
    package = repo / "snapshots" / PINNED / "coreml" / "pilot_ane_256.mlpackage"
    files = []
    for relative, content in (
        ("Manifest.json", b"{}"),
        ("Data/com.apple.CoreML/model.mlmodel", b"spec"),
        ("Data/com.apple.CoreML/weights/weight.bin", b"weights"),
    ):
        blob = blobs / f"blob-{len(files)}"
        blob.write_bytes(content)
        link = package / relative
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(blob)
        files.append(link)
    return files


def test_the_coreml_package_is_rebuilt_from_real_files_named_after_the_commit(tmp_path):
    files = _hub_snapshot(tmp_path)

    target = materialize_package(files, "coreml/pilot_ane_256.mlpackage")

    assert (
        target == tmp_path / "models--x--y" / "privaite-coreml" / PINNED / "pilot_ane_256.mlpackage"
    )
    weights = target / "Data/com.apple.CoreML/weights/weight.bin"
    assert weights.read_bytes() == b"weights" and not weights.is_symlink()
    assert (target / "Manifest.json").read_bytes() == b"{}"
    assert not list(target.rglob(".*.partial"))


def test_rebuilding_the_package_again_reuses_it(tmp_path):
    files = _hub_snapshot(tmp_path)
    first = materialize_package(files, "coreml/pilot_ane_256.mlpackage")
    stamp = (first / "Manifest.json").stat().st_ino

    again = materialize_package(files, "coreml/pilot_ane_256.mlpackage")

    assert again == first and (again / "Manifest.json").stat().st_ino == stamp


def test_a_package_outside_the_hub_cache_is_read_in_place(tmp_path):
    package = tmp_path / "local" / "coreml" / "pilot_ane_256.mlpackage"
    (package / "Data").mkdir(parents=True)
    manifest = package / "Manifest.json"
    manifest.write_bytes(b"{}")

    assert materialize_package([manifest], "coreml/pilot_ane_256.mlpackage") == package


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class _Fixed:
    """A detector returning fixed entities, standing in for a loaded model."""

    def __init__(self, name, entities):
        self._name, self._entities = name, entities

    @property
    def name(self):
        return self._name

    async def detect(self, text, language="en"):
        return list(self._entities)


@pytest.mark.asyncio
async def test_a_blocked_type_still_blocks_when_the_model_names_the_span_otherwise():
    """MiniLM calls an email CONTACT, with a higher score than Presidio's
    EMAIL_ADDRESS. A blocked type wins the overlap, so the gate still fires."""
    from privaite.pii.engine import PIIBlockedError
    from privaite.pii.entity import PIIEntity

    text = "write to jane@example.com"
    engine = PIIEngine(PIIConfig(preset=None, block_entities=["EMAIL_ADDRESS"]))
    engine.detectors = [
        _Fixed("minilm", [PIIEntity("CONTACT", "jane@example.com", 9, 25, 0.999, "minilm")]),
        _Fixed(
            "presidio", [PIIEntity("EMAIL_ADDRESS", "jane@example.com", 9, 25, 0.9, "presidio")]
        ),
    ]
    engine._ready = True

    with pytest.raises(PIIBlockedError):
        await engine.process_request([{"role": "user", "content": text}])


def test_block_entities_may_name_the_model_types():
    engine = PIIEngine(PIIConfig(preset="minilm", block_entities=["GOV_ID", "CONTACT"]))
    engine._validate_block_entities()


def test_block_entities_the_preset_cannot_produce_are_refused():
    engine = PIIEngine(PIIConfig(preset="minilm", block_entities=["MEDICAL_LICENSE"]))
    with pytest.raises(ValueError, match="MEDICAL_LICENSE"):
        engine._validate_block_entities()


@pytest.mark.asyncio
async def test_the_engine_starts_the_detector_with_the_preset(monkeypatch):
    started: list[str] = []

    async def fake_initialize(self):
        started.append(self.config.backend)

    monkeypatch.setattr(MiniLMDetector, "initialize", fake_initialize)
    config = PIIConfig(preset="minilm")
    config.detectors.presidio.enabled = False
    engine = PIIEngine(config)

    await engine.initialize()

    assert [d.name for d in engine.detectors] == ["minilm"]
    assert started == ["onnx"]


@pytest.mark.asyncio
async def test_a_failing_model_blocks_the_request(monkeypatch):
    detector, _ = _detector()

    def broken(ids):
        raise RuntimeError("ANE unavailable")

    detector._logits = broken
    config = PIIConfig(preset="minilm")
    config.detectors.presidio.enabled = False
    engine = PIIEngine(config)
    engine.detectors = [detector]
    engine._ready = True

    with pytest.raises(PIIProcessingError):
        await engine.process_request([{"role": "user", "content": "hello NameA"}])


# ---------------------------------------------------------------------------
# Characters the tokenizer drops
# ---------------------------------------------------------------------------


class _DroppingTokenizer(_WordTokenizer):
    """Drops zero-width spaces and combining marks from its offsets, as the
    XLM-R tokenizer does (a decomposed "é" is tokenized as "e")."""

    DROPPED = ("​", "́", "﻿")

    def __call__(self, text, **kwargs):
        cleaned = "".join(" " if ch in self.DROPPED else ch for ch in text)
        encoding = super().__call__(cleaned, **kwargs)
        return encoding


@pytest.mark.parametrize("char", ["​", "‍", "‎", "﻿", "́", "⃝", "▁", "�"])
def test_characters_the_tokenizer_drops_are_harmless(char):
    assert detector_minilm.unscanned_is_harmless(char)


@pytest.mark.parametrize("char", ["a", "7", "@", "€", "漢", "\u0000"])
def test_any_other_unscanned_character_is_not(char):
    assert not detector_minilm.unscanned_is_harmless(char)


@pytest.mark.asyncio
async def test_a_zero_width_space_does_not_block_the_request():
    detector, _ = _detector()
    detector._tokenizer = _DroppingTokenizer()
    detector._logits = _RuleLogits(detector._tokenizer)

    entities = await detector.detect("﻿hello​ NameA ​there")

    assert [e.text for e in entities] == ["NameA"]


@pytest.mark.asyncio
async def test_a_decomposed_accent_stays_masked_with_its_name():
    """macOS writes file names decomposed: "José" arrives as "Jose" + U+0301."""
    detector, _ = _detector()
    detector._tokenizer = _DroppingTokenizer()
    detector._logits = _RuleLogits(detector._tokenizer)

    entities = await detector.detect("call NameJosé now")

    assert [e.text for e in entities] == ["NameJosé"]


@pytest.mark.asyncio
async def test_an_unscanned_letter_still_blocks():
    detector, _ = _detector()

    class Skipping(_WordTokenizer):
        def __call__(self, text, **kwargs):
            return super().__call__(text.replace("x", " "), **kwargs)

    detector._tokenizer = Skipping()
    with pytest.raises(RuntimeError, match="coverage"):
        await detector.detect("abc xyz")


def test_the_package_is_copied_where_the_filesystem_refuses_a_link(monkeypatch, tmp_path):
    files = _hub_snapshot(tmp_path)

    def refuse(*args, **kwargs):
        raise OSError("cross-device link")

    monkeypatch.setattr(detector_minilm.os, "link", refuse)
    target = materialize_package(files, "coreml/pilot_ane_256.mlpackage")

    weights = target / "Data/com.apple.CoreML/weights/weight.bin"
    assert weights.read_bytes() == b"weights" and weights.stat().st_nlink == 1


@pytest.mark.asyncio
async def test_onnx_runtime_telemetry_is_switched_off_when_the_session_is_built(
    monkeypatch, tmp_path
):
    _fake_loading(monkeypatch, tmp_path)
    switched: list[bool] = []
    sys.modules["onnxruntime"].disable_telemetry_events = lambda: switched.append(True)  # type: ignore[attr-defined]

    await MiniLMDetector(MiniLMDetectorConfig(enabled=True)).initialize()

    assert switched == [True]


@pytest.mark.asyncio
async def test_tokens_that_cover_no_character_are_skipped():
    detector, _ = _detector()

    class WithEmpty(_WordTokenizer):
        def __call__(self, text, **kwargs):
            encoding = super().__call__(text, **kwargs)
            encoding["input_ids"].insert(0, 999)
            encoding["offset_mapping"].insert(0, (0, 0))
            return encoding

    detector._tokenizer = WithEmpty()
    detector._logits = _RuleLogits(detector._tokenizer)

    assert [e.text for e in await detector.detect("NameA here")] == ["NameA"]


@pytest.mark.asyncio
async def test_shutdown_releases_the_model():
    detector, _ = _detector()

    await detector.shutdown()

    with pytest.raises(RuntimeError, match="not initialized"):
        await detector.detect("NameA")


# ---------------------------------------------------------------------------
# Whole words
# ---------------------------------------------------------------------------


def _span(text: str, piece: str, kind: str = "PERSON") -> dict:
    start = text.index(piece)
    return {
        "entity_type": kind,
        "text": piece,
        "start": start,
        "end": start + len(piece),
        "score": 1.0,
    }


@pytest.mark.parametrize(
    ("text", "piece", "expected"),
    [
        ("say 'Hello Marie' now", "ello Marie", "Hello Marie"),  # started on a word's 2nd piece
        ("Sąd Okręgowy w Warszawie", "ąd Okręgowy", "Sąd Okręgowy"),  # accented first letter
        ("call Dupo nt", "Dupo", "Dupo"),  # ends at a space: nothing to extend
        ("call Dupont today", "Dupo", "Dupont"),  # stopped one piece short
        ("id 41540575 here", "154", "41540575"),
    ],
)
def test_a_span_cut_inside_a_word_takes_the_whole_word(text, piece, expected):
    (span,) = detector_minilm._to_whole_words([_span(text, piece)], text)
    assert span["text"] == expected == text[span["start"] : span["end"]]


@pytest.mark.parametrize(
    ("text", "piece"),
    [
        ("SMTP_HOST=mail.example.com", "HOST"),  # never grows into an identifier
        ("given__name__nonbinary", "nonbinary"),
        ("x" * 80 + "Marie", "Marie"),  # a word longer than any name: left as drawn
        ("/home/marie", "marie"),
    ],
)
def test_identifiers_and_overlong_words_are_left_as_drawn(text, piece):
    (span,) = detector_minilm._to_whole_words([_span(text, piece)], text)
    assert span["text"] == piece


@pytest.mark.asyncio
async def test_the_detector_masks_the_whole_word():
    detector, _ = _detector()

    class Splitting(_WordTokenizer):
        """Splits "xNameA" into "x" + "NameA", as XLM-R splits a first letter."""

        def __call__(self, text, **kwargs):
            encoding = super().__call__(text, **kwargs)
            ids, offsets = [], []
            for token, (start, end) in zip(
                encoding["input_ids"], encoding["offset_mapping"], strict=True
            ):
                word = text[start:end]
                if word.startswith("xName"):
                    head = self.vocab.setdefault("x", len(self.vocab) + 1)
                    tail = self.vocab.setdefault(word[1:], len(self.vocab) + 1)
                    self.words[head], self.words[tail] = "x", word[1:]
                    ids += [head, tail]
                    offsets += [(start, start + 1), (start + 1, end)]
                else:
                    ids.append(token)
                    offsets.append((start, end))
            return {"input_ids": ids, "offset_mapping": offsets}

    detector._tokenizer = Splitting()
    detector._logits = _RuleLogits(detector._tokenizer)

    assert [e.text for e in await detector.detect("hi xNameA there")] == ["xNameA"]


def test_the_docker_image_bakes_the_model_only_on_request():
    """The default preset does not use the model: the image stays the same size
    unless built with --build-arg MINILM=<backend>, which fetches exactly what
    that backend loads at the pinned revision."""
    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    assert 'ARG MINILM=""' in dockerfile
    assert 'if [ -n "$MINILM" ]' in dockerfile
    assert "BACKEND_FILES[os.environ['MINILM']]" in dockerfile
    assert "revision=config.revision" in dockerfile.split("ARG MINILM")[1]
