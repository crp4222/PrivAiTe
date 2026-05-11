from __future__ import annotations

import re

from presidio_analyzer import EntityRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

_LOC_GROUP = r"(?P<loc>[A-ZÀ-Ÿ][A-ZÀ-Ÿa-zà-ÿ\-]+(?:[\s\-]+[A-ZÀ-Ÿa-zà-ÿ\-]+){0,3})"

_RESIDENCE = (
    r"(?:[jJ]['']?habite|[jJ]e\s+vis|[jJ]['']?vis"
    r"|I\s+live|[lL]ives?\s+in|[rR]esident\s+(?:of|in|at)"
    r"|[wW]ohne?\s+in|[lL]ebt?\s+in"
    r"|[vV]ivo?\s+en|[aA]bito\s+a|[mM]oro\s+em|[wW]oon\s+in"
    r"|[bB]orn\s+in|[nN][ée]e?\s+[àa]|[gG]eboren\s+in|[nN]ascido\s+em|[nN]acido\s+en"
    r"|domicili[ée]\s+[àa]|[dD]emeurant\s+[àa]|[dD]omiciliado\s+en"
    r"|[sS]itu[ée]\s+[àa]|[bB]ased\s+in|[lL]ocated\s+in|[hH]eadquartered\s+in"
    r")\s+" + _LOC_GROUP
)

_STRONG_PREP = (
    r"(?:[àÀ]|[nN]ear|[vV]ia)\s+" + _LOC_GROUP
)

COMPILED = [
    re.compile(_RESIDENCE, re.UNICODE),
    re.compile(_STRONG_PREP, re.UNICODE),
]


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
