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

NER_REMAP = {
    "MISC": "PERSON",
}

FRENCH_COMMON_WORDS = {
    "le", "la", "les", "de", "du", "des", "un", "une", "mon", "ma", "mes",
    "ton", "ta", "tes", "son", "sa", "ses", "ce", "cette", "ces",
    "est", "et", "ou", "je", "tu", "il", "elle", "nous", "vous", "ils",
    "elles", "on", "ne", "pas", "que", "qui", "en", "au", "aux",
    "bonjour", "merci", "oui", "non", "monsieur", "madame",
}

TECHNICAL_WORDS = {
    "iban", "carte", "email", "numéro", "sécurité", "sociale", "adresse",
    "téléphone", "résumer", "tableau", "ip", "url", "http", "https", "www",
    "mot", "passe", "wifi", "code", "postal", "bancaire", "fiscal",
    "python", "java", "javascript", "typescript", "react", "rust", "golang",
    "linux", "macos", "docker", "kubernetes", "api", "sql", "html", "css",
    "json", "xml", "github", "gitlab", "npm", "pip",
}


class PresidioDetector(PIIDetector):
    def __init__(self, config: PresidioDetectorConfig) -> None:
        self.config = config
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

        from presidio_analyzer.predefined_recognizers import SpacyRecognizer

        existing_fr = [
            r for r in self._analyzer.registry.recognizers
            if isinstance(r, SpacyRecognizer) and r.supported_language == "fr"
        ]
        for r in existing_fr:
            self._analyzer.registry.remove_recognizer(r)

        custom_fr = SpacyRecognizer(
            supported_language="fr",
            supported_entities=[
                "PERSON", "LOCATION", "DATE_TIME", "NRP", "ORGANIZATION", "MISC",
            ],
        )
        self._analyzer.registry.add_recognizer(custom_fr)

        from privaite.pii.recognizer_context import ContextualNameRecognizer
        from privaite.pii.recognizer_fr_date import FrenchDateRecognizer

        for lang in self.config.languages:
            self._analyzer.registry.add_recognizer(ContextualNameRecognizer(supported_language=lang))
            self._analyzer.registry.add_recognizer(FrenchDateRecognizer(supported_language=lang))

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
        fetch_entities = list(allowed | set(NER_REMAP.keys())) if allowed else None

        for lang in self.config.languages:
            results = await asyncio.to_thread(
                self._analyzer.analyze,
                text=text,
                language=lang,
                entities=fetch_entities,
                score_threshold=self.config.score_threshold,
            )
            for result in results:
                if lang != primary_lang and result.entity_type not in REGEX_ENTITY_TYPES:
                    continue

                entity_type = result.entity_type
                if entity_type in NER_REMAP:
                    entity_type = NER_REMAP[entity_type]

                if allowed and entity_type not in allowed:
                    continue

                span_text = text[result.start : result.end].strip().lower()
                if entity_type in ("PERSON", "LOCATION") and self._is_noise(span_text):
                    continue

                span_key = (result.start, result.end, entity_type)
                if span_key not in seen_spans:
                    seen_spans.add(span_key)
                    all_results.append((result, entity_type))

        pii_entities = []
        for result, entity_type in all_results:
            pii_entities.append(
                PIIEntity(
                    entity_type=entity_type,
                    text=text[result.start : result.end],
                    start=result.start,
                    end=result.end,
                    score=result.score,
                    source="presidio",
                )
            )

        return pii_entities

    def _is_noise(self, text: str) -> bool:
        words = text.lower().split()
        word_set = set(words)
        if word_set.issubset(FRENCH_COMMON_WORDS | TECHNICAL_WORDS):
            return True
        if len(words) == 1 and len(words[0]) <= 2:
            return True
        return False
