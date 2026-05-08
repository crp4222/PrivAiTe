from __future__ import annotations

import hashlib
from collections.abc import Callable

from faker import Faker

from privaite.config.schema import AnonymizationConfig, EntityOverride


class FakerReplacementGenerator:
    def __init__(self, config: AnonymizationConfig) -> None:
        self.config = config
        self.faker = Faker(config.faker_locale)
        self._counters: dict[str, int] = {}

    def generate(self, entity_type: str, original: str) -> str:
        override = self.config.entity_overrides.get(entity_type)
        if override:
            return self._apply_override(override, original)

        method = self.config.method
        if method == "redact":
            return f"[{entity_type}]"
        if method == "mask":
            return "*" * len(original)
        if method == "placeholder":
            return self._next_placeholder(entity_type)

        return self._generate_fake(entity_type, original)

    def generate_variant(self, entity_type: str, original: str, variant: int) -> str:
        if self.config.method == "placeholder":
            return self._next_placeholder(entity_type)
        return self._generate_fake(entity_type, original, salt=variant)

    def _next_placeholder(self, entity_type: str) -> str:
        n = self._counters.get(entity_type, 0) + 1
        self._counters[entity_type] = n
        return f"<{entity_type}_{n}>"

    def _seeded_faker(self, original: str, salt: int = 0) -> Faker:
        raw = original.lower().strip() + str(salt)
        seed = int(hashlib.sha256(raw.encode()).hexdigest(), 16) % (10**9)
        f = Faker(self.config.faker_locale)
        f.seed_instance(seed)
        return f

    def _generate_fake(
        self, entity_type: str, original: str, salt: int = 0
    ) -> str:
        f = self._seeded_faker(original, salt)

        generators: dict[str, Callable[[], str]] = {
            "PERSON": f.name,
            "EMAIL_ADDRESS": f.email,
            "PHONE_NUMBER": f.phone_number,
            "LOCATION": f.city,
            "DATE_TIME": lambda: f.date(pattern="%d/%m/%Y"),
            "CREDIT_CARD": lambda: f.credit_card_number(),
            "IP_ADDRESS": f.ipv4,
            "URL": f.url,
            "US_SSN": f.ssn,
            "IBAN_CODE": f.iban,
            "FINANCIAL": f.bban,
            "SECRET": lambda: "[REDACTED]",
        }

        generator = generators.get(entity_type)
        if generator:
            return generator()

        return f"[{entity_type}_{f.pystr(min_chars=4, max_chars=8)}]"

    def _apply_override(self, override: EntityOverride, original: str) -> str:
        if override.method == "redact":
            return "[REDACTED]"
        if override.method == "mask":
            return override.masking_char * len(original)
        if override.method == "placeholder":
            return "<REDACTED>"

        return self._generate_fake("GENERIC", original)
