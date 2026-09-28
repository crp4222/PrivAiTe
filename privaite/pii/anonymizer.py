from __future__ import annotations

from privaite.config.schema import AnonymizationConfig
from privaite.pii.entity import PIIEntity
from privaite.pii.faker_providers import FakerReplacementGenerator
from privaite.pii.mapping import PLACEHOLDER_LITERAL, PIIMapping


class FakeReplacementExhaustedError(RuntimeError):
    """Raised when fake_replacement cannot produce a usable fake after retrying:
    every candidate either equaled the original (returning it would forward raw
    PII) or collided with a fake already mapped to a DIFFERENT original (which
    would cross-restore two values). Fail closed instead. The message names the
    entity TYPE only, never a value."""

    def __init__(self, entity_type: str) -> None:
        super().__init__(
            f"fake replacement generation exhausted for entity type {entity_type}; request blocked"
        )


class Anonymizer:
    def __init__(self, config: AnonymizationConfig) -> None:
        self.config = config
        self.generator = FakerReplacementGenerator(config)

    # Lossy methods: the original cannot be recovered from the output, so it must
    # never enter the reversible map (that both allowed a wrong restore and let two
    # different values that mask to the same string cross-restore each other).
    _IRREVERSIBLE = frozenset({"mask", "redact"})

    def is_irreversible(self, entity_type: str) -> bool:
        return self._method_for(entity_type) in self._IRREVERSIBLE

    def anonymize(
        self,
        text: str,
        entities: list[PIIEntity],
        mapping: PIIMapping,
    ) -> str:
        shielded = self._shield_literals(text, entities, mapping)
        if not entities and not shielded:
            return text

        # One right-to-left pass over entities and shielded literals together:
        # every offset still points into the untouched part of the text.
        spans: list[tuple[int, int, PIIEntity | str]] = [(e.start, e.end, e) for e in entities]
        spans.extend(shielded)

        for start, end, item in sorted(spans, key=lambda span: span[0], reverse=True):
            if isinstance(item, str):
                text = text[:start] + item + text[end:]
                continue
            entity = item
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

    def _shield_literals(
        self, text: str, entities: list[PIIEntity], mapping: PIIMapping
    ) -> list[tuple[int, int, str]]:
        """Keep the input's own placeholder-shaped strings apart from the issued
        placeholders. The restore swaps EVERY occurrence of an issued placeholder
        for its original, so a "<PERSON_1>" the client already had (a test
        fixture, a template variable, a planted "?to=<EMAIL_ADDRESS_1>") would
        get a real value written into it.

        A literal nothing has taken yet is reserved: it goes out verbatim and no
        placeholder is ever issued with that string. A literal an earlier text
        of the request already got as a placeholder is too late to reserve, so
        it is swapped for a fresh placeholder that restores to the literal
        itself. Returns those swaps as (start, end, fake), never overlapping a
        detected entity (the entity's own replacement already breaks the
        literal apart and restores it)."""
        matches = list(PLACEHOLDER_LITERAL.finditer(text))
        if not matches:
            return []
        for match in matches:
            if mapping.get_original(match.group(0)) is None:
                mapping.reserve(match.group(0))
        swaps: list[tuple[int, int, str]] = []
        for match in matches:
            literal = match.group(0)
            if mapping.get_original(literal) is None:
                continue
            if any(e.start < match.end() and match.start() < e.end for e in entities):
                continue
            fake = mapping.get_fake(literal)
            if fake is None:
                fake = self._numbered_placeholder(match.group(1), mapping)
                mapping.add_literal(literal, fake)
            swaps.append((match.start(), match.end(), fake))
        return swaps

    def _method_for(self, entity_type: str) -> str:
        # An entity override picks the method for its type; otherwise the global
        # config applies. One place decides, so reversibility and dispatch agree.
        override = self.config.entity_overrides.get(entity_type)
        return override.method if override else self.config.method

    def _make_placeholder(self, entity_type: str, original: str, mapping: PIIMapping) -> str:
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
            while (fake == original or mapping.get_original(fake) is not None) and retries < 10:
                # generate() is variant 0: asking for it again would reproduce
                # the candidate that just collided, so the retries walk 1..10.
                retries += 1
                fake = self.generator.generate_variant(entity_type, original, retries)
            if fake == original or mapping.get_original(fake) is not None:
                # Retry exhaustion used to return the colliding value anyway,
                # silently forwarding the original or cross-restoring another
                # mapping entry. Fail closed: the engine turns this into a
                # blocked request.
                raise FakeReplacementExhaustedError(entity_type)
            return fake

        # "placeholder" (the default) and any unknown method fall back to a
        # numbered placeholder, numbered through the per-request mapping.
        return self._numbered_placeholder(entity_type, mapping)

    @staticmethod
    def _numbered_placeholder(entity_type: str, mapping: PIIMapping) -> str:
        # Skip every number whose placeholder is already issued or already in
        # the input (see _shield_literals).
        idx = mapping.next_index(entity_type)
        while mapping.is_taken(f"<{entity_type}_{idx}>"):
            idx += 1
        return f"<{entity_type}_{idx}>"
