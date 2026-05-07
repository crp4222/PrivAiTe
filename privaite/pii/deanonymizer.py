from __future__ import annotations

from difflib import SequenceMatcher

from privaite.config.schema import DeanonymizationConfig
from privaite.pii.mapping import PIIMapping


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

        sorted_fakes = sorted(fakes.keys(), key=len, reverse=True)

        for fake in sorted_fakes:
            original = fakes[fake]
            fake_words = fake.split()
            fake_len = len(fake_words)

            if fake_len == 0:
                continue

            text_words = text.split()
            i = 0
            new_words: list[str] = []
            while i < len(text_words):
                if i + fake_len <= len(text_words):
                    candidate = " ".join(text_words[i : i + fake_len])
                    ratio = SequenceMatcher(None, candidate.lower(), fake.lower()).ratio()
                    if ratio >= self.config.fuzzy_threshold and candidate != original:
                        new_words.append(original)
                        i += fake_len
                        continue

                new_words.append(text_words[i])
                i += 1

            text = " ".join(new_words)

        return text
