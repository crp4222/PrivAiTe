from privaite.config.schema import AnonymizationConfig, EntityOverride
from privaite.pii.anonymizer import Anonymizer
from privaite.pii.entity import PIIEntity
from privaite.pii.mapping import PIIMapping


def _entity(entity_type: str, text: str, start: int, end: int, score: float = 0.9) -> PIIEntity:
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
        entity_overrides={"CREDIT_CARD": EntityOverride(method="mask", masking_char="*")},
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


def test_global_redact_uses_typed_marker():
    config = AnonymizationConfig(faker_locale=["en_US"], method="redact")
    anon = Anonymizer(config)
    mapping = PIIMapping()

    result = anon.anonymize("Hi John Smith", [_entity("PERSON", "John Smith", 3, 13)], mapping)
    assert "[PERSON]" in result
    assert "John Smith" not in result


def test_global_mask():
    config = AnonymizationConfig(faker_locale=["en_US"], method="mask")
    anon = Anonymizer(config)
    mapping = PIIMapping()

    result = anon.anonymize("Hi John", [_entity("PERSON", "John", 3, 7)], mapping)
    assert "****" in result
    assert "John" not in result


def test_mask_is_irreversible_and_not_in_reverse_map():
    config = AnonymizationConfig(faker_locale=["en_US"], method="mask")
    anon = Anonymizer(config)
    mapping = PIIMapping()

    anon.anonymize("Hi John", [_entity("PERSON", "John", 3, 7)], mapping)
    # nothing to restore: mask is lossy by definition
    assert mapping.get_original("****") is None
    assert mapping.is_empty  # is_empty tracks reversible substitutions only
    # but the detection is still counted for /stats
    assert mapping.has_detections
    assert mapping.entity_type_counts() == {"PERSON": 1}


def test_mask_collision_does_not_cross_restore():
    # Two different 4-char names both mask to "****"; the reverse map must not
    # end up mapping "****" to one of them, or restoration would inject the
    # wrong person's name into the response.
    config = AnonymizationConfig(faker_locale=["en_US"], method="mask")
    anon = Anonymizer(config)
    mapping = PIIMapping()

    anon.anonymize(
        "Jean and Marc met",
        [_entity("PERSON", "Jean", 0, 4), _entity("PERSON", "Marc", 9, 13)],
        mapping,
    )
    assert mapping.get_original("****") is None
    assert mapping.entity_type_counts() == {"PERSON": 2}


def test_redact_is_irreversible():
    config = AnonymizationConfig(faker_locale=["en_US"], method="redact")
    anon = Anonymizer(config)
    mapping = PIIMapping()

    anon.anonymize("Hi John Smith", [_entity("PERSON", "John Smith", 3, 13)], mapping)
    assert mapping.get_original("[PERSON]") is None
    assert mapping.is_empty
    assert mapping.entity_type_counts() == {"PERSON": 1}


def test_placeholder_stays_reversible():
    config = AnonymizationConfig(faker_locale=["en_US"], method="placeholder")
    anon = Anonymizer(config)
    mapping = PIIMapping()

    anon.anonymize("Hi Jean Michel", [_entity("PERSON", "Jean Michel", 3, 14)], mapping)
    assert mapping.get_original("<PERSON_1>") == "Jean Michel"
    assert not mapping.is_empty


def test_override_redact_matches_global_typed_marker():
    config = AnonymizationConfig(
        faker_locale=["en_US"],
        method="placeholder",
        entity_overrides={"SECRET": EntityOverride(method="redact")},
    )
    anon = Anonymizer(config)
    mapping = PIIMapping()

    result = anon.anonymize("key sk-abc123", [_entity("SECRET", "sk-abc123", 4, 13)], mapping)
    assert "[SECRET]" in result
    assert "sk-abc123" not in result


def test_override_placeholder_is_numbered():
    # An override of method "placeholder" must yield a numbered placeholder, not a
    # literal marker, even when the global method is fake_replacement.
    config = AnonymizationConfig(
        faker_locale=["en_US"],
        method="fake_replacement",
        entity_overrides={"PERSON": EntityOverride(method="placeholder")},
    )
    anon = Anonymizer(config)
    mapping = PIIMapping()

    result = anon.anonymize("Hi John Smith", [_entity("PERSON", "John Smith", 3, 13)], mapping)
    assert "<PERSON_1>" in result
    assert mapping.get_original("<PERSON_1>") == "John Smith"


def test_override_fake_replacement_uses_entity_type():
    # An override of method "fake_replacement" must produce a real fake for the
    # entity type, not the generic fallback and not a placeholder.
    config = AnonymizationConfig(
        faker_locale=["en_US"],
        method="placeholder",
        entity_overrides={"EMAIL_ADDRESS": EntityOverride(method="fake_replacement")},
    )
    anon = Anonymizer(config)
    mapping = PIIMapping()

    result = anon.anonymize("mail a@b.com", [_entity("EMAIL_ADDRESS", "a@b.com", 5, 12)], mapping)
    fake = mapping.get_fake("a@b.com")
    assert fake is not None
    assert "a@b.com" not in result
    assert "@" in fake
    assert not fake.startswith("<")
