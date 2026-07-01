from privaite.config.schema import DeanonymizationConfig
from privaite.pii.deanonymizer import DeAnonymizer
from privaite.pii.mapping import PIIMapping


def test_deanonymize_exact():
    config = DeanonymizationConfig(enabled=True, fuzzy_matching=False)
    deanon = DeAnonymizer(config)

    mapping = PIIMapping()
    mapping.add("Jean Eude", "Michel Deus", "PERSON")

    text = "Michel Deus a dit bonjour"
    result = deanon.deanonymize(text, mapping)
    assert result == "Jean Eude a dit bonjour"


def test_deanonymize_multiple():
    config = DeanonymizationConfig(enabled=True, fuzzy_matching=False)
    deanon = DeAnonymizer(config)

    mapping = PIIMapping()
    mapping.add("Jean", "Michel", "PERSON")
    mapping.add("jean@acme.com", "michel@example.net", "EMAIL_ADDRESS")

    text = "Michel (michel@example.net) a envoyé un email"
    result = deanon.deanonymize(text, mapping)
    assert "Jean" in result
    assert "jean@acme.com" in result


def test_deanonymize_empty_mapping():
    config = DeanonymizationConfig(enabled=True, fuzzy_matching=False)
    deanon = DeAnonymizer(config)

    mapping = PIIMapping()
    text = "Nothing to replace"
    assert deanon.deanonymize(text, mapping) == text


def test_deanonymize_fuzzy():
    config = DeanonymizationConfig(
        enabled=True, fuzzy_matching=True, fuzzy_threshold=0.75
    )
    deanon = DeAnonymizer(config)

    mapping = PIIMapping()
    mapping.add("Jean Eude", "Michel Deus", "PERSON")

    text = "Michel Deu a dit bonjour"
    result = deanon.deanonymize(text, mapping)
    assert "Jean Eude" in result


def test_deanonymize_longest_first():
    config = DeanonymizationConfig(enabled=True, fuzzy_matching=False)
    deanon = DeAnonymizer(config)

    mapping = PIIMapping()
    mapping.add("Jean Eude Dupont", "Michel Deus Martin", "PERSON")
    mapping.add("Jean Eude", "Michel Deus", "PERSON")

    text = "Michel Deus Martin est là"
    result = deanon.deanonymize(text, mapping)
    assert "Jean Eude Dupont" in result


def test_fuzzy_matching_is_opt_in():
    # fuzzy carries a wrong-substitution risk, so the default must be exact-only.
    assert DeanonymizationConfig().fuzzy_matching is False


def test_deanonymize_fuzzy_preserves_whitespace_and_structure():
    # The old implementation rebuilt the text with split()/join(), flattening
    # every newline and indent in any response that carried PII. The fuzzy pass
    # must now splice replacements into the original string.
    config = DeanonymizationConfig(
        enabled=True, fuzzy_matching=True, fuzzy_threshold=0.75
    )
    deanon = DeAnonymizer(config)

    mapping = PIIMapping()
    mapping.add("Jean Eude", "Michel Deus", "PERSON")

    text = "# Report\n\n- item one\n- Michel Deu said hi\n\n```python\nx = 1\n```\n"
    result = deanon.deanonymize(text, mapping)

    assert "Jean Eude" in result  # the typo'd fake was still caught
    assert "# Report\n\n- item one\n- " in result  # markdown intact
    assert "```python\nx = 1\n```" in result  # code block intact


def test_deanonymize_fuzzy_no_match_leaves_text_untouched():
    config = DeanonymizationConfig(enabled=True, fuzzy_matching=True)
    deanon = DeAnonymizer(config)

    mapping = PIIMapping()
    mapping.add("Jean Eude", "Michel Deus", "PERSON")

    text = "line one\n    indented line\n\nfinal paragraph"
    assert deanon.deanonymize(text, mapping) == text


def test_deanonymize_fuzzy_never_rewrites_unknown_placeholders():
    # The model hallucinated <PERSON_3>; only <PERSON_1> is mapped. Fuzzy must
    # not inject person 1's PII into a placeholder that never existed.
    config = DeanonymizationConfig(
        enabled=True, fuzzy_matching=True, fuzzy_threshold=0.85
    )
    deanon = DeAnonymizer(config)

    mapping = PIIMapping()
    mapping.add("Marie Dupont", "<PERSON_1>", "PERSON")

    text = "Contact <PERSON_3> tomorrow"
    assert deanon.deanonymize(text, mapping) == text
