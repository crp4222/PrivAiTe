"""PrivAiTe's own PII model (crp4222/privaite-pii-minilm): a multilingual MiniLM
L12 token classifier, 15 types in BIO tags, trained and evaluated on windows of
256 tokens framed by its special tokens, 64 tokens of overlap. Three backends of
the same weights: ONNX fp32 (span-identical to the trained model), ONNX int8
(CPU, about twice as fast) and Core ML on the Apple Neural Engine (fixed 256
tokens, padded and masked).

Windowing, coverage checks, stitching and span decoding are the ONNX detector's
(`detector_onnx`), so the same guarantees hold: every source token is scanned
once, a span across a window edge is decoded once, and a gap in coverage blocks
the request instead of forwarding unscanned text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np

from privaite.config.schema import MiniLMDetectorConfig
from privaite.pii.boundaries import refine_spans
from privaite.pii.detector_base import PIIDetector
from privaite.pii.detector_onnx import (
    _COMMIT_HASH,
    _check_text_coverage,
    _check_window_coverage,
    _stitch_windows,
    _window_ranges,
    _WindowTokens,
    decode_bioes_spans,
)
from privaite.pii.entity import PIIEntity
from privaite.pii.window_cache import current_window_cache, inference_request

logger = logging.getLogger("privaite.pii.detector_minilm")

# The geometry the model was trained and evaluated with: 256 tokens per window
# including its two special tokens, consecutive windows sharing 64 tokens.
WINDOW_TOKENS = 256
WINDOW_OVERLAP = 64
_CONTENT_TOKENS = WINDOW_TOKENS - 2

# Model types whose value may itself start with a separator (a URL path, a
# secret made of slashes): span snapping must not trim it (see _append_span).
_SEPARATOR_BEARING = frozenset({"CONTACT", "SECRET"})

# What the XLM-R tokenizer drops from the text, and so never scans: whitespace,
# combining marks (a decomposed "é", as macOS writes file names, loses its
# accent), invisible format characters (zero-width space and joiners, direction
# marks, the byte-order mark), its own word marker and the replacement
# character. None carries a value on its own; a gap of anything else blocks.
_DROPPED_CATEGORIES = frozenset({"Mn", "Me", "Cf"})
_DROPPED_SYMBOLS = frozenset({"\u2581", "\ufffd"})


def unscanned_is_harmless(char: str) -> bool:
    return (
        char.isspace()
        or char in _DROPPED_SYMBOLS
        or unicodedata.category(char) in _DROPPED_CATEGORIES
    )


def _with_trailing_marks(spans: list[dict], text: str) -> list[dict]:
    """Extend each span over the combining marks right after it: the accent of a
    decomposed name belongs to the letter the span ends with."""
    for span in spans:
        end = span["end"]
        while end < len(text) and unicodedata.category(text[end]) in ("Mn", "Me"):
            end += 1
        if end != span["end"]:
            span["end"], span["text"] = end, text[span["start"] : end]
    return spans


# A word longer than this is not a name or a place: a span inside one (a hash,
# a minified identifier) is left as the model drew it.
_WORD_REACH = 64


def _is_word_char(char: str) -> bool:
    # Not the underscore: a span must never grow into a snake_case or
    # upper-case identifier (`SMTP_HOST`), which agents need to read in clear.
    return char.isalnum() or unicodedata.category(char) in ("Mn", "Me")


def _to_whole_words(spans: list[dict], text: str) -> list[dict]:
    """Extend a span that starts or ends inside a word to that word's edges.

    The model labels sub-word tokens and sometimes starts a name on the second
    piece of a word (`'Hello Marie'` came back as `H` + a span from `ello`), or
    stops one piece short. A cut inside a word hands the provider the rest of
    it, so the span takes the whole word: masking more is the safe direction.
    """
    for span in spans:
        start, end = span["start"], span["end"]
        if 0 < start < len(text) and _is_word_char(text[start - 1]) and _is_word_char(text[start]):
            word_start = start
            while word_start > 0 and _is_word_char(text[word_start - 1]):
                word_start -= 1
            if start - word_start <= _WORD_REACH:
                start = word_start
        if 0 < end < len(text) and _is_word_char(text[end - 1]) and _is_word_char(text[end]):
            word_end = end
            while word_end < len(text) and _is_word_char(text[word_end]):
                word_end += 1
            if word_end - end <= _WORD_REACH:
                end = word_end
        if (start, end) != (span["start"], span["end"]):
            span["start"], span["end"], span["text"] = start, end, text[start:end]
    return spans


_COREML_PACKAGE = "coreml/pilot_ane_256.mlpackage"
BACKEND_FILES: dict[str, tuple[str, ...]] = {
    "onnx": ("onnx/model.onnx",),
    "onnx_int8": ("onnx/model_int8.onnx",),
    "coreml": (
        f"{_COREML_PACKAGE}/Manifest.json",
        f"{_COREML_PACKAGE}/Data/com.apple.CoreML/model.mlmodel",
        f"{_COREML_PACKAGE}/Data/com.apple.CoreML/weights/weight.bin",
    ),
}


def download_minilm_files(
    repo_id: str,
    filenames: tuple[str, ...],
    cache_dir: str | None = None,
    revision: str | None = None,
) -> list[Path]:
    """Local paths of the given repo files, from the cache when the revision is
    a pinned commit (what the cache holds for a commit never changes, so the Hub
    is not asked), downloaded otherwise."""
    from huggingface_hub import hf_hub_download, try_to_load_from_cache

    pinned = bool(revision and _COMMIT_HASH.fullmatch(revision))
    paths = []
    for filename in filenames:
        known = (
            try_to_load_from_cache(repo_id, filename, cache_dir=cache_dir, revision=revision)
            if pinned
            else None
        )
        if isinstance(known, str):
            paths.append(Path(known))
        else:
            paths.append(
                Path(
                    hf_hub_download(
                        repo_id=repo_id, filename=filename, cache_dir=cache_dir, revision=revision
                    )
                )
            )
    return paths


# Folder, inside the model repo's cache folder, holding real copies of the Core
# ML package: the hub cache hands out symlinks, which the Core ML compiler does
# not follow ("weight.bin does not exist" while compiling).
_MATERIALIZED_DIR = "privaite-coreml"


def materialize_package(files: list[Path], package: str) -> Path:
    """The package folder, as real files: hard links into the hub cache (no copy,
    no download) or copies where the filesystem refuses a link. ``files`` are the
    package's files as the hub returned them, ``package`` its path in the repo."""
    prefix = package.rstrip("/") + "/"
    root = Path(str(files[0]).split(prefix, 1)[0])  # the snapshot (or local) folder
    if root.parent.name != "snapshots":  # not the hub cache: real files already
        return root / package
    # Named after the commit: a commit never changes, so a folder that exists
    # always holds that commit's files, and a new revision gets its own.
    target = root.parent.parent / _MATERIALIZED_DIR / root.name / Path(package).name
    for path in files:
        dest = target / str(path).split(prefix, 1)[1]
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Written under a private name, then renamed: a concurrent start never
        # sees a half-copied file.
        partial = dest.with_name(f".{dest.name}.{os.getpid()}.partial")
        partial.unlink(missing_ok=True)
        try:
            os.link(path.resolve(), partial)
        except OSError:
            shutil.copyfile(path.resolve(), partial)
        os.replace(partial, dest)
    return target


class MiniLMDetector(PIIDetector):
    def __init__(self, config: MiniLMDetectorConfig) -> None:
        self.config = config
        self._tokenizer: Any = None
        self._id2label: dict[int, str] = {}
        self._runner: Any = None  # the session or Core ML model, for the window cache key
        self._logits: Any = None  # ids -> per-token logits, without the special tokens' rows

    @property
    def name(self) -> str:
        return "minilm"

    async def initialize(self) -> None:
        def _load() -> None:
            from transformers import AutoTokenizer

            backend = self.config.backend
            if backend == "coreml" and sys.platform != "darwin":
                raise RuntimeError(
                    "minilm backend 'coreml' runs on the Apple Neural Engine and needs macOS; "
                    "use backend 'onnx' or 'onnx_int8' (preset 'minilm') elsewhere"
                )
            self._tokenizer = self._load_tokenizer(AutoTokenizer)
            # Windows are cut here, never by the tokenizer: its 256-token limit
            # would only log a misleading "indexing errors" warning on every
            # long text.
            self._tokenizer.model_max_length = sys.maxsize
            (config_path,) = download_minilm_files(
                self.config.model_name,
                ("config.json",),
                self.config.cache_dir,
                self.config.revision,
            )
            labels = json.loads(config_path.read_text(encoding="utf-8"))["id2label"]
            self._id2label = {int(k): v for k, v in labels.items()}
            files = download_minilm_files(
                self.config.model_name,
                BACKEND_FILES[backend],
                self.config.cache_dir,
                self.config.revision,
            )
            if backend == "coreml":
                self._load_coreml(materialize_package(files, _COREML_PACKAGE))
            else:
                self._load_onnx(files[0])
            # Fail at startup, not on the first request: a Core ML model that
            # does not compile only warns when it loads.
            self._logits([self._tokenizer.cls_token_id, self._tokenizer.sep_token_id])

        logger.info(
            "Loading PrivAiTe MiniLM model (%s, backend=%s)...",
            self.config.model_name,
            self.config.backend,
        )
        await asyncio.to_thread(_load)
        logger.info("MiniLM model loaded")

    def _load_tokenizer(self, auto_tokenizer: Any) -> Any:
        """From the local cache first when the revision is pinned (see the onnx
        detector: from_pretrained otherwise asks the Hub about every file)."""
        if self.config.revision and _COMMIT_HASH.fullmatch(self.config.revision):
            try:
                return auto_tokenizer.from_pretrained(
                    self.config.model_name,
                    revision=self.config.revision,
                    cache_dir=self.config.cache_dir,
                    local_files_only=True,
                )
            except OSError:
                pass
        return auto_tokenizer.from_pretrained(
            self.config.model_name, revision=self.config.revision, cache_dir=self.config.cache_dir
        )

    def _load_onnx(self, model_path: Path) -> None:
        import onnxruntime as ort

        # A host that initialised ONNX Runtime before importing privaite no
        # longer reads ORT_DISABLE_TELEMETRY.
        telemetry_off = getattr(ort, "disable_telemetry_events", None)
        if telemetry_off is not None:
            telemetry_off()
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )

        def logits(ids: list[int]) -> Any:
            feed = np.asarray([ids], dtype=np.int64)
            return session.run(None, {"input_ids": feed, "attention_mask": np.ones_like(feed)})[0][
                0
            ]

        self._runner, self._logits = session, logits

    def _load_coreml(self, package: Path) -> None:
        try:
            import coremltools as ct
        except ImportError as exc:
            raise RuntimeError(
                "minilm backend 'coreml' needs coremltools: pip install 'privaite[ane]'"
            ) from exc
        model = ct.models.MLModel(str(package), compute_units=ct.ComputeUnit.CPU_AND_NE)
        pad = self._tokenizer.pad_token_id

        def logits(ids: list[int]) -> Any:
            n = len(ids)
            feed = np.full((1, WINDOW_TOKENS), pad, dtype=np.int32)
            mask = np.zeros((1, WINDOW_TOKENS), dtype=np.int32)
            feed[0, :n], mask[0, :n] = ids, 1
            return model.predict({"input_ids": feed, "attention_mask": mask})["logits"][0, :n]

        self._runner, self._logits = model, logits

    @inference_request
    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        if self._logits is None or self._tokenizer is None:
            raise RuntimeError("MiniLMDetector not initialized")

        spans = await asyncio.to_thread(self._run_inference, text)
        entities = []
        for span in spans:
            if span["score"] < self.config.score_threshold:
                continue
            mapped = self.config.label_mapping.get(span["entity_type"])
            if not mapped:
                continue
            entities.append(
                PIIEntity(
                    entity_type=mapped,
                    text=span["text"],
                    start=span["start"],
                    end=span["end"],
                    score=span["score"],
                    source="minilm",
                )
            )
        return [piece for entity in entities for piece in refine_spans(text, entity)]

    def _run_inference(self, text: str) -> list[dict]:
        encoding = self._tokenizer(
            text, add_special_tokens=False, truncation=False, return_offsets_mapping=True
        )
        ids, offsets = encoding["input_ids"], encoding["offset_mapping"]
        if len(offsets) != len(ids):
            raise RuntimeError("Inconsistent MiniLM token encoding lengths")
        ranges = _window_ranges(len(ids), _CONTENT_TOKENS, WINDOW_OVERLAP)
        _check_window_coverage(ranges, len(ids))
        _check_text_coverage(offsets, text, unscanned_is_harmless)
        rows = [self._run_window(ids[start:end], offsets[start:end]) for start, end in ranges]
        labels, scores, kept = _stitch_windows(rows, WINDOW_OVERLAP)
        spans = decode_bioes_spans(labels, scores, kept, text, _SEPARATOR_BEARING)
        return _to_whole_words(_with_trailing_marks(spans, text), text)

    def _run_window(self, ids: list[int], offsets: list[tuple[int, int]]) -> _WindowTokens:
        framed = [self._tokenizer.cls_token_id, *ids, self._tokenizer.sep_token_id]
        cache = current_window_cache() if self.config.deduplicate_windows else None
        key = None
        if cache is not None:
            raw = np.asarray(framed, dtype=np.int64)
            key = cache.key(self._runner, [("input_ids", raw.shape, raw.tobytes())])
        predictions = cache.get(key) if cache is not None and key is not None else None
        if predictions is None:
            logits = np.asarray(self._logits(framed), dtype=np.float32)[1:-1]
            exp = np.exp(logits - logits.max(axis=-1, keepdims=True))
            probs = exp / exp.sum(axis=-1, keepdims=True)
            predictions = (
                tuple(int(label) for label in probs.argmax(axis=-1)),
                tuple(float(score) for score in probs.max(axis=-1)),
            )
            if cache is not None and key is not None:
                cache.put(key, predictions)
        pred_ids, pred_scores = predictions

        labels: list[str] = []
        scores: list[float] = []
        kept: list[tuple[int, int]] = []
        for i, (start, end) in enumerate(offsets):
            if start == end:
                continue
            labels.append(self._id2label.get(pred_ids[i], "O"))
            scores.append(pred_scores[i])
            kept.append((int(start), int(end)))
        return labels, scores, kept

    async def shutdown(self) -> None:
        self._tokenizer = None
        self._runner = None
        self._logits = None
