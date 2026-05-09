from __future__ import annotations

import asyncio
import logging
from typing import Any

from privaite.config.schema import PIIConfig
from privaite.pii.anonymizer import Anonymizer
from privaite.pii.deanonymizer import DeAnonymizer
from privaite.pii.detector_base import PIIDetector
from privaite.pii.entity import PIIEntity, merge_entities
from privaite.pii.mapping import PIIMapping

logger = logging.getLogger("privaite.pii.engine")


class PIIEngine:
    def __init__(self, config: PIIConfig) -> None:
        self.config = config
        self.detectors: list[PIIDetector] = []
        self.anonymizer = Anonymizer(config.anonymization)
        self.deanonymizer = DeAnonymizer(config.deanonymization)
        self._ready = False

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def initialize(self) -> None:
        if self.config.detectors.presidio.enabled:
            from privaite.pii.detector_presidio import PresidioDetector

            detector = PresidioDetector(
                self.config.detectors.presidio,
                custom_patterns=self.config.custom_patterns,
            )
            await detector.initialize()
            self.detectors.append(detector)
            logger.info("Presidio detector initialized")

        if self.config.detectors.bert_ner.enabled:
            from privaite.pii.detector_bert_ner import BertNERDetector

            detector = BertNERDetector(self.config.detectors.bert_ner)
            await detector.initialize()
            self.detectors.append(detector)
            logger.info("BERT NER detector initialized")

        if self.config.detectors.mlmodel.enabled:
            from privaite.pii.detector_mlmodel import MLModelDetector

            detector = MLModelDetector(self.config.detectors.mlmodel)
            await detector.initialize()
            self.detectors.append(detector)
            logger.info("ML model detector initialized")

        if self.config.detectors.onnx.enabled:
            from privaite.pii.detector_onnx import OnnxPrivacyFilterDetector

            detector = OnnxPrivacyFilterDetector(self.config.detectors.onnx)
            await detector.initialize()
            self.detectors.append(detector)
            logger.info("ONNX privacy-filter detector initialized")

        self._ready = True
        logger.info("PII engine ready with %d detector(s)", len(self.detectors))

    async def shutdown(self) -> None:
        for detector in self.detectors:
            await detector.shutdown()
        self._ready = False

    async def process_request(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], PIIMapping]:
        mapping = PIIMapping()
        anonymized_messages = []

        for message in messages:
            role = message.get("role", "")
            content = message.get("content", "")

            if self.config.passthrough.system_messages and role == "system":
                anonymized_messages.append(dict(message))
                continue

            if not content or not isinstance(content, str):
                anonymized_messages.append(dict(message))
                continue

            langs = self.config.detectors.presidio.languages
            language = langs[0] if langs else "en"
            entities = await self._detect_all(content, language)
            anonymized_content = self.anonymizer.anonymize(content, entities, mapping)

            new_msg = dict(message)
            new_msg["content"] = anonymized_content
            anonymized_messages.append(new_msg)

        return anonymized_messages, mapping

    async def process_response(self, content: str, mapping: PIIMapping) -> str:
        if not self.config.deanonymization.enabled:
            return content
        return self.deanonymizer.deanonymize(content, mapping)

    async def _detect_all(
        self, text: str, language: str = "en"
    ) -> list[PIIEntity]:
        if not self.detectors:
            return []

        tasks = [detector.detect(text, language) for detector in self.detectors]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_entities: list[PIIEntity] = []
        for result in results:
            if isinstance(result, Exception):
                logger.error("Detector failed: %s", result)
                raise result
            all_entities.extend(result)

        return merge_entities(
            all_entities,
            strategy=self.config.merge_strategy,
            overlap_resolution=self.config.overlap_resolution,
            source_text=text,
        )
