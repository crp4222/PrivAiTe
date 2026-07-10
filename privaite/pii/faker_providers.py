from __future__ import annotations

import hashlib
from collections.abc import Callable

from faker import Faker

from privaite.config.schema import AnonymizationConfig


class FakerReplacementGenerator:
    """Produces realistic fake values for the ``fake_replacement`` method.

    This class only generates fakes. Method dispatch (placeholder, mask, redact)
    and entity overrides are owned by :class:`~privaite.pii.anonymizer.Anonymizer`,
    the single place that also holds the per-request mapping used to number
    placeholders. Keeping dispatch in one place avoids the two implementations
    drifting apart.
    """

    def __init__(self, config: AnonymizationConfig) -> None:
        self.config = config

    def generate(self, entity_type: str, original: str) -> str:
        return self._generate_fake(entity_type, original)

    def generate_variant(self, entity_type: str, original: str, variant: int) -> str:
        return self._generate_fake(entity_type, original, salt=variant)

    def _seeded_faker(self, original: str, salt: int = 0) -> Faker:
        raw = original.lower().strip() + str(salt)
        seed = int(hashlib.sha256(raw.encode()).hexdigest(), 16) % (10**9)
        f = Faker(self.config.faker_locale)
        f.seed_instance(seed)
        return f

    def _generate_fake(self, entity_type: str, original: str, salt: int = 0) -> str:
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
