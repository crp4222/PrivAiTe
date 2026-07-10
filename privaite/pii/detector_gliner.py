from __future__ import annotations

import asyncio
import logging
from typing import Any

from privaite.config.schema import GlinerDetectorConfig
from privaite.pii.detector_base import (
    NerResult,
    PIIDetector,
    resolve_torch_device,
    windowed_ner_detect,
)
from privaite.pii.entity import PIIEntity

logger = logging.getLogger("privaite.pii.detector_gliner")


def _gliner_extract(result: dict, chunk: str) -> NerResult:
    start = int(result.get("start", 0))
    end = int(result.get("end", 0))
    return (result.get("label", ""), result.get("score", 0.0), start, end, chunk[start:end])


class GlinerDetector(PIIDetector):
    """GLiNER PII detector (the `max` preset).

    GLiNER is a label-conditioned span extractor trained on synthetic data that is
    independent of AI4Privacy, so it raises out-of-distribution recall when unioned
    with the onnx suite. It needs torch + the `gliner` package, which are not part
    of the onnxruntime floor, so initialization fails loudly with an install hint if
    the package is missing (fail closed, never silently skipped).
    """

    def __init__(self, config: GlinerDetectorConfig) -> None:
        self.config = config
        self._model: Any = None

    @property
    def name(self) -> str:
        return "gliner"

    async def initialize(self) -> None:
        def _load() -> None:
            try:
                from gliner import GLiNER
            except ImportError as exc:
                raise RuntimeError(
                    "The 'max' preset / gliner detector needs the gliner package. "
                    "Install it with: pip install 'privaite[gliner]'"
                ) from exc

            model = GLiNER.from_pretrained(self.config.model_name, revision=self.config.revision)
            device = resolve_torch_device(self.config.device)
            self._model = model.to(device)

        logger.info("Loading GLiNER model: %s ...", self.config.model_name)
        await asyncio.to_thread(_load)
        logger.info("GLiNER model loaded")

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        if self._model is None:
            raise RuntimeError("GlinerDetector not initialized")

        # GLiNER truncates long inputs (~384 tokens); windowing (max_chars=1200)
        # keeps PII past that point visible. It filters by threshold itself, and the
        # shared loop re-checks it and dedupes overlapping-window hits.
        labels = self.config.labels
        threshold = self.config.score_threshold
        return await windowed_ner_detect(
            text,
            predict=lambda chunk: self._model.predict_entities(chunk, labels, threshold=threshold),
            extract=_gliner_extract,
            label_mapping=self.config.label_mapping,
            score_threshold=threshold,
            source="gliner",
            max_chars=1200,
        )

    async def shutdown(self) -> None:
        self._model = None
