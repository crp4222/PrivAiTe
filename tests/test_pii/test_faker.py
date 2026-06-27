from privaite.config.schema import AnonymizationConfig, EntityOverride
from privaite.pii.faker_providers import FakerReplacementGenerator


def test_deterministic():
    config = AnonymizationConfig(faker_locale=["en_US"], method="fake_replacement")
    gen = FakerReplacementGenerator(config)

    r1 = gen.generate("PERSON", "jean dupont")
    r2 = gen.generate("PERSON", "jean dupont")
    assert r1 == r2


def test_different_inputs_different_outputs():
    config = AnonymizationConfig(faker_locale=["en_US"], method="fake_replacement")
    gen = FakerReplacementGenerator(config)

    r1 = gen.generate("PERSON", "alice")
    r2 = gen.generate("PERSON", "bob")
    assert r1 != r2


def test_generate_variant():
    config = AnonymizationConfig(faker_locale=["en_US"], method="fake_replacement")
    gen = FakerReplacementGenerator(config)

    r0 = gen.generate("PERSON", "alice")
    r1 = gen.generate_variant("PERSON", "alice", 1)
    r2 = gen.generate_variant("PERSON", "alice", 2)
    assert r0 != r1 or r1 != r2


def test_email_generation():
    config = AnonymizationConfig(faker_locale=["en_US"], method="fake_replacement")
    gen = FakerReplacementGenerator(config)

    result = gen.generate("EMAIL_ADDRESS", "test@example.com")
    assert "@" in result


def test_override_mask():
    config = AnonymizationConfig(
        faker_locale=["en_US"],
        entity_overrides={"CREDIT_CARD": EntityOverride(method="mask", masking_char="X")},
    )
    gen = FakerReplacementGenerator(config)

    result = gen.generate("CREDIT_CARD", "4111111111111111")
    assert result == "X" * 16


def test_override_redact():
    config = AnonymizationConfig(
        faker_locale=["en_US"],
        entity_overrides={"SECRET": EntityOverride(method="redact")},
    )
    gen = FakerReplacementGenerator(config)

    result = gen.generate("SECRET", "password123")
    assert result == "[REDACTED]"


def test_method_redact():
    config = AnonymizationConfig(faker_locale=["en_US"], method="redact")
    gen = FakerReplacementGenerator(config)

    result = gen.generate("PERSON", "alice")
    assert result == "[PERSON]"


def test_method_mask():
    config = AnonymizationConfig(faker_locale=["en_US"], method="mask")
    gen = FakerReplacementGenerator(config)

    result = gen.generate("PERSON", "alice")
    assert result == "*****"
