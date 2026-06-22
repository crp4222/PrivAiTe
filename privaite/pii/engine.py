from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from privaite.config.schema import PIIConfig
from privaite.pii.anonymizer import Anonymizer
from privaite.pii.deanonymizer import DeAnonymizer
from privaite.pii.detector_base import PIIDetector
from privaite.pii.entity import PIIEntity, merge_entities
from privaite.pii.mapping import PIIMapping

logger = logging.getLogger("privaite.pii.engine")

_KNOWN_MEDIA_PART_TYPES = frozenset(
    {"image_url", "image", "input_audio", "audio", "video", "file", "document", "refusal"}
)


class UnsupportedContentError(Exception):
    """Raised in strict mode when a payload shape cannot be inspected for PII."""


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

            detector: PIIDetector = PresidioDetector(
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
        language = self._language()
        anonymized_messages = []

        for message in messages:
            role = message.get("role", "")

            if self.config.passthrough.system_messages and role == "system":
                anonymized_messages.append(dict(message))
                continue

            new_msg = dict(message)

            if "content" in message:
                new_msg["content"] = await self._anonymize_content(
                    message["content"], mapping, language
                )

            if not self.config.passthrough.tool_calls and message.get("tool_calls"):
                new_msg["tool_calls"] = await self._anonymize_tool_calls(
                    message["tool_calls"], mapping, language
                )

            # Legacy (pre-tool_calls) OpenAI function calling.
            if not self.config.passthrough.tool_calls and message.get("function_call"):
                new_msg["function_call"] = await self._anonymize_function_call(
                    message["function_call"], mapping, language
                )

            anonymized_messages.append(new_msg)

        return anonymized_messages, mapping

    def _language(self) -> str:
        langs = self.config.detectors.presidio.languages
        return langs[0] if langs else "en"

    async def _anonymize_text(
        self, text: Any, mapping: PIIMapping, language: str
    ) -> Any:
        if not text or not isinstance(text, str):
            return text
        entities = await self._detect_all(text, language)
        return self.anonymizer.anonymize(text, entities, mapping)

    async def _anonymize_content(
        self, content: Any, mapping: PIIMapping, language: str
    ) -> Any:
        if isinstance(content, str):
            return await self._anonymize_text(content, mapping, language)
        if content is None:
            return content

        # Multimodal parts: [{"type": "text", "text": ...}, {"type": "image_url", ...}]
        if isinstance(content, list):
            new_parts: list[Any] = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    new_part = dict(part)
                    new_part["text"] = await self._anonymize_text(
                        part["text"], mapping, language
                    )
                    new_parts.append(new_part)
                elif self._is_known_media_part(part):
                    new_parts.append(part)
                else:
                    self._reject_if_strict(part)
                    new_parts.append(part)
            return new_parts

        self._reject_if_strict(content)
        return content

    def _is_known_media_part(self, part: Any) -> bool:
        # A media part (image/audio/...) carries no text to scrub, so passing it
        # through is safe even in strict mode.
        return isinstance(part, dict) and part.get("type") in _KNOWN_MEDIA_PART_TYPES

    def _reject_if_strict(self, value: Any) -> None:
        if self.config.strict:
            raise UnsupportedContentError(
                f"strict mode: content of type {type(value).__name__} cannot be inspected"
            )

    async def _anonymize_tool_calls(
        self, tool_calls: Any, mapping: PIIMapping, language: str
    ) -> Any:
        if not isinstance(tool_calls, list):
            return tool_calls

        new_calls: list[Any] = []
        for call in tool_calls:
            if not isinstance(call, dict):
                new_calls.append(call)
                continue
            new_call = dict(call)
            function = call.get("function")
            if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                new_function = dict(function)
                new_function["arguments"] = await self._anonymize_arguments(
                    function["arguments"], mapping, language
                )
                new_call["function"] = new_function
            new_calls.append(new_call)
        return new_calls

    async def _anonymize_function_call(
        self, function_call: Any, mapping: PIIMapping, language: str
    ) -> Any:
        if not isinstance(function_call, dict) or not isinstance(
            function_call.get("arguments"), str
        ):
            return function_call
        new_call = dict(function_call)
        new_call["arguments"] = await self._anonymize_arguments(
            function_call["arguments"], mapping, language
        )
        return new_call

    async def _anonymize_arguments(
        self, arguments: str, mapping: PIIMapping, language: str
    ) -> str:
        try:
            parsed = json.loads(arguments)
        except (json.JSONDecodeError, ValueError):
            # Arguments are not valid JSON: anonymize the raw string instead.
            return await self._anonymize_text(arguments, mapping, language)
        walked = await self._walk_anonymize(parsed, mapping, language)
        return json.dumps(walked, ensure_ascii=False)

    async def _walk_anonymize(
        self, value: Any, mapping: PIIMapping, language: str
    ) -> Any:
        if isinstance(value, str):
            return await self._anonymize_text(value, mapping, language)
        if isinstance(value, dict):
            result: dict[Any, Any] = {}
            for key, item in value.items():
                result[key] = await self._walk_anonymize(item, mapping, language)
            return result
        if isinstance(value, list):
            return [await self._walk_anonymize(item, mapping, language) for item in value]
        return value

    async def process_response(self, content: str, mapping: PIIMapping) -> str:
        if not self.config.deanonymization.enabled:
            return content
        return self.deanonymizer.deanonymize(content, mapping)

    async def process_response_tool_calls(
        self, tool_calls: Any, mapping: PIIMapping | None
    ) -> Any:
        if (
            not self.config.deanonymization.enabled
            or mapping is None
            or mapping.is_empty
            or not isinstance(tool_calls, list)
        ):
            return tool_calls

        new_calls: list[Any] = []
        for call in tool_calls:
            if not isinstance(call, dict):
                new_calls.append(call)
                continue
            new_call = dict(call)
            function = call.get("function")
            if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                new_function = dict(function)
                new_function["arguments"] = self._deanonymize_arguments(
                    function["arguments"], mapping
                )
                new_call["function"] = new_function
            new_calls.append(new_call)
        return new_calls

    async def process_response_function_call(
        self, function_call: Any, mapping: PIIMapping | None
    ) -> Any:
        if (
            not self.config.deanonymization.enabled
            or mapping is None
            or mapping.is_empty
            or not isinstance(function_call, dict)
            or not isinstance(function_call.get("arguments"), str)
        ):
            return function_call
        new_call = dict(function_call)
        new_call["arguments"] = self._deanonymize_arguments(
            function_call["arguments"], mapping
        )
        return new_call

    def _deanonymize_arguments(self, arguments: str, mapping: PIIMapping) -> str:
        try:
            parsed = json.loads(arguments)
        except (json.JSONDecodeError, ValueError):
            return self.deanonymizer.deanonymize(arguments, mapping)
        return json.dumps(self._walk_deanonymize(parsed, mapping), ensure_ascii=False)

    def _walk_deanonymize(self, value: Any, mapping: PIIMapping) -> Any:
        if isinstance(value, str):
            return self.deanonymizer.deanonymize(value, mapping)
        if isinstance(value, dict):
            return {k: self._walk_deanonymize(v, mapping) for k, v in value.items()}
        if isinstance(value, list):
            return [self._walk_deanonymize(v, mapping) for v in value]
        return value

    async def _detect_all(
        self, text: str, language: str = "en"
    ) -> list[PIIEntity]:
        if not self.detectors:
            return []

        tasks = [detector.detect(text, language) for detector in self.detectors]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_entities: list[PIIEntity] = []
        for result in results:
            if isinstance(result, BaseException):
                logger.error("Detector failed: %s", result)
                raise result
            all_entities.extend(result)

        return merge_entities(
            all_entities,
            strategy=self.config.merge_strategy,
            overlap_resolution=self.config.overlap_resolution,
            source_text=text,
        )
