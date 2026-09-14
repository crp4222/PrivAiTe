from __future__ import annotations

import re

from privaite.pii.recognizer_base import RegexSpanRecognizer

# Deliberately explicit field names: a generic "key" or an entropy threshold
# would hide identifiers, source code, and ordinary diagnostic text. Bounded
# prefixes cover environment variables such as SERVICE_API_KEY without an
# unbounded backtracking search through long log lines.
_FIELD = (
    r"(?:[a-z][a-z0-9_]{0,63}_)?"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|password|passwd|smtp[_-]?secret|presented[_-]?key)"
)
_ASSIGNMENT = (
    r"(?<![\w.-])(?:\"" + _FIELD + r"\"|'" + _FIELD + r"'|" + _FIELD + r")"
    r"[ \t]*[:=][ \t]*"
)


class StructuredSecretRecognizer(RegexSpanRecognizer):
    """Context-independent credential spans in plaintext tool outputs.

    Report only the value, preserving field names, quotes and URI structure.
    These rules supplement NLP; a match never skips the other detectors.
    They are not a complete credential scanner (encoded payloads and arbitrary
    field names still need model detection or operator custom patterns).
    """

    def __init__(self, supported_language: str = "en") -> None:
        super().__init__(
            supported_entities=["SECRET"],
            supported_language=supported_language,
            name="StructuredSecretRecognizer",
        )
        patterns = [
            _ASSIGNMENT + r'"(?P<value>(?:[^"\\\r\n]|\\[^\r\n])+)"',
            _ASSIGNMENT + r"'(?P<value>(?:[^'\\\r\n]|\\[^\r\n])+)'",
            # Unquoted values end at whitespace. Commas, semicolons and braces
            # may be password characters: trimming them would leave a fragment
            # exposed. Use quoted values to disambiguate structural delimiters.
            # Do not start this alternative inside an unterminated quote.
            _ASSIGNMENT + r"(?P<value>[^\s\"'`]+)",
            # RFC-style URI userinfo. Restrict delimiters so a later email or
            # another URL cannot be mistaken for the password's closing @.
            r"\b[a-z][a-z0-9+.-]{0,31}://[^\s/:@?#\"'<>]+:"
            r"(?P<value>[^\s/@?#\"'<>]+)@",
            r"\bauthorization[ \t]*:[ \t]*bearer[ \t]+"
            r"(?P<value>[a-z0-9._~+/-]+={0,2})",
        ]
        self._specs = [
            (re.compile(pattern, re.IGNORECASE), "SECRET", 0.85, "value") for pattern in patterns
        ]
