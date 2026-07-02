from __future__ import annotations

import asyncio
import logging
from typing import Any

from privaite.config.schema import GlinerDetectorConfig
from privaite.pii.detector_base import PIIDetector, chunk_text
from privaite.pii.entity import PIIEntity

logger = logging.getLogger("privaite.pii.detector_gliner")


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

            model = GLiNER.from_pretrained(
                self.config.model_name, revision=self.config.revision
            )
            device = _resolve_device(self.config.device)
            self._model = model.to(device)

        logger.info("Loading GLiNER model: %s ...", self.config.model_name)
        await asyncio.to_thread(_load)
        logger.info("GLiNER model loaded")

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        if self._model is None:
            raise RuntimeError("GlinerDetector not initialized")

        labels = self.config.labels
        threshold = self.config.score_threshold
        pii_entities: list[PIIEntity] = []
        seen: set[tuple[int, int, str]] = set()

        # GLiNER truncates long inputs (~384 tokens); run overlapping windows so PII
        # past that point is still seen, and offset spans back to the full text.
        for offset, chunk in chunk_text(text, max_chars=1200):
            results = await asyncio.to_thread(
                self._model.predict_entities, chunk, labels, threshold=threshold
            )
            for result in results:
                score = result.get("score", 0.0)
                if score < threshold:
                    continue

                mapped_type = self.config.label_mapping.get(result.get("label", ""))
                if not mapped_type:
                    continue

                start = int(result.get("start", 0)) + offset
                end = int(result.get("end", 0)) + offset

                key = (start, end, mapped_type)
                if key in seen:
                    continue
                seen.add(key)

                pii_entities.append(
                    PIIEntity(
                        entity_type=mapped_type,
                        text=text[start:end],
                        start=start,
                        end=end,
                        score=float(score),
                        source="gliner",
                    )
                )

        return pii_entities

    async def shutdown(self) -> None:
        self._model = None


def _resolve_device(device_str: str) -> str:
    if device_str == "auto":
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
        return "cpu"
    return device_str
