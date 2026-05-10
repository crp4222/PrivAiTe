from __future__ import annotations

import re

from presidio_analyzer import EntityRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

_LOC_GROUP = r"(?P<loc>[A-ZÀ-Ÿ][A-ZÀ-Ÿa-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ][A-ZÀ-Ÿa-zà-ÿ\-]+){0,2})"

_CONTEXT = (
    r"(?:à|au|en|de|du|from|in|at|near|via)\s+" + _LOC_GROUP,
    r"(?:j['']habite|j['']vis|je\s+vis|I\s+live|lives?\s+in|wohne?\s+in"
    r"|born\s+in|née?\s+à|geboren\s+in)\s+" + _LOC_GROUP,
)

COMPILED = [re.compile(p, re.UNICODE) for p in _CONTEXT]


class ContextualLocationRecognizer(EntityRecognizer):
    def __init__(self, supported_language: str = "fr") -> None:
        super().__init__(
            supported_entities=["LOCATION"],
            supported_language=supported_language,
            name="ContextualLocationRecognizer",
        )

    def load(self) -> None:
        pass

    def analyze(
        self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts = None
    ) -> list[RecognizerResult]:
        results = []
        for pattern in COMPILED:
            for match in pattern.finditer(text):
                start = match.start("loc")
                end = match.end("loc")
                results.append(
                    RecognizerResult(
                        entity_type="LOCATION",
                        start=start,
                        end=end,
                        score=0.95,
                    )
                )
        return results
