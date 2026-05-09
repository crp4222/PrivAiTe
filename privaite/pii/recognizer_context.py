from __future__ import annotations

import re

from presidio_analyzer import EntityRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

_NAME_GROUP = r"(?P<name>[A-ZÀ-Ÿa-zà-ÿ][\w'-]*(?:\s+[A-ZÀ-Ÿa-zà-ÿ][\w'-]*){0,4})"

_INTRO = (
    r"je\s+m['']appelle|my\s+name\s+is|i['']m|je\s+suis"
    r"|mon\s+nom\s+est|je\s+me\s+nomme|je\s+me\s+pr[ée]nomme"
    r"|ich\s+hei[ßs]e|ich\s+bin|mein\s+Name\s+ist"
)
_ALIAS = r"appelez[- ]moi|call\s+me|on\s+m['']appelle|nennt?\s+mich"

PATTERNS = [
    rf"(?:{_INTRO})\s+{_NAME_GROUP}",
    rf"(?:{_ALIAS})\s+{_NAME_GROUP}",
]

STOP_WORDS = {
    "et", "ou", "mais", "donc", "car", "ni", "que", "qui", "de", "du",
    "des", "le", "la", "les", "un", "une", "mon", "ma", "mes", "son",
    "sa", "ses", "ce", "cette", "ces", "au", "aux", "en", "dans",
    "sur", "pour", "par", "avec", "sans", "sous", "vers", "chez",
    "and", "or", "but", "the", "a", "an", "my", "his", "her", "your",
    "und", "oder", "aber", "ich", "mein", "meine", "der", "die", "das",
    "ein", "eine", "ist", "bin", "wir", "sie", "er", "es",
}

COMPILED = [re.compile(p, re.IGNORECASE | re.UNICODE) for p in PATTERNS]


def _trim_name(name: str) -> str:
    words = name.split()
    trimmed = []
    for w in words:
        if w.lower() in STOP_WORDS:
            break
        trimmed.append(w)
    return " ".join(trimmed)


class ContextualNameRecognizer(EntityRecognizer):
    def __init__(self, supported_language: str = "fr") -> None:
        super().__init__(
            supported_entities=["PERSON"],
            supported_language=supported_language,
            name="ContextualNameRecognizer",
        )

    def load(self) -> None:
        pass

    def analyze(
        self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts = None
    ) -> list[RecognizerResult]:
        results = []

        for pattern in COMPILED:
            for match in pattern.finditer(text):
                raw_name = match.group("name")
                name = _trim_name(raw_name)
                if not name or len(name) < 2:
                    continue

                start = match.start("name")
                end = start + len(name)

                results.append(
                    RecognizerResult(
                        entity_type="PERSON",
                        start=start,
                        end=end,
                        score=0.9,
                    )
                )

        return results
