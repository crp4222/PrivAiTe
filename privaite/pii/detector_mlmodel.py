from __future__ import annotations

import asyncio
import logging
from typing import Any

from privaite.config.schema import MLModelDetectorConfig
from privaite.pii.detector_base import PIIDetector
from privaite.pii.entity import PIIEntity

logger = logging.getLogger("privaite.pii.detector_mlmodel")


def _resolve_device(device_str: str) -> str:
    if device_str == "auto":
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
        return "cpu"
    return device_str


def _resolve_dtype(dtype_str: str):
    import torch

    return {"float16": torch.float16, "float32": torch.float32, "bfloat16": torch.bfloat16}.get(
        dtype_str, torch.float16
    )


class MLModelDetector(PIIDetector):
    def __init__(self, config: MLModelDetectorConfig) -> None:
        self.config = config
        self._classifier: Any = None

    @property
    def name(self) -> str:
        return "mlmodel"

    async def initialize(self) -> None:
        def _load() -> None:
            from transformers import AutoModelForTokenClassification, AutoTokenizer, pipeline

            device = _resolve_device(self.config.device)
            dtype = _resolve_dtype(self.config.torch_dtype)

            logger.info(
                "Loading model weights (dtype=%s, device=%s)...",
                self.config.torch_dtype, device,
            )

            tokenizer = AutoTokenizer.from_pretrained(
                self.config.model_name, trust_remote_code=True
            )
            model = AutoModelForTokenClassification.from_pretrained(
                self.config.model_name,
                trust_remote_code=True,
                torch_dtype=dtype,
                low_cpu_mem_usage=True,
            )

            if device != "cpu":
                model = model.to(device)

            self._classifier = pipeline(
                task="token-classification",
                model=model,
                tokenizer=tokenizer,
                device=device if device != "cpu" else -1,
                aggregation_strategy="simple",
            )

        logger.info("Loading ML model: %s ...", self.config.model_name)
        await asyncio.to_thread(_load)
        logger.info("ML model loaded successfully")

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        if self._classifier is None:
            raise RuntimeError("MLModelDetector not initialized")

        results = await asyncio.to_thread(self._classifier, text)

        pii_entities: list[PIIEntity] = []
        for result in results:
            entity_group = result.get("entity_group", "")
            score = result.get("score", 0.0)

            if score < self.config.score_threshold:
                continue

            mapped_type = self.config.label_mapping.get(entity_group)
            if not mapped_type:
                continue

            start = result.get("start", 0)
            end = result.get("end", 0)
            word = result.get("word", text[start:end])

            pii_entities.append(
                PIIEntity(
                    entity_type=mapped_type,
                    text=word.strip(),
                    start=start,
                    end=end,
                    score=score,
                    source="mlmodel",
                )
            )

        return pii_entities

    async def shutdown(self) -> None:
        self._classifier = None
