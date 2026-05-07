from __future__ import annotations

from privaite.config.schema import AnonymizationConfig
from privaite.pii.entity import PIIEntity
from privaite.pii.faker_providers import FakerReplacementGenerator
from privaite.pii.mapping import PIIMapping


class Anonymizer:
    def __init__(self, config: AnonymizationConfig) -> None:
        self.config = config
        self.generator = FakerReplacementGenerator(config)

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
            original = text[entity.start:entity.end]

            existing_fake = mapping.get_fake(original)
            if existing_fake:
                fake = existing_fake
            else:
                fake = self.generator.generate(entity.entity_type, original)
                retries = 0
                while (fake == original or mapping.get_original(fake) is not None) and retries < 10:
                    fake = self.generator.generate_variant(entity.entity_type, original, retries)
                    retries += 1
                mapping.add(original, fake, entity.entity_type)

            text = text[:entity.start] + fake + text[entity.end:]

        return text
