from __future__ import annotations

import re

from presidio_analyzer import RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

from privaite.pii.boundaries import ENV_LINE_PREFIX
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
# .env files and shell exports declare a credential as an upper-case name at the
# start of a line. TOKEN, SECRET and KEY are too common as plain identifiers to
# be trusted anywhere else (token=None, cache_key=users, next_page_token=...),
# and even here the value has to look opaque: long, with a digit and a letter,
# and not a path. PASS means a password, so any value counts; PWD needs a prefix
# (MYSQL_PWD), because PWD alone is the shell's working directory. The prefix is
# the one the boundary rules use: a read tool's line numbers, a diff marker.
_ENV_LINE = r"(?m:^)" + ENV_LINE_PREFIX
_ENV_NAME = r"(?-i:(?:[A-Z][A-Z0-9]*_)*(?:TOKEN|SECRET|KEY))[ \t]*=[ \t]*"
_ENV_PASSWORD = r"(?-i:(?:[A-Z][A-Z0-9]*_)*PASS|(?:[A-Z][A-Z0-9]*_)+PWD)[ \t]*=[ \t]*"
_OPAQUE = (
    r"(?![/~.])(?=[^\s\"'`]*[0-9])(?=[^\s\"'`]*[A-Za-z])"
    r"[A-Za-z0-9_+/=.~-]{16,}(?![^\s\"'`])"
)
# Formats a provider documents for its own keys: recognizable with no field
# name at all. Prefix and length together, so the prefix alone ("sk-", "hf_")
# and words that merely contain it ("task-force-2026") are left alone.
_KNOWN_KEY = (
    r"(?<![A-Za-z0-9_-])(?-i:"
    r"sk-(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{20,}"
    r"|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{40,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|hf_[A-Za-z0-9]{30,}"
    r")(?![A-Za-z0-9_-])"
)
# CLI option names are code identifiers and apply in every configured language.
# They follow the assignment vocabulary, with the same kind of bounded prefix
# (--db-password), so an agent echoing a restored credential is covered whatever
# the option is called.
_CLI_NAME = (
    r"(?<![\w.-])--(?:[a-z][a-z0-9]{0,31}[_-]){0,3}"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|password|passwd|mot-de-passe)"
)
_CLI_EQUALS = _CLI_NAME + r"[ \t]*=[ \t]*"
_CLI_SPACE = _CLI_NAME + r"(?![ \t]*=)[ \t]+"
_CLI_PASSWORD = r"(?:" + _CLI_EQUALS + r"|" + _CLI_SPACE + r")"
_CLI_PREFIX = re.compile(_CLI_PASSWORD, re.IGNORECASE)
_VALUE_END = r"(?![^\s\"'`])"
# A variable or a command substitution stands for the secret: it is not the
# secret, and replacing it would break the command the agent is about to run.
_SHELL_REFERENCE = r"\$(?:\{[a-z_][a-z0-9_]*\}|[a-z_][a-z0-9_]*)"
# What follows the option in help output: <value>, [VALUE], PASSWORD, TEXT.
_METAVARIABLE = r"(?:<[^\s\"'`]*>|\[[^\s\"'`]*\]|\{[^\s\"'`]*\}|(?-i:[A-Z][A-Z_]*))" + _VALUE_END
_UNQUOTED = (
    r"(?!\\+[\"'])(?!\$\()(?!" + _SHELL_REFERENCE + _VALUE_END + r")(?!" + _METAVARIABLE + r")"
)
# In prose the option is followed by a word, not a value ("use --password to
# set it"). Only the space-separated form reads that way, and only these words
# are skipped: a weak password such as "changeme" is still a value.
_PROSE_WORD = (
    r"(?:to|is|are|was|be|and|or|for|the|a|an|of|in|on|if|when|then|with|from|as|at|by|it|"
    r"this|that|which|not|no|option|options|flag|flags|argument|arg|value|values|string|"
    r"str|text|prompt|force|required|optional|must|should|can|will|you|your|"
    r"est|et|ou|pour|le|la|les|un|une|de|du|des|valeur|doit|requis|requise)"
    r"[.,;:!?)]*" + _VALUE_END
)
_STRING_LITERALS = re.compile(
    r'"(?P<double>(?:[^"\\\r\n]|\\[^\r\n])*)"'
    r"|'(?P<single>(?:[^'\\\r\n]|\\[^\r\n])*)'"
)


def _literal_view(
    text: str, start: int, end: int, source_spans: list[tuple[int, int]] | None
) -> tuple[str, list[tuple[int, int]]]:
    """Unescape quotes/backslashes while retaining each character's source span."""
    characters = []
    spans = []
    index = start
    while index < end:
        next_index = index + 1
        character = text[index]
        if character == "\\" and next_index < end and text[next_index] in "\\\"'":
            character = text[next_index]
            next_index += 1
        characters.append(character)
        if source_spans is None:
            spans.append((index, next_index))
        else:
            spans.append((source_spans[index][0], source_spans[next_index - 1][1]))
        index = next_index
    return "".join(characters), spans


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
            # another URL cannot be mistaken for the password's closing @. The
            # user name can be empty: redis://:password@host is the usual form.
            r"\b[a-z][a-z0-9+.-]{0,31}://[^\s/:@?#\"'<>]*:"
            r"(?P<value>[^\s/@?#\"'<>]+)@",
            _ENV_LINE + _ENV_NAME + r"[\"']?(?P<value>" + _OPAQUE + r")",
            _ENV_LINE + _ENV_PASSWORD + r'"(?P<value>(?:[^"\\\r\n]|\\[^\r\n])+)"',
            _ENV_LINE + _ENV_PASSWORD + r"'(?P<value>[^'\r\n]+)'",
            _ENV_LINE + _ENV_PASSWORD + r"(?P<value>[^\s\"'`]+)",
            r"(?P<value>" + _KNOWN_KEY + r")",
            r"\bauthorization[ \t]*:[ \t]*bearer[ \t]+"
            r"(?P<value>[a-z0-9._~+/-]+={0,2})",
        ]
        self._specs = [
            (re.compile(pattern, re.IGNORECASE), "SECRET", 0.85, "value") for pattern in patterns
        ]
        self._cli_specs = [
            re.compile(pattern, re.IGNORECASE)
            for pattern in (
                # Double quotes expand variables, so "$NAME" is a reference too.
                _CLI_PASSWORD
                + r'"(?P<value>(?!\$\()(?!'
                + _SHELL_REFERENCE
                + r'")(?:[^"\\\r\n]|\\[^\r\n])+)"',
                # Shell single quotes preserve backslashes, including a trailing one.
                _CLI_PASSWORD + r"'(?P<value>[^'\r\n]+)'",
                # An encoded quote starts a quoted argument, never a one-character value.
                _CLI_EQUALS + r"(?P<value>" + _UNQUOTED + r"[^\s\"'`]+)",
                _CLI_SPACE + r"(?P<value>" + _UNQUOTED + r"(?!" + _PROSE_WORD + r")[^\s\"'`]+)",
            )
        ]
        self._specs.extend((pattern, "SECRET", 0.85, "value") for pattern in self._cli_specs)

    def _cli_literal_results(
        self,
        text: str,
        source_spans: list[tuple[int, int]] | None = None,
        depth: int = 0,
    ) -> list[RecognizerResult]:
        # A command literal and an optional JSON serialization cover the agent echo.
        if depth >= 2:
            return []
        results = []
        for literal in _STRING_LITERALS.finditer(text):
            group = "double" if literal.group("double") is not None else "single"
            if _CLI_PREFIX.search(literal.group(group)) is None:
                continue
            decoded, spans = _literal_view(
                text, literal.start(group), literal.end(group), source_spans
            )
            for pattern in self._cli_specs:
                for match in pattern.finditer(decoded):
                    start, end = match.span("value")
                    results.append(
                        RecognizerResult(
                            entity_type="SECRET",
                            start=spans[start][0],
                            end=spans[end - 1][1],
                            score=0.85,
                        )
                    )
            results.extend(self._cli_literal_results(decoded, spans, depth + 1))
        return results

    def analyze(
        self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts | None = None
    ) -> list[RecognizerResult]:
        results = super().analyze(text, entities, nlp_artifacts)
        if _CLI_PREFIX.search(text) is None:
            return results
        results.extend(self._cli_literal_results(text))
        # Decoded spans can contain a partial raw-text match or duplicate its span.
        # Keep the complete value once, without changing the surrounding source.
        results.sort(key=lambda result: (result.start, -result.end))
        complete = []
        covered_end = 0
        for result in results:
            if result.end > covered_end:
                complete.append(result)
                covered_end = result.end
        return complete
