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


class PIIProcessingError(RuntimeError):
    """A safe error for an unexpected PII-processing failure.

    Detector and anonymizer exceptions can include the text they were given. Do
    not let those exceptions cross a request boundary: integration hosts commonly
    log unhandled exceptions, which would turn a fail-closed request into a PII
    log leak.
    """

    def __init__(self) -> None:
        super().__init__("PII processing failed")


class PIIBlockedError(Exception):
    """Raised when a request contains a PII type configured to be blocked outright
    (pii.block_entities). Carries the entity TYPES that triggered the block, never
    the underlying PII values."""

    def __init__(self, entity_types: set[str]) -> None:
        self.entity_types = sorted(entity_types)
        super().__init__(
            "request blocked: contains disallowed PII type(s): " + ", ".join(self.entity_types)
        )


class PIIEngine:
    def __init__(self, config: PIIConfig) -> None:
        self.config = config
        self.detectors: list[PIIDetector] = []
        self.anonymizer = Anonymizer(config.anonymization)
        self.deanonymizer = DeAnonymizer(config.deanonymization)
        # Types that cause a hard reject (empty = default: mask everything).
        self._blocked: set[str] = set(config.block_entities or [])
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

        if self.config.detectors.gliner.enabled:
            from privaite.pii.detector_gliner import GlinerDetector

            detector = GlinerDetector(self.config.detectors.gliner)
            await detector.initialize()
            self.detectors.append(detector)
            logger.info("GLiNER detector initialized")

        if self.config.enabled and not self.detectors:
            # pii.enabled=true with every detector switched off would serve every
            # request with detection silently doing nothing. Refuse to start; an
            # operator who wants passthrough must set pii.enabled=false explicitly.
            raise ValueError(
                "pii.enabled is true but no detector is enabled: detection would "
                "be silently off. Enable a detector or set pii.enabled: false"
            )

        if self.config.merge_strategy == "intersection" and len(self.detectors) < 2:
            # Intersection keeps only spans confirmed by >=2 detectors. With fewer
            # detectors NOTHING can ever be confirmed, so every request would be
            # forwarded with all its PII raw. Refuse to start instead.
            raise ValueError(
                "merge_strategy 'intersection' requires at least 2 enabled "
                f"detectors, got {len(self.detectors)}: it would silently disable "
                "all PII detection"
            )

        self._ready = True
        logger.info("PII engine ready with %d detector(s)", len(self.detectors))

    async def warmup(self) -> None:
        """Exercise the detectors once so the first real request does not pay
        cold-start cost (onnxruntime graph warm, spaCy first-parse JIT). Models
        are already loaded by initialize(); this just runs one throwaway pass."""
        if not self.detectors:
            return
        try:
            await self.process_request([{"role": "user", "content": "warm up jean@example.com"}])
        except Exception:  # pragma: no cover - warmup is best-effort, never fatal
            # Never include an exception or traceback here: a detector may have
            # copied its input into the exception message.
            logger.warning("PII engine warmup pass failed (non-fatal)")

    async def shutdown(self) -> None:
        for detector in self.detectors:
            await detector.shutdown()
        self._ready = False

    async def process_request(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], PIIMapping]:
        """Anonymize a request, exposing only safe failures to callers.

        Both in-process integrations let this exception propagate to their host,
        which may log it. Keep expected policy errors intact, but deliberately
        discard all other exception details because they may contain raw input.
        """
        try:
            return await self._process_request(messages)
        except (PIIBlockedError, UnsupportedContentError, PIIProcessingError):
            raise
        except Exception:
            logger.error("PII request processing failed; request will be blocked")
            raise PIIProcessingError() from None

    async def _process_request(
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

    async def process_request_value(self, value: Any, mapping: PIIMapping) -> Any:
        """Anonymize one auxiliary request-side value (a string, or a JSON-like
        dict/list structure whose string and long-numeric leaves are scrubbed)
        into an existing per-request mapping.

        Used for text-bearing request fields outside ``messages`` that would
        otherwise be forwarded verbatim by the kwargs passthrough: chat
        ``prediction.content`` (predicted outputs carry the client's current
        document), ``web_search_options.user_location``, and the completions
        ``suffix``. These are request inputs, so they only need scrubbing, never
        a dedicated restore path. Same single choke point (``_anonymize_text``,
        so the block gate applies) and same safe-error policy as
        ``process_request``.
        """
        try:
            return await self._walk_anonymize(value, mapping, self._language())
        except (PIIBlockedError, UnsupportedContentError, PIIProcessingError):
            raise
        except Exception:
            logger.error("PII request processing failed; request will be blocked")
            raise PIIProcessingError() from None

    async def _anonymize_text(self, text: Any, mapping: PIIMapping, language: str) -> Any:
        if not text or not isinstance(text, str):
            return text
        try:
            entities = await self._detect_all(text, language)
            # Hard policy gate: if any detected type is blocked, reject the whole
            # request before anonymizing (nothing is forwarded). This single choke
            # point covers message content, multimodal text, and tool-call arguments.
            if self._blocked:
                hit = {e.entity_type for e in entities if e.entity_type in self._blocked}
                if hit:
                    raise PIIBlockedError(hit)
            return self.anonymizer.anonymize(text, entities, mapping)
        except (PIIBlockedError, PIIProcessingError):
            raise
        except Exception:
            # The anonymizer receives raw text too, so it must not expose an
            # implementation error (or its traceback) to a host logger.
            logger.error("PII anonymization failed; request will be blocked")
            raise PIIProcessingError() from None

    async def _anonymize_content(self, content: Any, mapping: PIIMapping, language: str) -> Any:
        if isinstance(content, str):
            return await self._anonymize_text(content, mapping, language)
        if content is None:
            return content

        # Multimodal parts: [{"type": "text", "text": ...}, {"type": "image_url", ...}]
        if isinstance(content, list):
            new_parts: list[Any] = []
            for part in content:
                if isinstance(part, str):
                    # A bare string inside a content list is user text: scan it.
                    # Skipping it let PII (and blocked types) through whenever a
                    # client sent content as a plain string list.
                    new_parts.append(await self._anonymize_text(part, mapping, language))
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    new_part = dict(part)
                    new_part["text"] = await self._anonymize_text(part["text"], mapping, language)
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

    async def _anonymize_arguments(self, arguments: str, mapping: PIIMapping, language: str) -> str:
        try:
            parsed = json.loads(arguments)
        except (json.JSONDecodeError, ValueError):
            # Arguments are not valid JSON: anonymize the raw string instead.
            return await self._anonymize_text(arguments, mapping, language)
        walked = await self._walk_anonymize(parsed, mapping, language)
        return json.dumps(walked, ensure_ascii=False)

    async def _walk_anonymize(self, value: Any, mapping: PIIMapping, language: str) -> Any:
        if isinstance(value, str):
            return await self._anonymize_text(value, mapping, language)
        # bool is a subclass of int; leave true/false alone.
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            # A credit card or SSN sent as a bare JSON number would otherwise skip
            # detection AND the block gate entirely. Scan the digits; only if PII is
            # actually found does the leaf become a (masked) string, so ordinary
            # numbers keep their type. The block gate still fires inside
            # _anonymize_text for a blocked type. Numbers with fewer than 7 digits
            # are skipped outright: the shortest realistic numeric PII (phone
            # fragments, SSN, cards) is longer, and scanning every count/year/
            # coordinate would run the full detector stack per leaf and risk
            # rewriting schema-typed numbers on a false positive.
            as_text = str(value)
            if sum(ch.isdigit() for ch in as_text) < 7:
                return value
            anonymized = await self._anonymize_text(as_text, mapping, language)
            return anonymized if anonymized != as_text else value
        if isinstance(value, dict):
            result: dict[Any, Any] = {}
            for key, item in value.items():
                result[key] = await self._walk_anonymize(item, mapping, language)
            return result
        if isinstance(value, list):
            return [await self._walk_anonymize(item, mapping, language) for item in value]
        return value

    async def inspect_text(self, text: str) -> list[PIIEntity]:
        """Detections for one text, exactly as the request path would see them.

        Returns the MERGED entity list (same merge strategy and overlap
        resolution as `_anonymize_text`), so the result corresponds 1:1 with
        what would be replaced. Read-only: nothing is anonymized, no mapping is
        created, the block gate is not raised (callers report would-block types
        instead). Powers the dry-run /v1/pii/inspect endpoint.
        """
        if not text or not isinstance(text, str):
            return []
        try:
            return await self._detect_all(text, self._language())
        except PIIProcessingError:
            raise
        except Exception:
            logger.error("PII inspection failed")
            raise PIIProcessingError() from None

    async def process_response(self, content: str, mapping: PIIMapping) -> str:
        if not self.config.deanonymization.enabled:
            return content
        try:
            return self.deanonymizer.deanonymize(content, mapping)
        except Exception:
            # A restore failure can include the original value from the mapping.
            logger.error("PII response deanonymization failed")
            raise PIIProcessingError() from None

    async def process_response_tool_calls(self, tool_calls: Any, mapping: PIIMapping | None) -> Any:
        if (
            not self.config.deanonymization.enabled
            or mapping is None
            or mapping.is_empty
            or not isinstance(tool_calls, list)
        ):
            return tool_calls

        try:
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
        except Exception:
            logger.error("PII tool-call deanonymization failed")
            raise PIIProcessingError() from None

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
        try:
            new_call = dict(function_call)
            new_call["arguments"] = self._deanonymize_arguments(function_call["arguments"], mapping)
            return new_call
        except Exception:
            logger.error("PII function-call deanonymization failed")
            raise PIIProcessingError() from None

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

    async def _detect_all(self, text: str, language: str = "en") -> list[PIIEntity]:
        if not self.detectors:
            return []

        tasks = [detector.detect(text, language) for detector in self.detectors]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            logger.error("PII detector execution failed; request will be blocked")
            raise PIIProcessingError() from None

        all_entities: list[PIIEntity] = []
        for result in results:
            if isinstance(result, BaseException):
                # Do not log or re-raise the original exception: detector
                # libraries (and custom detectors) may put their input text in
                # the message. The safe error keeps all callers fail-closed.
                logger.error("PII detector failed; request will be blocked")
                raise PIIProcessingError() from None
            all_entities.extend(result)

        try:
            return merge_entities(
                all_entities,
                strategy=self.config.merge_strategy,
                overlap_resolution=self.config.overlap_resolution,
                source_text=text,
            )
        except Exception:
            # merge_entities receives source_text for overlap resolution; keep
            # its errors safe for the same reason as detector errors.
            logger.error("PII detection result processing failed; request will be blocked")
            raise PIIProcessingError() from None
