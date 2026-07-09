from __future__ import annotations

import asyncio
import logging
from typing import Any

from privaite.config.schema import BertNERDetectorConfig
from privaite.pii.detector_base import HFPipelineDetector, resolve_torch_device

logger = logging.getLogger("privaite.pii.detector_bert_ner")


class BertNERDetector(HFPipelineDetector):
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

    async def shutdown(self) -> None:
        self._classifier = None


def _resolve_device(device_str: str) -> int | str:
    # HF pipelines want a device index (-1 cpu, 0 cuda) or the "mps" string; map the
    # shared torch device name onto that format.
    pipeline_device: dict[str, int | str] = {"cpu": -1, "cuda": 0, "mps": "mps"}
    return pipeline_device.get(resolve_torch_device(device_str), -1)
