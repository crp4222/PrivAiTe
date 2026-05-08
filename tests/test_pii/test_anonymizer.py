from privaite.config.schema import AnonymizationConfig, EntityOverride
from privaite.pii.anonymizer import Anonymizer
from privaite.pii.entity import PIIEntity
from privaite.pii.mapping import PIIMapping


def _entity(
    entity_type: str, text: str, start: int, end: int, score: float = 0.9
) -> PIIEntity:
    return PIIEntity(
        entity_type=entity_type,
        text=text,
        start=start,
        end=end,
        score=score,
        source="test",
    )


def test_anonymize_single_person():
    config = AnonymizationConfig(faker_locale=["en_US"])
    anon = Anonymizer(config)
    mapping = PIIMapping()

    text = "Hello John Smith!"
    entities = [_entity("PERSON", "John Smith", 6, 16)]

    result = anon.anonymize(text, entities, mapping)

    assert "John Smith" not in result
    assert result.startswith("Hello ")
    assert result.endswith("!")
    assert mapping.count == 1


def test_anonymize_preserves_non_pii():
    config = AnonymizationConfig(faker_locale=["en_US"])
    anon = Anonymizer(config)
    mapping = PIIMapping()

    text = "The quick brown fox"
    result = anon.anonymize(text, [], mapping)
    assert result == "The quick brown fox"
    assert mapping.is_empty


def test_same_name_maps_to_same_fake():
    config = AnonymizationConfig(faker_locale=["en_US"])
    anon = Anonymizer(config)
    mapping = PIIMapping()

    text1 = "Alice said hello"
    entities1 = [_entity("PERSON", "Alice", 0, 5)]
    anon.anonymize(text1, entities1, mapping)
    fake_name = mapping.get_fake("Alice")
    assert fake_name is not None

    text2 = "Then Alice left"
    entities2 = [_entity("PERSON", "Alice", 5, 10)]
    result2 = anon.anonymize(text2, entities2, mapping)
    assert fake_name in result2


def test_deterministic_fakes():
    config = AnonymizationConfig(faker_locale=["en_US"])
    anon1 = Anonymizer(config)
    anon2 = Anonymizer(config)
    m1 = PIIMapping()
    m2 = PIIMapping()

    text = "Hello John Smith!"
    entities = [_entity("PERSON", "John Smith", 6, 16)]

    r1 = anon1.anonymize(text, entities, m1)
    r2 = anon2.anonymize(text, entities, m2)
    assert r1 == r2


def test_entity_override_mask():
    config = AnonymizationConfig(
        faker_locale=["en_US"],
        entity_overrides={
            "CREDIT_CARD": EntityOverride(method="mask", masking_char="*")
        },
    )
    anon = Anonymizer(config)
    mapping = PIIMapping()

    text = "Card: 4111111111111111"
    entities = [_entity("CREDIT_CARD", "4111111111111111", 6, 22)]

    result = anon.anonymize(text, entities, mapping)
    assert "4111111111111111" not in result
    assert "****************" in result


def test_multiple_entities_different_types():
    config = AnonymizationConfig(faker_locale=["en_US"])
    anon = Anonymizer(config)
    mapping = PIIMapping()

    text = "Contact John at john@acme.com"
    entities = [
        _entity("PERSON", "John", 8, 12),
        _entity("EMAIL_ADDRESS", "john@acme.com", 16, 29),
    ]

    result = anon.anonymize(text, entities, mapping)
    assert "John" not in result
    assert "john@acme.com" not in result
    assert mapping.count == 2


def test_placeholder_mode():
    config = AnonymizationConfig(faker_locale=["en_US"], method="placeholder")
    anon = Anonymizer(config)
    mapping = PIIMapping()

    text = "Hello Jean Michel!"
    entities = [_entity("PERSON", "Jean Michel", 6, 17)]
    result = anon.anonymize(text, entities, mapping)

    assert "<PERSON_1>" in result
    assert "Jean Michel" not in result
    assert mapping.get_original("<PERSON_1>") == "Jean Michel"
