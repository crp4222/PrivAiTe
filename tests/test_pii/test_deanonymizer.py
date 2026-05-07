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
