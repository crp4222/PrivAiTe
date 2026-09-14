"""Language-scoped pattern sets for the recognizers PrivAiTe registers itself.

A recognizer is registered on every configured language, so any human-language
word inside its regexes reaches texts written in another language. Unioning the
vocabulary of all languages into one pattern set is how issue #31 happened: the
French and German month names applied to Dutch and English, masking exactly the
months spelled like the German ones and leaving the rest of the calendar alone.

The rule this module exists to enforce: **a cue made of words belongs to the
language those words come from.** Only patterns that contain no human-language
word at all (digits, punctuation, code identifiers) are language-neutral and
stay active everywhere.

The asymmetry to keep in mind when editing a recognizer: a *cue* widens what is
detected, so an out-of-language cue is over-masking and must be scoped. A
*stop word* list only ever shortens a match, so unioning those across languages
stays on the safe side and is deliberately left unscoped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

# The languages PrivAiTe can configure Presidio for (spaCy models exist for
# these). A key outside this set would be silently dead vocabulary.
SUPPORTED_LANGUAGES = frozenset({"de", "en", "es", "fr", "it", "nl", "pt"})


class LanguagePatterns:
    """Compiled patterns selected by the language a recognizer is built for.

    ``by_language`` maps a language code to the patterns whose words belong to
    that language; ``neutral`` holds the patterns that carry no vocabulary and
    therefore apply to every language, including one with no entry of its own.
    """

    def __init__(
        self,
        by_language: Mapping[str, Sequence[str]],
        neutral: Sequence[str] = (),
        flags: int = re.UNICODE,
    ) -> None:
        unknown = set(by_language) - SUPPORTED_LANGUAGES
        if unknown:
            raise ValueError(f"vocabulary for unconfigurable language(s): {sorted(unknown)}")
        self._neutral = [re.compile(p, flags) for p in neutral]
        self._by_language = {
            lang: [re.compile(p, flags) for p in patterns] + self._neutral
            for lang, patterns in by_language.items()
        }

    def for_language(self, language: str) -> list[re.Pattern[str]]:
        return self._by_language.get(language, self._neutral)

    @property
    def languages(self) -> frozenset[str]:
        return frozenset(self._by_language)
