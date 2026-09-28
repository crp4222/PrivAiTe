import pytest

from privaite.config.schema import AnonymizationConfig, DeanonymizationConfig, EntityOverride
from privaite.pii.anonymizer import Anonymizer, FakeReplacementExhaustedError
from privaite.pii.deanonymizer import DeAnonymizer
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


def test_fake_replacement_exhaustion_fails_closed_on_identity():
    # If every candidate equals the original, returning it would forward raw
    # PII. Exhaustion must raise (the engine turns it into a blocked request),
    # and the error must name the TYPE, never the value.
    config = AnonymizationConfig(faker_locale=["en_US"], method="fake_replacement")
    anon = Anonymizer(config)
    anon.generator.generate = lambda etype, original: original
    anon.generator.generate_variant = lambda etype, original, variant: original
    mapping = PIIMapping()

    with pytest.raises(FakeReplacementExhaustedError) as ei:
        anon.anonymize("Hi John Smith", [_entity("PERSON", "John Smith", 3, 13)], mapping)
    assert "PERSON" in str(ei.value)
    assert "John Smith" not in str(ei.value)


def test_fake_replacement_exhaustion_fails_closed_on_collision():
    # If every candidate collides with a fake already mapped to a DIFFERENT
    # original, returning it would cross-restore the two values.
    config = AnonymizationConfig(faker_locale=["en_US"], method="fake_replacement")
    anon = Anonymizer(config)
    anon.generator.generate = lambda etype, original: "Jane Roe"
    anon.generator.generate_variant = lambda etype, original, variant: "Jane Roe"
    mapping = PIIMapping()
    mapping.add("Alice Wong", "Jane Roe", "PERSON")

    with pytest.raises(FakeReplacementExhaustedError):
        anon.anonymize("Hi John Smith", [_entity("PERSON", "John Smith", 3, 13)], mapping)
    # the poisoned candidate never entered the map for the new original
    assert mapping.get_original("Jane Roe") == "Alice Wong"
    assert mapping.get_fake("John Smith") is None


def test_fake_replacement_recovers_via_variant_before_exhaustion():
    # A collision on the first candidate but a clean variant must still succeed.
    config = AnonymizationConfig(faker_locale=["en_US"], method="fake_replacement")
    anon = Anonymizer(config)
    anon.generator.generate = lambda etype, original: original
    anon.generator.generate_variant = lambda etype, original, variant: "Safe Fake"
    mapping = PIIMapping()

    result = anon.anonymize("Hi John Smith", [_entity("PERSON", "John Smith", 3, 13)], mapping)
    assert result == "Hi Safe Fake"
    assert mapping.get_original("Safe Fake") == "John Smith"


def test_fake_replacement_retries_never_reuse_the_initial_seed():
    # generate() and generate_variant(..., 0) share salt 0: a retry asking for
    # variant 0 reproduces the very candidate that just collided and burns one
    # of the ten attempts for nothing. The retries must walk variants 1..10.
    config = AnonymizationConfig(faker_locale=["en_US"], method="fake_replacement")
    anon = Anonymizer(config)
    gen = anon.generator
    assert gen.generate_variant("PERSON", "John Smith", 0) == gen.generate("PERSON", "John Smith")

    asked: list[int] = []

    def _variant(etype: str, original: str, variant: int) -> str:
        asked.append(variant)
        return original  # keep colliding so every retry is exercised

    anon.generator.generate = lambda etype, original: original
    anon.generator.generate_variant = _variant
    with pytest.raises(FakeReplacementExhaustedError):
        anon.anonymize("Hi John Smith", [_entity("PERSON", "John Smith", 3, 13)], PIIMapping())
    assert asked == list(range(1, 11))


def test_input_placeholder_literal_is_never_issued():
    # A "<PERSON_1>" the client already had (a template variable, a test
    # fixture) used to be issued to a real name as well: the provider saw two
    # identical placeholders and the restore wrote the name into the literal.
    anon = Anonymizer(AnonymizationConfig(method="placeholder"))
    mapping = PIIMapping()
    text = "The template uses <PERSON_1> as a variable. Contact Marie Dupont."
    start = text.index("Marie Dupont")

    result = anon.anonymize(text, [_entity("PERSON", "Marie Dupont", start, start + 12)], mapping)

    assert result == "The template uses <PERSON_1> as a variable. Contact <PERSON_2>."
    assert DeAnonymizer(DeanonymizationConfig()).deanonymize(result, mapping) == text


def test_late_placeholder_literal_is_swapped_and_restored_to_itself():
    # The literal arrives in a later text, after "<PERSON_1>" was issued: too
    # late to reserve, so it travels as a fresh placeholder and comes back as
    # itself. The entity after it in the same text keeps correct offsets.
    anon = Anonymizer(AnonymizationConfig(method="placeholder"))
    mapping = PIIMapping()
    first = anon.anonymize(
        "Contact Marie Dupont.", [_entity("PERSON", "Marie Dupont", 8, 20)], mapping
    )
    text = "assert '<PERSON_1>' == fixture, owner Jean Martin"
    start = text.index("Jean Martin")

    second = anon.anonymize(text, [_entity("PERSON", "Jean Martin", start, start + 11)], mapping)
    again = anon.anonymize("still <PERSON_1>", [], mapping)

    assert first == "Contact <PERSON_1>."
    assert second == "assert '<PERSON_2>' == fixture, owner <PERSON_3>"
    assert again == "still <PERSON_2>"
    restored = DeAnonymizer(DeanonymizationConfig()).deanonymize(f"{first}\n{second}", mapping)
    assert restored == f"Contact Marie Dupont.\n{text}"
    # The swap is not a detection: /stats counts the two names only.
    assert mapping.entity_type_counts() == {"PERSON": 2}


def test_late_literal_inside_a_detected_entity_is_left_to_the_entity():
    # The entity's own replacement already takes the literal out of the text
    # and restores it, so no second swap may overlap it.
    anon = Anonymizer(AnonymizationConfig(method="placeholder"))
    mapping = PIIMapping()
    first = anon.anonymize("Marie Dupont", [_entity("PERSON", "Marie Dupont", 0, 12)], mapping)

    second = anon.anonymize("name=<PERSON_1>", [_entity("PERSON", "<PERSON_1>", 5, 15)], mapping)

    assert second == "name=<PERSON_2>"
    restored = DeAnonymizer(DeanonymizationConfig()).deanonymize(f"{first} {second}", mapping)
    assert restored == "Marie Dupont name=<PERSON_1>"
