from __future__ import annotations

import re
from difflib import SequenceMatcher

from privaite.config.schema import DeanonymizationConfig
from privaite.pii.mapping import PIIMapping

# An intact-but-unknown placeholder (e.g. the model hallucinated <PERSON_3> when
# only <PERSON_1> exists). Fuzzy-matching it to a KNOWN placeholder would inject
# someone else's PII, so these candidates are never fuzzy-replaced.
_PLACEHOLDER_SHAPE = re.compile(r"<[A-Z][A-Z0-9_]*_\d+>\Z")


class DeAnonymizer:
    def __init__(self, config: DeanonymizationConfig) -> None:
        self.config = config

    def deanonymize(self, text: str, mapping: PIIMapping) -> str:
        if mapping.is_empty:
            return text

        if self.config.fuzzy_matching:
            return self._deanonymize_fuzzy(text, mapping)

        return self._deanonymize_exact(text, mapping)

    def _deanonymize_exact(self, text: str, mapping: PIIMapping) -> str:
        fakes = mapping.get_all_fakes()
        sorted_fakes = sorted(fakes.keys(), key=len, reverse=True)

        for fake in sorted_fakes:
            original = fakes[fake]
            text = text.replace(fake, original)

        return text

    def _deanonymize_fuzzy(self, text: str, mapping: PIIMapping) -> str:
        text = self._deanonymize_exact(text, mapping)

        fakes = mapping.get_all_fakes()
        if not fakes:
            return text

        # Token spans over the REAL text. Replacements are applied by slicing the
        # original string, so every untouched character (newlines, indentation,
        # inner punctuation) survives verbatim. Rebuilding via split()/join() here
        # once flattened all whitespace in every response that carried PII.
        tokens = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
        if not tokens:
            return text

        known_fakes = set(fakes)
        replacements: list[tuple[int, int, str]] = []

        def _overlaps(start: int, end: int) -> bool:
            return any(s < end and start < e for s, e, _ in replacements)

        for fake in sorted(fakes, key=len, reverse=True):
            original = fakes[fake]
            window = len(fake.split())
            if window == 0:
                continue

            for i in range(len(tokens) - window + 1):
                start = tokens[i][0]
                end = tokens[i + window - 1][1]
                if _overlaps(start, end):
                    continue
                candidate = text[start:end]
                if candidate == original or candidate in known_fakes:
                    continue
                if _PLACEHOLDER_SHAPE.match(candidate):
                    continue
                ratio = SequenceMatcher(None, candidate.lower(), fake.lower()).ratio()
                if ratio >= self.config.fuzzy_threshold:
                    replacements.append((start, end, original))

        for start, end, original in sorted(replacements, reverse=True):
            text = text[:start] + original + text[end:]

        return text
