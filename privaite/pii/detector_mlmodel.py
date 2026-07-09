from __future__ import annotations

import asyncio
import logging
from typing import Any

from privaite.config.schema import MLModelDetectorConfig
from privaite.pii.detector_base import HFPipelineDetector, resolve_torch_device

logger = logging.getLogger("privaite.pii.detector_mlmodel")


def _resolve_dtype(dtype_str: str):
    import torch

    return {"float16": torch.float16, "float32": torch.float32, "bfloat16": torch.bfloat16}.get(
        dtype_str, torch.float16
    )


class MLModelDetector(HFPipelineDetector):
    def __init__(self, config: MLModelDetectorConfig) -> None:
        self.config = config
        self._classifier: Any = None

    @property
    def name(self) -> str:
        return "mlmodel"

    async def initialize(self) -> None:
        def _load() -> None:
            from transformers import AutoModelForTokenClassification, AutoTokenizer, pipeline

            device = resolve_torch_device(self.config.device)
            dtype = _resolve_dtype(self.config.torch_dtype)

            logger.info(
                "Loading model weights (dtype=%s, device=%s)...",
                self.config.torch_dtype, device,
            )

            tokenizer = AutoTokenizer.from_pretrained(
                self.config.model_name,
                revision=self.config.revision,
                trust_remote_code=self.config.trust_remote_code,
            )
            model = AutoModelForTokenClassification.from_pretrained(
                self.config.model_name,
                revision=self.config.revision,
                trust_remote_code=self.config.trust_remote_code,
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

    async def shutdown(self) -> None:
        self._classifier = None
