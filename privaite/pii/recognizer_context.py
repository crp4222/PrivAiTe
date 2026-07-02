from __future__ import annotations

import re

from presidio_analyzer import EntityRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

_NAME_GROUP = r"(?P<name>[A-ZÀ-Ÿa-zà-ÿ][\w'-]*(?:\s+[A-ZÀ-Ÿa-zà-ÿ][\w'-]*){0,4})"

_INTRO = (
    r"je\s+m['']appelle|my\s+name\s+is|i['']m|je\s+suis"
    r"|mon\s+nom\s+est|je\s+me\s+nomme|je\s+me\s+pr[ée]nomme"
    r"|ich\s+hei[ßs]e|ich\s+bin|mein\s+Name\s+ist"
    r"|me\s+llamo|mi\s+nombre\s+es|soy"
    r"|mi\s+chiamo|il\s+mio\s+nome\s+[èe]|sono"
    r"|meu\s+nome\s+[ée]|eu\s+sou|me\s+chamo"
    r"|ik\s+ben|mijn\s+naam\s+is|ik\s+heet"
)
_ALIAS = (
    r"appelez[- ]moi|call\s+me|on\s+m['']appelle|nennt?\s+mich"
    r"|ll[áa]mame|chiamami|me\s+chame|noem\s+mij"
)

_FORM_FIELD = (
    r"(?:Nom|Name|Prénom|Vorname|Nombre|Nome|Naam"
    r"|Patient|Bénéficiaire|Beneficiary|Contact|Manager"
    r"|Destinataire|Emittente|Landlord|Tenant|Applicant"
    r"|Vermieter|Mieter|Denunciante|Testigo|Trabajador)\s*:\s*"
)

PATTERNS = [
    rf"(?:{_INTRO})\s+{_NAME_GROUP}",
    rf"(?:{_ALIAS})\s+{_NAME_GROUP}",
    rf"{_FORM_FIELD}{_NAME_GROUP}",
]

STOP_WORDS = {
    "et", "ou", "mais", "donc", "car", "ni", "que", "qui", "de", "du",
    "des", "le", "la", "les", "un", "une", "mon", "ma", "mes", "son",
    "sa", "ses", "ce", "cette", "ces", "au", "aux", "en", "dans",
    "sur", "pour", "par", "avec", "sans", "sous", "vers", "chez",
    "and", "or", "but", "the", "a", "an", "my", "his", "her", "your",
    "und", "oder", "aber", "ich", "mein", "meine", "der", "die", "das",
    "ein", "eine", "ist", "bin", "wir", "sie", "er", "es",
    "y", "o", "pero", "mi", "su", "el", "los", "las", "con",
    "e", "ma", "il", "lo", "la", "gli", "le", "con", "per",
    "eu", "meu", "minha", "os", "as", "com", "em", "para",
    "ik", "mijn", "het", "een", "van", "met", "op", "voor",
}

COMPILED = [re.compile(p, re.IGNORECASE | re.UNICODE) for p in PATTERNS]

# macOS/iOS and most chat UIs emit a typographic apostrophe (U+2019) for "'".
# Map the common variants to a straight quote so intro patterns like
# "je m'appelle X" still match. The mapping is length-preserving, so the regex
# offsets keep pointing at the right span in the original text.
_APOSTROPHES = {ord(c): "'" for c in "’‘ʼ＇´`"}


def _trim_name(name: str) -> str:
    """Cut the match at the first stop word, returning the kept prefix VERBATIM
    (original spacing included). A split/rejoin here once shortened "Jean  Dupont"
    (double space) by one character, and `end = start + len(name)` then left the
    final character of the real name unmasked."""
    kept_end = 0
    for match in re.finditer(r"\S+", name):
        if match.group().lower() in STOP_WORDS:
            break
        kept_end = match.end()
    return name[:kept_end]


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
        self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts | None = None
    ) -> list[RecognizerResult]:
        results = []
        normalized = text.translate(_APOSTROPHES)

        for pattern in COMPILED:
            for match in pattern.finditer(normalized):
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
