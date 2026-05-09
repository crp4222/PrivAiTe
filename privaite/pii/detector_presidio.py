from __future__ import annotations

import asyncio
import logging

from privaite.config.schema import PresidioDetectorConfig
from privaite.pii.detector_base import PIIDetector
from privaite.pii.entity import PIIEntity

logger = logging.getLogger("privaite.pii.detector_presidio")

REGEX_ENTITY_TYPES = {
    "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD", "IBAN_CODE",
    "IP_ADDRESS", "URL", "US_SSN", "UK_NHS", "CRYPTO",
}


class PresidioDetector(PIIDetector):
    def __init__(self, config: PresidioDetectorConfig, custom_patterns=None) -> None:
        self.config = config
        self._custom_patterns = custom_patterns or []
        self._analyzer = None

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

        nlp_config = {
            "nlp_engine_name": "spacy",
            "models": [],
        }

        for lang in self.config.languages:
            model = lang_model_map.get(lang)
            if model:
                nlp_config["models"].append({"lang_code": lang, "model_name": model})

        if not nlp_config["models"]:
            nlp_config["models"].append({"lang_code": "en", "model_name": "en_core_web_lg"})

        provider = NlpEngineProvider(nlp_configuration=nlp_config)
        nlp_engine = provider.create_engine()

        self._analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine,
            supported_languages=self.config.languages,
        )

        from privaite.pii.recognizer_context import ContextualNameRecognizer
        from privaite.pii.recognizer_fr_date import FrenchDateRecognizer

        for lang in self.config.languages:
            self._analyzer.registry.add_recognizer(
                ContextualNameRecognizer(supported_language=lang)
            )
            self._analyzer.registry.add_recognizer(
                FrenchDateRecognizer(supported_language=lang)
            )

        if self._custom_patterns:
            from privaite.pii.recognizer_custom import CustomPatternRecognizer

            for lang in self.config.languages:
                self._analyzer.registry.add_recognizer(
                    CustomPatternRecognizer(self._custom_patterns, supported_language=lang)
                )
            logger.info("Registered %d custom patterns", len(self._custom_patterns))

        logger.info(
            "Presidio analyzer initialized with languages: %s",
            self.config.languages,
        )

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        if self._analyzer is None:
            raise RuntimeError("PresidioDetector not initialized")

        all_results = []
        seen_spans: set[tuple[int, int, str]] = set()

        primary_lang = self.config.languages[0] if self.config.languages else "fr"

        allowed = set(self.config.entities) if self.config.entities else None

        for lang in self.config.languages:
            results = await asyncio.to_thread(
                self._analyzer.analyze,
                text=text,
                language=lang,
                entities=list(allowed) if allowed else None,
                score_threshold=self.config.score_threshold,
            )
            for result in results:
                if allowed and result.entity_type not in allowed:
                    continue

                is_spacy_ner = result.recognition_metadata.get(
                    "recognizer_name", ""
                ) == "SpacyRecognizer"
                if is_spacy_ner and lang != primary_lang:
                    continue

                span_key = (result.start, result.end, result.entity_type)
                if span_key not in seen_spans:
                    seen_spans.add(span_key)
                    all_results.append(result)

        pii_entities = []
        for result in all_results:
            pii_entities.append(
                PIIEntity(
                    entity_type=result.entity_type,
                    text=text[result.start : result.end],
                    start=result.start,
                    end=result.end,
                    score=result.score,
                    source="presidio",
                )
            )

        return pii_entities
