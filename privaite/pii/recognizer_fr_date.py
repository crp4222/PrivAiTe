from __future__ import annotations

import re

from presidio_analyzer import EntityRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

MONTHS_FR = (
    "janvier|février|fevrier|mars|avril|mai|juin|"
    "juillet|août|aout|septembre|octobre|novembre|décembre|decembre"
)

PATTERNS = [
    rf"(?P<date>\d{{1,2}}\s+(?:{MONTHS_FR})\s+\d{{4}})",
    rf"(?P<date>(?:{MONTHS_FR})\s+\d{{4}})",
    rf"(?P<date>\d{{1,2}}\s+(?:{MONTHS_FR}))",
    r"(?P<date>(?:née?|born)\s+(?:le\s+)?\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})",
]

COMPILED = [re.compile(p, re.IGNORECASE | re.UNICODE) for p in PATTERNS]


class FrenchDateRecognizer(EntityRecognizer):
    def __init__(self, supported_language: str = "fr") -> None:
        super().__init__(
            supported_entities=["DATE_TIME"],
            supported_language=supported_language,
            name="FrenchDateRecognizer",
        )

    def load(self) -> None:
        pass

    def analyze(
        self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts = None
    ) -> list[RecognizerResult]:
        results = []

        for pattern in COMPILED:
            for match in pattern.finditer(text):
                start = match.start("date")
                end = match.end("date")

                results.append(
                    RecognizerResult(
                        entity_type="DATE_TIME",
                        start=start,
                        end=end,
                        score=0.85,
                    )
                )

        return results
