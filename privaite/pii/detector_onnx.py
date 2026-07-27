from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from privaite.config.schema import OnnxDetectorConfig
from privaite.pii.detector_base import PIIDetector
from privaite.pii.entity import PIIEntity

logger = logging.getLogger("privaite.pii.detector_onnx")

_ENTITY_ORDER = [
    "account_number",
    "private_address",
    "private_date",
    "private_email",
    "private_person",
    "private_phone",
    "private_url",
    "secret",
]
_BIOES = ["B", "I", "E", "S"]

ID2LABEL: dict[int, str] = {0: "O"}
for _i, _ent in enumerate(_ENTITY_ORDER):
    for _j, _tag in enumerate(_BIOES):
        ID2LABEL[_i * 4 + _j + 1] = f"{_tag}-{_ent}"

ENTITY_TYPES = [
    "account_number",
    "private_address",
    "private_date",
    "private_email",
    "private_person",
    "private_phone",
    "private_url",
    "secret",
]

# Window geometry for long inputs. A single full-sequence run costs quadratic
# time and memory (measured on M1 Pro, CPU EP: 2046 tokens 1.5s, 8192 tokens
# 133s at 8.0GB peak RSS), so no single ONNX run may exceed _WINDOW_TOKENS
# tokens. Adjacent windows share _WINDOW_OVERLAP tokens of context; each shared
# token keeps the prediction from the window where it sits furthest from an
# edge, and BIOES decoding runs once over the stitched sequence (see
# _stitch_windows). Measured at 1024/128: an 8192-token leaf drops from 154.7s
# to 6.9s with peak RSS bounded near 2.2GB, and results on real captured
# payloads are span-identical to full inference.
_WINDOW_TOKENS = 1024
_WINDOW_OVERLAP = 128

# Per-window token predictions: (labels, scores, offsets), offsets absolute
# into the original text (fast tokenizers report offsets into the full text
# even for overflow windows).
_WindowTokens = tuple[list[str], list[float], list[tuple[int, int]]]


def _shared_run_length(
    prev_offsets: list[tuple[int, int]],
    cur_offsets: list[tuple[int, int]],
    expected: int,
) -> int:
    """Number of trailing tokens of ``prev_offsets`` that are the same source
    tokens as the head of ``cur_offsets``. Normally exactly the tokenizer
    stride, but derived from the offsets themselves so a stride quirk can only
    cost speed, never alignment (a silent misalignment would duplicate or drop
    tokens, i.e. duplicate or lose PII spans)."""
    limit = min(len(prev_offsets), len(cur_offsets))
    expected = min(expected, limit)
    if expected and prev_offsets[-expected:] == cur_offsets[:expected]:
        return expected
    for run in range(limit, 0, -1):
        if prev_offsets[-run:] == cur_offsets[:run]:
            return run
    return 0


def _stitch_windows(rows: list[_WindowTokens], overlap: int) -> _WindowTokens:
    """Merge per-window token predictions into one global token sequence.

    Each token shared by two windows is kept exactly once: the first half of
    the shared run from the earlier window, the second half from the later one,
    so every kept token has at least overlap // 2 tokens of real context on
    each side (except at the ends of the text, where no more context exists and
    full inference would see none either). BIOES decoding then runs once over
    the stitched sequence, so an entity whose B tokens fall in one window and
    whose E token falls in the next is decoded as one span, never duplicated
    and never split at the boundary."""
    if not rows:
        return [], [], []
    if len(rows) == 1:
        return rows[0]
    shared = [_shared_run_length(rows[r][2], rows[r + 1][2], overlap) for r in range(len(rows) - 1)]
    labels: list[str] = []
    scores: list[float] = []
    offsets: list[tuple[int, int]] = []
    for r, (row_labels, row_scores, row_offsets) in enumerate(rows):
        head = shared[r - 1] // 2 if r > 0 else 0
        tail = len(row_offsets)
        if r < len(rows) - 1:
            tail -= shared[r] - shared[r] // 2
        labels.extend(row_labels[head:tail])
        scores.extend(row_scores[head:tail])
        offsets.extend(row_offsets[head:tail])
    return labels, scores, offsets


def _parse_tag(label: str) -> tuple[str, str]:
    if label == "O":
        return ("O", "O")
    prefix, entity = label.split("-", 1)
    return (prefix, entity)


def decode_bioes_spans(
    labels: Sequence[str],
    scores: Sequence[float],
    offsets: Sequence[tuple[int, int]],
    text: str,
) -> list[dict]:
    spans: list[dict] = []
    current_tokens: list[int] = []
    current_type: str | None = None

    def _flush() -> None:
        nonlocal current_tokens, current_type
        if not current_tokens or current_type is None:
            current_tokens, current_type = [], None
            return
        start = offsets[current_tokens[0]][0]
        end = offsets[current_tokens[-1]][1]
        avg_score = float(np.mean([scores[i] for i in current_tokens]))
        spans.append(
            {
                "entity_type": current_type,
                "text": text[start:end],
                "start": start,
                "end": end,
                "score": avg_score,
            }
        )
        current_tokens, current_type = [], None

    for idx, label in enumerate(labels):
        prefix, etype = _parse_tag(label)

        if prefix == "S":
            _flush()
            s, e = offsets[idx][0], offsets[idx][1]
            spans.append(
                {
                    "entity_type": etype,
                    "text": text[s:e],
                    "start": s,
                    "end": e,
                    "score": float(scores[idx]),
                }
            )

        elif prefix == "B":
            _flush()
            current_type = etype
            current_tokens = [idx]

        elif prefix in ("I", "E"):
            if current_type == etype:
                current_tokens.append(idx)
                if prefix == "E":
                    _flush()
            else:
                _flush()
                current_type = etype
                current_tokens = [idx]

        else:
            _flush()

    _flush()
    return spans


def download_onnx_model(
    repo_id: str = "openai/privacy-filter",
    variant: str = "q4f16",
    cache_dir: str | None = None,
    revision: str | None = None,
) -> Path:
    from huggingface_hub import hf_hub_download

    # EntryNotFoundError moved to the top-level .errors module in hub 0.25; older
    # versions (pyproject floors at 0.23) expose it only under .utils. Import
    # defensively so a floor install does not crash the default onnx detector.
    try:
        from huggingface_hub.errors import EntryNotFoundError
    except ImportError:  # pragma: no cover - only on hub < 0.25
        from huggingface_hub.utils import EntryNotFoundError

    local_path = Path(
        hf_hub_download(
            repo_id=repo_id,
            filename=f"onnx/model_{variant}.onnx",
            cache_dir=cache_dir,
            revision=revision,
        )
    )
    try:
        # Only variants with externalized weights ship this side file; a variant
        # packed into a single .onnx must not fail here.
        hf_hub_download(
            repo_id=repo_id,
            filename=f"onnx/model_{variant}.onnx_data",
            cache_dir=cache_dir,
            revision=revision,
        )
    except EntryNotFoundError:
        logger.info("Variant %s has no .onnx_data side file (single-file model)", variant)

    logger.info("ONNX model ready at %s", local_path)
    return local_path


class OnnxPrivacyFilterDetector(PIIDetector):
    def __init__(self, config: OnnxDetectorConfig) -> None:
        self.config = config
        self._session: Any = None
        self._tokenizer: Any = None

    @property
    def name(self) -> str:
        return "onnx"

    async def initialize(self) -> None:
        def _load() -> None:
            import onnxruntime as ort
            from transformers import AutoTokenizer

            model_path = download_onnx_model(
                repo_id=self.config.model_name,
                variant=self.config.onnx_variant,
                cache_dir=self.config.cache_dir,
                revision=self.config.revision,
            )

            requested = self._get_providers()
            logger.info(
                "Creating ONNX session (variant=%s, requested_providers=%s)",
                self.config.onnx_variant,
                requested,
            )

            sess_opts = ort.SessionOptions()
            sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(
                str(model_path),
                sess_options=sess_opts,
                providers=requested,
            )
            self._log_session_providers(requested)

            self._tokenizer = AutoTokenizer.from_pretrained(
                self.config.model_name,
                revision=self.config.revision,
                trust_remote_code=self.config.trust_remote_code,
            )

        logger.info("Loading ONNX privacy-filter model (%s)...", self.config.model_name)
        await asyncio.to_thread(_load)
        logger.info("ONNX model loaded successfully")

    def _log_session_providers(self, requested: list[str]) -> None:
        """Report the execution providers the session ACTUALLY runs on.

        onnxruntime does not fail when a requested provider is absent from the
        build: it emits a Python UserWarning and builds a CPU session. Logging
        the requested list alone printed "CUDAExecutionProvider" on a CPU-only
        machine, so an operator could not tell a working accelerator from a
        silent fallback."""
        active = list(self._session.get_providers())
        missing = [provider for provider in requested if provider not in active]
        if missing:
            logger.warning(
                "Requested ONNX execution provider(s) %s not available in this "
                "onnxruntime build; the session runs on %s",
                missing,
                active,
            )
        logger.info("ONNX session providers: %s", active)

    def _get_providers(self) -> list[str]:
        device = self.config.device
        if device == "auto":
            import onnxruntime as ort

            available = ort.get_available_providers()
            # "auto" deliberately never selects CoreML. Measured on this model
            # (M1 Pro, onnxruntime 1.27): CoreML covers only 28 of 365 graph
            # nodes in 25 partitions, is slower than CPU at every input size
            # (0.88s vs 0.47s at 511 tokens, 8.55s vs 1.50s at 2046), and each
            # new input shape accumulates compiled-model memory until the
            # kernel kills the process. There is no size at which it wins, so
            # do not "restore" it here; device: "coreml" stays as an explicit
            # opt-in below.
            if "CUDAExecutionProvider" in available:
                return ["CUDAExecutionProvider", "CPUExecutionProvider"]
            return ["CPUExecutionProvider"]
        if device == "cuda":
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        if device in ("coreml", "mps"):
            return ["CoreMLExecutionProvider", "CPUExecutionProvider"]
        # Only "cpu" reaches here: the config schema refuses every other value at
        # boot, so this is no longer a silent catch-all for typos.
        return ["CPUExecutionProvider"]

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        if self._session is None or self._tokenizer is None:
            raise RuntimeError("OnnxPrivacyFilterDetector not initialized")

        results = await asyncio.to_thread(self._run_inference, text)

        pii_entities: list[PIIEntity] = []
        for span in results:
            if span["score"] < self.config.score_threshold:
                continue

            mapped_type = self.config.label_mapping.get(span["entity_type"])
            if not mapped_type:
                continue

            pii_entities.append(
                PIIEntity(
                    entity_type=mapped_type,
                    text=span["text"],
                    start=span["start"],
                    end=span["end"],
                    score=span["score"],
                    source="onnx",
                )
            )

        return pii_entities

    def _window_geometry(self) -> tuple[int, int]:
        """Effective (window, overlap) in tokens. ``max_length`` used to be a
        truncation cap whose overflow was forwarded unscanned; it now bounds the
        per-window sequence length instead (capped at _WINDOW_TOKENS), and the
        text is always covered whole."""
        window = min(self.config.max_length, _WINDOW_TOKENS)
        return window, min(_WINDOW_OVERLAP, max(window // 8, 1))

    def _run_inference(self, text: str) -> list[dict]:
        window, overlap = self._window_geometry()
        encoding = self._tokenizer(
            text,
            truncation=True,
            max_length=window,
            stride=overlap,
            return_overflowing_tokens=True,
            return_offsets_mapping=True,
        )
        input_names = {inp.name for inp in self._session.get_inputs()}
        rows = [
            self._run_window(encoding, row, input_names)
            for row in range(len(encoding["input_ids"]))
        ]
        labels, scores, offsets = _stitch_windows(rows, overlap)
        return decode_bioes_spans(labels, scores, offsets, text)

    def _run_window(self, encoding: Any, row: int, input_names: set[str]) -> _WindowTokens:
        """Run one window through the session and return its filtered per-token
        predictions with offsets absolute into the original text."""
        feed = {}
        for key in ("input_ids", "attention_mask", "token_type_ids"):
            if key in input_names and key in encoding:
                feed[key] = np.asarray([encoding[key][row]], dtype=np.int64)

        logits = self._session.run(None, feed)[0][0]
        exp = np.exp(logits - logits.max(axis=-1, keepdims=True))
        probs = exp / exp.sum(axis=-1, keepdims=True)
        pred_ids = probs.argmax(axis=-1)
        pred_scores = probs.max(axis=-1)

        labels: list[str] = []
        scores: list[float] = []
        offsets: list[tuple[int, int]] = []
        for i, (start, end) in enumerate(encoding["offset_mapping"][row]):
            if start == end:
                # Special or padding token: it maps to no source characters.
                continue
            labels.append(ID2LABEL.get(int(pred_ids[i]), "O"))
            scores.append(float(pred_scores[i]))
            offsets.append((int(start), int(end)))
        return labels, scores, offsets

    async def shutdown(self) -> None:
        self._session = None
        self._tokenizer = None
