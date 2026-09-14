from __future__ import annotations

import re

from privaite.pii.recognizer_base import RegexSpanRecognizer

MONTHS_FR = (
    "janvier|février|fevrier|mars|avril|mai|juin|"
    "juillet|août|aout|septembre|octobre|novembre|décembre|decembre"
)

MONTHS_DE = (
    "Januar|Februar|März|Maerz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember"
)

# Month names are language-specific vocabulary, so each language gets its own.
# They used to be unioned and compiled once for every configured language, which
# masked a third of the English and Dutch calendar wherever a month happens to
# be spelled like the German one (April, August, September, November, juni,
# juli, oktober) and left the rest alone (issue #31).
MONTHS_BY_LANGUAGE = {"fr": MONTHS_FR, "de": MONTHS_DE}


# Word boundaries on both edges: without the trailing \b, "3 maintenant" used to
# match "3 mai" and "1 marseillais" matched "1 mars", splitting ordinary words.
def _month_patterns(months: str) -> list[str]:
    return [
        rf"(?P<date>(?<!\d)\d{{1,2}}\.?\s+(?:{months})\s+\d{{4}}(?!\d))",
        rf"\b(?P<date>(?:{months})\s+\d{{4}}(?!\d))",
        rf"(?P<date>(?<!\d)\d{{1,2}}\.?\s+(?:{months}))\b",
    ]


# Numeric and cued by a birth word, so no month vocabulary is involved: this one
# is language-neutral and stays active for every configured language, which is
# why scoping the whole recognizer by language would have been a regression.
NUMERIC_PATTERNS = [
    r"(?P<date>(?:née?|born|geboren)\s+(?:le\s+|am\s+)?\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})(?!\d)",
]

COMPILED_NUMERIC = [re.compile(p, re.IGNORECASE | re.UNICODE) for p in NUMERIC_PATTERNS]
COMPILED_MONTHS = {
    lang: [re.compile(p, re.IGNORECASE | re.UNICODE) for p in _month_patterns(months)]
    for lang, months in MONTHS_BY_LANGUAGE.items()
}


class FrenchDateRecognizer(RegexSpanRecognizer):
    def __init__(self, supported_language: str = "fr") -> None:
        super().__init__(
            supported_entities=["DATE_TIME"],
            supported_language=supported_language,
            name="FrenchDateRecognizer",
        )
        # Month patterns only for a language whose months these are; every
        # other language keeps the numeric birth-date pattern and nothing else.
        compiled = COMPILED_MONTHS.get(supported_language, []) + COMPILED_NUMERIC
        self._specs = [(c, "DATE_TIME", 0.85, "date") for c in compiled]
