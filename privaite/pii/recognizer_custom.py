from __future__ import annotations

import re

from privaite.config.schema import CustomPatternConfig
from privaite.pii.recognizer_base import RegexSpanRecognizer

# The conventional group name for "the part to anonymize" when a pattern declares
# several named groups. Documented in docs/configuration.md.
_VALUE_GROUP = "value"


def value_group(compiled: re.Pattern[str]) -> str | None:
    """Which capture group of an operator pattern holds the value to anonymize,
    or None to use the whole match.

    A named group is how an operator says "the PII is here": with
    ``api_key=(?P<value>\\S+)`` the span must cover the key alone, not the
    ``api_key=`` label (the placeholder used to swallow the field name and the
    equals sign because the group was discarded). ``value`` wins when several
    groups are named, otherwise the first declared one is used, so the choice is
    deterministic. Patterns with no named group keep the previous behaviour: the
    whole match is the span.
    """
    names = compiled.groupindex
    if not names:
        return None
    if _VALUE_GROUP in names:
        return _VALUE_GROUP
    return min(names, key=lambda name: names[name])


class CustomPatternRecognizer(RegexSpanRecognizer):
    def __init__(
        self,
        patterns: list[CustomPatternConfig],
        supported_language: str = "fr",
    ) -> None:
        super().__init__(
            supported_entities=list({p.entity_type for p in patterns}),
            supported_language=supported_language,
            name="CustomPatternRecognizer",
        )
        compiled = [(re.compile(p.pattern, re.IGNORECASE | re.UNICODE), p) for p in patterns]
        self._specs = [(regex, p.entity_type, p.score, value_group(regex)) for regex, p in compiled]
