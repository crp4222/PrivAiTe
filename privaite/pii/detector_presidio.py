from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from privaite.config.schema import PresidioDetectorConfig
from privaite.pii.detector_base import PIIDetector
from privaite.pii.entity import PIIEntity

logger = logging.getLogger("privaite.pii.detector_presidio")


def build_recognizers(lang: str, custom_patterns: Sequence[Any] = ()) -> list[Any]:
    """The recognizers PrivAiTe adds on top of Presidio's own, for one language.

    Their supported entity types are exempt from the configured entity allowlist
    (see ``PresidioDetector.detect``), so anything reasoning about what Presidio
    can emit has to build the list here rather than restate it."""
    from privaite.pii.recognizer_context import ContextualNameRecognizer
    from privaite.pii.recognizer_fr_date import FrenchDateRecognizer
    from privaite.pii.recognizer_location import ContextualLocationRecognizer

    recognizers: list[Any] = [
        ContextualNameRecognizer(supported_language=lang),
        FrenchDateRecognizer(supported_language=lang),
        ContextualLocationRecognizer(supported_language=lang),
    ]
    if custom_patterns:
        from privaite.pii.recognizer_custom import CustomPatternRecognizer

        recognizers.append(CustomPatternRecognizer(list(custom_patterns), supported_language=lang))
    return recognizers


def builtin_recognizer_entity_types() -> set[str]:
    """Entity types the recognizers PrivAiTe always registers can emit.

    They are exempt from the Presidio entity allowlist, so the engine's
    `block_entities` producible check must count them: without this, blocking
    LOCATION on a Presidio-only allowlist config would be refused at boot as
    unenforceable while the contextual location recognizer can in fact emit it.
    """
    return {t for rec in build_recognizers("en") for t in rec.supported_entities}


class PresidioDetector(PIIDetector):
    def __init__(self, config: PresidioDetectorConfig, custom_patterns=None) -> None:
        self.config = config
        self._custom_patterns = custom_patterns or []
        self._analyzer: Any = None
        # The recognizers PrivAiTe registers itself, name -> entity types it can
        # emit. Filled by initialize() and used by detect() to exempt them from
        # the configured entity allowlist: a recognizer registered on every
        # analyzer must never be filtered out of every result.
        self._own_recognizers: dict[str, set[str]] = {}

    @property
    def name(self) -> str:
        return "presidio"

    async def initialize(self) -> None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        lang_model_map = {
            "en": "en_core_web_lg",
            "fr": "fr_core_news_md",
            "de": "de_core_news_md",
            "es": "es_core_news_md",
            "it": "it_core_news_md",
            "pt": "pt_core_news_md",
            "nl": "nl_core_news_md",
        }

        nlp_config: dict[str, Any] = {
            "nlp_engine_name": "spacy",
            "models": [],
        }

        unknown = [lang for lang in self.config.languages if lang not in lang_model_map]
        if unknown:
            # Silently dropping a language here used to pass init and then crash
            # EVERY request at detect() time (the analyze loop still used it).
            # Fail fast at startup with an actionable message instead.
            raise ValueError(
                f"No spaCy model mapping for language(s) {unknown}. "
                f"Supported: {sorted(lang_model_map)}"
            )

        for lang in self.config.languages:
            nlp_config["models"].append({"lang_code": lang, "model_name": lang_model_map[lang]})

        if not nlp_config["models"]:
            nlp_config["models"].append({"lang_code": "en", "model_name": "en_core_web_lg"})

        provider = NlpEngineProvider(nlp_configuration=nlp_config)
        nlp_engine = provider.create_engine()

        self._analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine,
            supported_languages=self.config.languages,
        )

        for lang in self.config.languages:
            for recognizer in build_recognizers(lang, self._custom_patterns):
                self._analyzer.registry.add_recognizer(recognizer)
                self._own_recognizers.setdefault(recognizer.name, set()).update(
                    recognizer.supported_entities
                )

        if self._custom_patterns:
            logger.info("Registered %d custom patterns", len(self._custom_patterns))

        logger.info(
            "Presidio analyzer initialized with languages: %s",
            self.config.languages,
        )

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        if self._analyzer is None:
            raise RuntimeError("PresidioDetector not initialized")

        primary_lang = self.config.languages[0] if self.config.languages else "fr"
        # The configured allowlist (set by the onnx/max presets) scopes
        # Presidio's OWN recognizers to the types it is strong at. The
        # recognizers we register are exempt: a custom pattern is an explicit
        # operator opt-in, and a built-in contextual recognizer whose only type
        # the allowlist drops (ContextualLocationRecognizer emits LOCATION) was
        # dead code under the DEFAULT preset, registered on every analyzer and
        # filtered out of every result. Their types have to be requested from
        # the analyzer for them to run at all, which also lets Presidio's own
        # recognizers emit those types, so the results are filtered back by
        # recognizer below: what the allowlist scoped stays scoped.
        configured = set(self.config.entities) if self.config.entities else None
        own_types = {t for types in self._own_recognizers.values() for t in types}
        requested = None if configured is None else configured | own_types

        all_results = []
        seen_spans: set[tuple[int, int, str]] = set()

        for lang in self.config.languages:
            results = await asyncio.to_thread(
                self._analyzer.analyze,
                text=text,
                language=lang,
                entities=list(requested) if requested else None,
                score_threshold=self.config.score_threshold,
            )
            for result in results:
                if requested and result.entity_type not in requested:
                    continue

                recognizer = result.recognition_metadata.get("recognizer_name", "")
                if (
                    configured is not None
                    and result.entity_type not in configured
                    and recognizer not in self._own_recognizers
                ):
                    continue
                if recognizer == "SpacyRecognizer" and lang != primary_lang:
                    continue

                span_key = (result.start, result.end, result.entity_type)
                if span_key not in seen_spans:
                    seen_spans.add(span_key)
                    all_results.append(result)

        pii_entities = []
        for result in all_results:
            span = text[result.start : result.end]

            recognizer = result.recognition_metadata.get("recognizer_name", "")
            if (
                result.entity_type == "PERSON"
                and recognizer == "SpacyRecognizer"
                and not _looks_like_name(span)
            ):
                continue

            pii_entities.append(
                PIIEntity(
                    entity_type=result.entity_type,
                    text=span,
                    start=result.start,
                    end=result.end,
                    score=result.score,
                    source="presidio",
                )
            )

        return pii_entities


def _looks_like_name(text: str) -> bool:
    if "\n" in text:
        return False
    words = text.split()
    if len(words) > 4 or len(words) < 2:
        return False
    if any(c in text for c in "+={}[]<>;/\\@~#"):
        return False
    particles = {"de", "da", "di", "du", "von", "van", "del", "der", "le", "la"}
    if not all(w[0].isupper() or w in particles for w in words):
        return False
    return True
