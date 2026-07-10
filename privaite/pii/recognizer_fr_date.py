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

MONTHS_ALL = f"{MONTHS_FR}|{MONTHS_DE}"

# Word boundaries on both edges: without the trailing \b, "3 maintenant" used to
# match "3 mai" and "1 marseillais" matched "1 mars", splitting ordinary words.
PATTERNS = [
    rf"(?P<date>(?<!\d)\d{{1,2}}\.?\s+(?:{MONTHS_ALL})\s+\d{{4}}(?!\d))",
    rf"\b(?P<date>(?:{MONTHS_ALL})\s+\d{{4}}(?!\d))",
    rf"(?P<date>(?<!\d)\d{{1,2}}\.?\s+(?:{MONTHS_ALL}))\b",
    r"(?P<date>(?:née?|born|geboren)\s+(?:le\s+|am\s+)?\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})(?!\d)",
]

COMPILED = [re.compile(p, re.IGNORECASE | re.UNICODE) for p in PATTERNS]


class FrenchDateRecognizer(RegexSpanRecognizer):
    def __init__(self, supported_language: str = "fr") -> None:
        super().__init__(
            supported_entities=["DATE_TIME"],
            supported_language=supported_language,
            name="FrenchDateRecognizer",
        )
        self._specs = [(c, "DATE_TIME", 0.85, "date") for c in COMPILED]
