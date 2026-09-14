from __future__ import annotations

import re

from presidio_analyzer import EntityRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

from privaite.pii.recognizer_vocab import LanguagePatterns

_NAME_GROUP = r"(?P<name>[A-ZÀ-Ÿa-zà-ÿ][\w'-]*(?:\s+[A-ZÀ-Ÿa-zà-ÿ][\w'-]*){0,4})"
# Same group, first letter capitalised. Used behind the weak cues below, which
# are compiled case-sensitively for that reason.
_NAME_GROUP_CAPITALISED = r"(?P<name>[A-ZÀ-Ÿ][\w'-]*(?:\s+[A-ZÀ-Ÿa-zà-ÿ][\w'-]*){0,4})"

# Cues that announce a name explicitly ("my name is", "je m'appelle"). What
# follows one is a name whatever its casing, so these accept a lowercase name:
# chat users type "je m'appelle jean dupont".
_INTRO_STRONG = {
    "fr": r"je\s+m[']appelle|mon\s+nom\s+est|je\s+me\s+nomme|je\s+me\s+pr[ée]nomme",
    "en": r"my\s+name\s+is",
    "de": r"ich\s+hei[ßs]e|mein\s+Name\s+ist",
    "es": r"me\s+llamo|mi\s+nombre\s+es",
    "it": r"mi\s+chiamo|il\s+mio\s+nome\s+[èe]",
    "pt": r"meu\s+nome\s+[ée]|me\s+chamo",
    "nl": r"mijn\s+naam\s+is|ik\s+heet",
}

# Cues that merely mean "I am". They introduce a name often enough to be worth
# keeping, but they equally introduce an adjective: "I'm ready to go" reported
# "ready to go" as a person, "ik ben klaar" reported "klaar". Requiring a
# capitalised first token is what separates the two, and it costs nothing a
# strong cue does not already cover.
_INTRO_WEAK = {
    "fr": r"je\s+suis",
    "en": r"i[']m",
    "de": r"ich\s+bin",
    "es": r"soy",
    "it": r"sono",
    "pt": r"eu\s+sou",
    "nl": r"ik\s+ben",
}

_ALIAS = {
    "fr": r"appelez[- ]moi|on\s+m[']appelle",
    "en": r"call\s+me",
    "de": r"nennt?\s+mich",
    "es": r"ll[áa]mame",
    "it": r"chiamami",
    "pt": r"me\s+chame",
    "nl": r"noem\s+mij",
}

# Form labels, each in the language it is written in. A label shared by several
# languages ("Name", "Contact", "Nome") is listed under each of them.
_FORM_FIELD = {
    "fr": r"Nom|Prénom|Bénéficiaire|Destinataire|Contact|Patient",
    "en": r"Name|Beneficiary|Contact|Manager|Landlord|Tenant|Applicant|Patient",
    "de": r"Name|Vorname|Vermieter|Mieter|Patient",
    "es": r"Nombre|Denunciante|Testigo|Trabajador",
    "it": r"Nome|Emittente|Denunciante",
    "pt": r"Nome|Denunciante",
    "nl": r"Naam",
}

_LANGUAGES = frozenset(_INTRO_STRONG) | frozenset(_ALIAS) | frozenset(_FORM_FIELD)


def _patterns_for(lang: str) -> list[str]:
    """Cues are wrapped in a scoped case-insensitive group rather than compiled
    with a global IGNORECASE, so the capitalisation required after a weak cue is
    actually enforced."""
    patterns = []
    if lang in _INTRO_STRONG:
        patterns.append(rf"(?i:{_INTRO_STRONG[lang]})\s+{_NAME_GROUP}")
    if lang in _INTRO_WEAK:
        patterns.append(rf"(?i:{_INTRO_WEAK[lang]})\s+{_NAME_GROUP_CAPITALISED}")
    if lang in _ALIAS:
        patterns.append(rf"(?i:{_ALIAS[lang]})\s+{_NAME_GROUP}")
    if lang in _FORM_FIELD:
        patterns.append(rf"(?i:(?:{_FORM_FIELD[lang]})\s*:\s*){_NAME_GROUP}")
    return patterns


NAME_PATTERNS = LanguagePatterns({lang: _patterns_for(lang) for lang in _LANGUAGES})

# Stop words cut a match short, so they only ever reduce what is masked: unlike
# the cues above, unioning them across languages stays on the safe side.
STOP_WORDS = {
    "et",
    "ou",
    "mais",
    "donc",
    "car",
    "ni",
    "que",
    "qui",
    "de",
    "du",
    "des",
    "le",
    "la",
    "les",
    "un",
    "une",
    "mon",
    "ma",
    "mes",
    "son",
    "sa",
    "ses",
    "ce",
    "cette",
    "ces",
    "au",
    "aux",
    "en",
    "dans",
    "sur",
    "pour",
    "par",
    "avec",
    "sans",
    "sous",
    "vers",
    "chez",
    "and",
    "or",
    "but",
    "the",
    "a",
    "an",
    "my",
    "his",
    "her",
    "your",
    "und",
    "oder",
    "aber",
    "ich",
    "mein",
    "meine",
    "der",
    "die",
    "das",
    "ein",
    "eine",
    "ist",
    "bin",
    "wir",
    "sie",
    "er",
    "es",
    "y",
    "o",
    "pero",
    "mi",
    "su",
    "el",
    "los",
    "las",
    "con",
    "e",
    "ma",
    "il",
    "lo",
    "la",
    "gli",
    "le",
    "con",
    "per",
    "eu",
    "meu",
    "minha",
    "os",
    "as",
    "com",
    "em",
    "para",
    "ik",
    "mijn",
    "het",
    "een",
    "van",
    "met",
    "op",
    "voor",
}


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
        self._compiled = NAME_PATTERNS.for_language(supported_language)

    def load(self) -> None:
        pass

    def analyze(
        self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts | None = None
    ) -> list[RecognizerResult]:
        results = []
        normalized = text.translate(_APOSTROPHES)

        for pattern in self._compiled:
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
