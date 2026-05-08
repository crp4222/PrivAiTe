from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from privaite.config.schema import OnnxDetectorConfig
from privaite.pii.detector_base import PIIDetector
from privaite.pii.entity import PIIEntity

logger = logging.getLogger("privaite.pii.detector_onnx")

_ENTITY_ORDER = [
    "account_number", "private_address", "private_date", "private_email",
    "private_person", "private_phone", "private_url", "secret",
]
_BIOES = ["B", "I", "E", "S"]

ID2LABEL: dict[int, str] = {0: "O"}
for _i, _ent in enumerate(_ENTITY_ORDER):
    for _j, _tag in enumerate(_BIOES):
        ID2LABEL[_i * 4 + _j + 1] = f"{_tag}-{_ent}"

ENTITY_TYPES = [
    "account_number", "private_address", "private_date", "private_email",
    "private_person", "private_phone", "private_url", "secret",
]



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
        spans.append({
            "entity_type": current_type,
            "text": text[start:end],
            "start": start,
            "end": end,
            "score": avg_score,
        })
        current_tokens, current_type = [], None

    for idx, label in enumerate(labels):
        prefix, etype = _parse_tag(label)

        if prefix == "S":
            _flush()
            s, e = offsets[idx][0], offsets[idx][1]
            spans.append({
                "entity_type": etype,
                "text": text[s:e],
                "start": s,
                "end": e,
                "score": float(scores[idx]),
            })

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
) -> Path:
    from huggingface_hub import hf_hub_download

    filenames = [f"onnx/model_{variant}.onnx", f"onnx/model_{variant}.onnx_data"]
    local_path: Path | None = None
    for fn in filenames:
        p = hf_hub_download(
            repo_id=repo_id,
            filename=fn,
            cache_dir=cache_dir,
        )
        if fn.endswith(".onnx"):
            local_path = Path(p)

    if local_path is None:
        raise FileNotFoundError(f"ONNX model file not found for variant '{variant}'")
    logger.info("ONNX model ready at %s", local_path)
    return local_path



class OnnxPrivacyFilterDetector(PIIDetector):

    def __init__(self, config: OnnxDetectorConfig) -> None:
        self.config = config
        self._session = None
        self._tokenizer = None

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
            )

            providers = self._get_providers()
            logger.info(
                "Creating ONNX session (variant=%s, providers=%s)",
                self.config.onnx_variant,
                providers,
            )

            sess_opts = ort.SessionOptions()
            sess_opts.graph_optimization_level = (
                ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            )
            self._session = ort.InferenceSession(
                str(model_path),
                sess_options=sess_opts,
                providers=providers,
            )

            self._tokenizer = AutoTokenizer.from_pretrained(
                self.config.model_name, trust_remote_code=True
            )

        logger.info("Loading ONNX privacy-filter model (%s)...", self.config.model_name)
        await asyncio.to_thread(_load)
        logger.info("ONNX model loaded successfully")

    def _get_providers(self) -> list[str]:
        device = self.config.device
        if device == "auto":
            import onnxruntime as ort
            available = ort.get_available_providers()
            if "CoreMLExecutionProvider" in available:
                return ["CoreMLExecutionProvider", "CPUExecutionProvider"]
            if "CUDAExecutionProvider" in available:
                return ["CUDAExecutionProvider", "CPUExecutionProvider"]
            return ["CPUExecutionProvider"]
        if device == "cuda":
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        if device in ("coreml", "mps"):
            return ["CoreMLExecutionProvider", "CPUExecutionProvider"]
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

    def _run_inference(self, text: str) -> list[dict]:
        encoding = self._tokenizer(
            text,
            return_tensors="np",
            truncation=True,
            max_length=self.config.max_length,
            return_offsets_mapping=True,
        )

        offset_mapping = encoding.pop("offset_mapping")[0]
        input_names = {inp.name for inp in self._session.get_inputs()}
        feed = {}
        for key in ("input_ids", "attention_mask", "token_type_ids"):
            if key in input_names and key in encoding:
                feed[key] = encoding[key].astype(np.int64)

        logits = self._session.run(None, feed)[0][0]
        exp = np.exp(logits - logits.max(axis=-1, keepdims=True))
        probs = exp / exp.sum(axis=-1, keepdims=True)
        pred_ids = probs.argmax(axis=-1)
        pred_scores = probs.max(axis=-1)

        labels: list[str] = []
        scores: list[float] = []
        offsets: list[tuple[int, int]] = []

        for i, (start, end) in enumerate(offset_mapping):
            if start == 0 and end == 0 and (i == 0 or i == len(offset_mapping) - 1):
                continue
            if start == end:
                continue

            labels.append(ID2LABEL.get(int(pred_ids[i]), "O"))
            scores.append(float(pred_scores[i]))
            offsets.append((int(start), int(end)))

        return decode_bioes_spans(labels, scores, offsets, text)

    async def shutdown(self) -> None:
        self._session = None
        self._tokenizer = None
