from __future__ import annotations

import re

from presidio_analyzer import EntityRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

_LOC_GROUP = r"(?P<loc>[A-ZÀ-Ÿ][A-ZÀ-Ÿa-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ][A-ZÀ-Ÿa-zà-ÿ\-]+){0,2})"

_CONTEXT = (
    r"(?:[àÀ]|[aA]u|[eE]n|[dD][eua]|[dD]a|[eE]m|[fF]rom|[iI]n|[aA]t|[nN]ear|[vV]ia)\s+"
    + _LOC_GROUP,
    r"(?:[jJ][''](?:habite|vis)|[jJ]e\s+vis|I\s+live|[lL]ives?\s+in|[wW]ohne?\s+in"
    r"|[bB]orn\s+in|[nN][ée]e?\s+[àa]|[gG]eboren\s+in"
    r"|[vV]ivo?\s+en|[aA]bito\s+a|[mM]oro\s+em"
    r"|[nN]ascido\s+em|[nN]acido\s+en|[wW]oon\s+in"
    r")\s+" + _LOC_GROUP,
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
