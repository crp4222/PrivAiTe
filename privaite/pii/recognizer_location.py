from __future__ import annotations

from privaite.pii.recognizer_base import RegexSpanRecognizer
from privaite.pii.recognizer_vocab import LanguagePatterns

_LOC_GROUP = r"(?P<loc>[A-ZÀ-Ÿ][A-ZÀ-Ÿa-zà-ÿ\-]+(?:[\s\-]+[A-ZÀ-Ÿa-zà-ÿ\-]+){0,3})"

# Residence cues, each in the language its words come from. Registered on every
# configured language they over-masked: "via" is an Italian street, but in
# English it is "by way of", so "configured via Terraform" reported Terraform as
# a location. Same shape as issue #31, different vocabulary.
_RESIDENCE_BY_LANGUAGE = {
    "fr": (
        r"[jJ]['']?habite|[jJ]e\s+vis|[jJ]['']?vis"
        r"|[nN][ée]e?\s+[àa]|domicili[ée]\s+[àa]|[dD]emeurant\s+[àa]|[sS]itu[ée]\s+[àa]"
    ),
    "en": (
        r"I\s+live|[lL]ives?\s+in|[rR]esident\s+(?:of|in|at)"
        r"|[bB]orn\s+in|[bB]ased\s+in|[lL]ocated\s+in|[hH]eadquartered\s+in"
    ),
    "de": r"[wW]ohne?\s+in|[lL]ebt?\s+in|[gG]eboren\s+in",
    "es": r"[vV]ivo?\s+en|[nN]acido\s+en|[dD]omiciliado\s+en",
    "it": r"[aA]bito\s+a",
    "pt": r"[mM]oro\s+em|[nN]ascido\s+em",
    "nl": r"[wW]oon\s+in",
}

# One-word prepositions strong enough to carry a place on their own, which makes
# them the most language-sensitive cues of all.
_STRONG_PREP_BY_LANGUAGE = {
    "fr": r"[àÀ]",
    "en": r"[nN]ear",
    "it": r"[vV]ia",
}

LOCATION_PATTERNS = LanguagePatterns(
    {
        lang: [
            rf"(?:{_RESIDENCE_BY_LANGUAGE[lang]})\s+{_LOC_GROUP}",
            *(
                [rf"(?:{_STRONG_PREP_BY_LANGUAGE[lang]})\s+{_LOC_GROUP}"]
                if lang in _STRONG_PREP_BY_LANGUAGE
                else []
            ),
        ]
        for lang in _RESIDENCE_BY_LANGUAGE
    }
)


class ContextualLocationRecognizer(RegexSpanRecognizer):
    def __init__(self, supported_language: str = "fr") -> None:
        super().__init__(
            supported_entities=["LOCATION"],
            supported_language=supported_language,
            name="ContextualLocationRecognizer",
        )
        self._specs = [
            (c, "LOCATION", 0.95, "loc") for c in LOCATION_PATTERNS.for_language(supported_language)
        ]
