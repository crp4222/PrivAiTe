from __future__ import annotations

import re

from presidio_analyzer import EntityRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

from privaite.config.schema import CustomPatternConfig


class CustomPatternRecognizer(EntityRecognizer):
    def __init__(
        self,
        patterns: list[CustomPatternConfig],
        supported_language: str = "fr",
    ) -> None:
        entity_types = list({p.entity_type for p in patterns})
        super().__init__(
            supported_entities=entity_types,
            supported_language=supported_language,
            name="CustomPatternRecognizer",
        )
        self._patterns = [
            (re.compile(p.pattern, re.IGNORECASE | re.UNICODE), p.entity_type, p.score)
            for p in patterns
        ]

    def load(self) -> None:
        pass

    def analyze(
        self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts = None
    ) -> list[RecognizerResult]:
        results = []
        for compiled, entity_type, score in self._patterns:
            for match in compiled.finditer(text):
                results.append(
                    RecognizerResult(
                        entity_type=entity_type,
                        start=match.start(),
                        end=match.end(),
                        score=score,
                    )
                )
        return results
