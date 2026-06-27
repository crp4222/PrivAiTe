from privaite.config.schema import AnonymizationConfig
from privaite.pii.faker_providers import FakerReplacementGenerator


def _gen() -> FakerReplacementGenerator:
    return FakerReplacementGenerator(AnonymizationConfig(faker_locale=["en_US"]))


def test_deterministic():
    gen = _gen()
    assert gen.generate("PERSON", "jean dupont") == gen.generate("PERSON", "jean dupont")


def test_different_inputs_different_outputs():
    gen = _gen()
    assert gen.generate("PERSON", "alice") != gen.generate("PERSON", "bob")


def test_generate_variant():
    gen = _gen()
    r0 = gen.generate("PERSON", "alice")
    r1 = gen.generate_variant("PERSON", "alice", 1)
    r2 = gen.generate_variant("PERSON", "alice", 2)
    assert r0 != r1 or r1 != r2


def test_email_generation():
    gen = _gen()
    assert "@" in gen.generate("EMAIL_ADDRESS", "test@example.com")


def test_unknown_entity_type_falls_back():
    gen = _gen()
    result = gen.generate("WIDGET_ID", "abc123")
    assert result.startswith("[WIDGET_ID_")
