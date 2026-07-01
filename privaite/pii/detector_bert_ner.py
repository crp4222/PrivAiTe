from __future__ import annotations

import asyncio
import logging
from typing import Any

from privaite.config.schema import BertNERDetectorConfig
from privaite.pii.detector_base import PIIDetector
from privaite.pii.entity import PIIEntity

logger = logging.getLogger("privaite.pii.detector_bert_ner")


class BertNERDetector(PIIDetector):
    def __init__(self, config: BertNERDetectorConfig) -> None:
        self.config = config
        self._classifier: Any = None

    @property
    def name(self) -> str:
        return "bert_ner"

    async def initialize(self) -> None:
        def _load() -> None:
            from transformers import pipeline

            device = _resolve_device(self.config.device)
            self._classifier = pipeline(
                task="token-classification",
                model=self.config.model_name,
                revision=self.config.revision,
                device=device,
                aggregation_strategy="simple",
            )

        logger.info("Loading BERT NER model: %s ...", self.config.model_name)
        await asyncio.to_thread(_load)
        logger.info("BERT NER model loaded")

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        if self._classifier is None:
            raise RuntimeError("BertNERDetector not initialized")

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
                    source="bert_ner",
                )
            )

        return pii_entities

    async def shutdown(self) -> None:
        self._classifier = None


def _resolve_device(device_str: str) -> int | str:
    if device_str == "auto":
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return 0
        return -1
    if device_str == "cpu":
        return -1
    if device_str == "mps":
        return "mps"
    if device_str == "cuda":
        return 0
    return -1
