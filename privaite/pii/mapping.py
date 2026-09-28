from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# The shape of an issued numbered placeholder (<PERSON_1>, <EMAIL_ADDRESS_12>).
# The type part is loose on purpose (any run without brackets): a custom entity
# type is a free-form name, and a missed literal is a collision while a junk
# match only reserves a string no placeholder would ever take.
PLACEHOLDER_LITERAL = re.compile(r"<([^<>]{1,100}?)_(\d+)>")


@dataclass
class PIIMapping:
    _original_to_fake: dict[str, str] = field(default_factory=dict)
    _fake_to_original: dict[str, str] = field(default_factory=dict)
    _entity_types: dict[str, str] = field(default_factory=dict)
    _type_counters: dict[str, int] = field(default_factory=dict)
    # Placeholder-shaped strings that were already in the request. The restore
    # replaces every occurrence of an issued placeholder, so issuing one of
    # these would write a real value into the client's own "<PERSON_1>" (a test
    # fixture, a template variable, or a planted "?to=<EMAIL_ADDRESS_1>").
    _reserved: set[str] = field(default_factory=set)

    def add(self, original: str, fake: str, entity_type: str) -> None:
        self._original_to_fake[original] = fake
        self._fake_to_original[fake] = original
        self._entity_types[original] = entity_type
        self._type_counters[entity_type] = self._type_counters.get(entity_type, 0) + 1

    def note(self, original: str, entity_type: str) -> None:
        # Record a detection for stats/counting WITHOUT a reversible substitution.
        # Used by lossy methods (mask, redact): they must never be restored, and
        # two different originals that mask to the same string ("****") must not
        # collide in the reverse map and cross-restore each other's PII. Counts
        # once per unique original, matching add()'s semantics, so /stats does not
        # inflate when the same value is masked at several positions.
        if original not in self._entity_types:
            self._type_counters[entity_type] = self._type_counters.get(entity_type, 0) + 1
        self._entity_types[original] = entity_type

    def add_literal(self, literal: str, fake: str) -> None:
        # A placeholder-shaped string from the input that an earlier text of the
        # same request already got issued as a placeholder. It is sent as a
        # fresh placeholder and restored to itself. Not a detection: it stays
        # out of the type counts and /stats.
        self._original_to_fake[literal] = fake
        self._fake_to_original[fake] = literal

    def reserve(self, value: Any) -> None:
        """Reserve every placeholder-shaped string in value (a string or a
        JSON-like tree, dict keys included) so no placeholder issued for this
        request can equal it. Read only: value is not modified."""
        if isinstance(value, str):
            self._reserved.update(m.group(0) for m in PLACEHOLDER_LITERAL.finditer(value))
        elif isinstance(value, dict):
            for key, item in value.items():
                self.reserve(key)
                self.reserve(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.reserve(item)

    def is_taken(self, fake: str) -> bool:
        return fake in self._reserved or fake in self._fake_to_original

    def next_index(self, entity_type: str) -> int:
        return self._type_counters.get(entity_type, 0) + 1

    def get_fake(self, original: str) -> str | None:
        return self._original_to_fake.get(original)

    def get_original(self, fake: str) -> str | None:
        return self._fake_to_original.get(fake)

    def has_original(self, original: str) -> bool:
        return original in self._original_to_fake

    def get_all_fakes(self) -> dict[str, str]:
        return dict(self._fake_to_original)

    def get_entity_type(self, original: str) -> str | None:
        return self._entity_types.get(original)

    def entity_type_counts(self) -> dict[str, int]:
        # Per-type detection counts, including lossy mask/redact ones that never
        # enter the reversible map. Used for /stats.
        return dict(self._type_counters)

    @property
    def has_detections(self) -> bool:
        # True if ANY PII was detected (reversible or lossy). is_empty only tracks
        # reversible substitutions, so a mask-only request is is_empty but not this.
        return bool(self._entity_types)

    @property
    def count(self) -> int:
        return len(self._original_to_fake)

    @property
    def is_empty(self) -> bool:
        return len(self._original_to_fake) == 0
