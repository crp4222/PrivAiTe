from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PIIMapping:
    _original_to_fake: dict[str, str] = field(default_factory=dict)
    _fake_to_original: dict[str, str] = field(default_factory=dict)
    _entity_types: dict[str, str] = field(default_factory=dict)
    _type_counters: dict[str, int] = field(default_factory=dict)

    def add(self, original: str, fake: str, entity_type: str) -> None:
        self._original_to_fake[original] = fake
        self._fake_to_original[fake] = original
        self._entity_types[original] = entity_type
        self._type_counters[entity_type] = self._type_counters.get(entity_type, 0) + 1

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

    @property
    def count(self) -> int:
        return len(self._original_to_fake)

    @property
    def is_empty(self) -> bool:
        return len(self._original_to_fake) == 0
