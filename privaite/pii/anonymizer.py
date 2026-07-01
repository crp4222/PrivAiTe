from __future__ import annotations

from privaite.config.schema import AnonymizationConfig
from privaite.pii.entity import PIIEntity
from privaite.pii.faker_providers import FakerReplacementGenerator
from privaite.pii.mapping import PIIMapping


class Anonymizer:
    def __init__(self, config: AnonymizationConfig) -> None:
        self.config = config
        self.generator = FakerReplacementGenerator(config)

    # Lossy methods: the original cannot be recovered from the output, so it must
    # never enter the reversible map (that both allowed a wrong restore and let two
    # different values that mask to the same string cross-restore each other).
    _IRREVERSIBLE = frozenset({"mask", "redact"})

    def anonymize(
        self,
        text: str,
        entities: list[PIIEntity],
        mapping: PIIMapping,
    ) -> str:
        if not entities:
            return text

        sorted_entities = sorted(entities, key=lambda e: e.start, reverse=True)

        for entity in sorted_entities:
            original = text[entity.start : entity.end]
            method = self._method_for(entity.entity_type)

            if method in self._IRREVERSIBLE:
                fake = self._make_placeholder(entity.entity_type, original, mapping)
                mapping.note(original, entity.entity_type)
            else:
                existing_fake = mapping.get_fake(original)
                if existing_fake:
                    fake = existing_fake
                else:
                    fake = self._make_placeholder(entity.entity_type, original, mapping)
                    mapping.add(original, fake, entity.entity_type)

            text = text[: entity.start] + fake + text[entity.end :]

        return text

    def _method_for(self, entity_type: str) -> str:
        # An entity override picks the method for its type; otherwise the global
        # config applies. One place decides, so reversibility and dispatch agree.
        override = self.config.entity_overrides.get(entity_type)
        return override.method if override else self.config.method

    def _make_placeholder(
        self, entity_type: str, original: str, mapping: PIIMapping
    ) -> str:
        override = self.config.entity_overrides.get(entity_type)
        method = override.method if override else self.config.method
        masking_char = override.masking_char if override else "*"

        if method == "mask":
            return masking_char * len(original)
        if method == "redact":
            return f"[{entity_type}]"
        if method == "fake_replacement":
            fake = self.generator.generate(entity_type, original)
            retries = 0
            while (
                fake == original or mapping.get_original(fake) is not None
            ) and retries < 10:
                fake = self.generator.generate_variant(entity_type, original, retries)
                retries += 1
            return fake

        # "placeholder" (the default) and any unknown method fall back to a
        # numbered placeholder, numbered through the per-request mapping.
        idx = mapping.next_index(entity_type)
        return f"<{entity_type}_{idx}>"
