from __future__ import annotations

import re

from privaite.pii.recognizer_base import RegexSpanRecognizer
from privaite.pii.recognizer_vocab import LanguagePatterns

MONTHS_FR = (
    "janvier|février|fevrier|mars|avril|mai|juin|"
    "juillet|août|aout|septembre|octobre|novembre|décembre|decembre"
)

MONTHS_DE = (
    "Januar|Februar|März|Maerz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember"
)


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

# Month names are vocabulary, so they follow the language: unioned, they masked a
# third of the English and Dutch calendar (issue #31).
DATE_PATTERNS = LanguagePatterns(
    {"fr": _month_patterns(MONTHS_FR), "de": _month_patterns(MONTHS_DE)},
    neutral=NUMERIC_PATTERNS,
    flags=re.IGNORECASE | re.UNICODE,
)


class FrenchDateRecognizer(RegexSpanRecognizer):
    def __init__(self, supported_language: str = "fr") -> None:
        super().__init__(
            supported_entities=["DATE_TIME"],
            supported_language=supported_language,
            name="FrenchDateRecognizer",
        )
        self._specs = [
            (c, "DATE_TIME", 0.85, "date") for c in DATE_PATTERNS.for_language(supported_language)
        ]
