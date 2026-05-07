from privaite.pii.mapping import PIIMapping


def test_add_and_retrieve():
    m = PIIMapping()
    m.add("Jean Eude", "Michel Deus", "PERSON")

    assert m.get_fake("Jean Eude") == "Michel Deus"
    assert m.get_original("Michel Deus") == "Jean Eude"
    assert m.get_entity_type("Jean Eude") == "PERSON"


def test_has_original():
    m = PIIMapping()
    m.add("test@example.com", "fake@example.net", "EMAIL_ADDRESS")

    assert m.has_original("test@example.com")
    assert not m.has_original("other@example.com")


def test_get_all_fakes():
    m = PIIMapping()
    m.add("Alice Smith", "Bob Jones", "PERSON")
    m.add("alice@a.com", "bob@b.com", "EMAIL_ADDRESS")

    fakes = m.get_all_fakes()
    assert fakes["Bob Jones"] == "Alice Smith"
    assert fakes["bob@b.com"] == "alice@a.com"


def test_count():
    m = PIIMapping()
    assert m.count == 0
    assert m.is_empty

    m.add("A", "B", "EMAIL_ADDRESS")
    assert m.count == 1
    assert not m.is_empty


def test_same_original_same_fake():
    m = PIIMapping()
    m.add("Jean", "Michel", "EMAIL_ADDRESS")
    assert m.get_fake("Jean") == "Michel"

    m.add("Jean", "Michel", "EMAIL_ADDRESS")
    assert m.count == 1


def test_person_subparts_two_words():
    m = PIIMapping()
    m.add("jean michel", "Samuel Lewis", "PERSON")

    assert m.get_original("Samuel") == "jean"
    assert m.get_original("Lewis") == "michel"
    assert m.get_original("Samuel Lewis") == "jean michel"


def test_person_subparts_three_words():
    m = PIIMapping()
    m.add("jean michel trognieux", "Samuel Lewis", "PERSON")

    assert m.get_original("Samuel") == "jean"
    assert m.get_original("Lewis") == "michel trognieux"


def test_non_person_no_subparts():
    m = PIIMapping()
    m.add("Paris France", "London UK", "LOCATION")

    assert m.get_original("London UK") == "Paris France"
    assert m.get_original("London") is None


def test_subparts_dont_overwrite():
    m = PIIMapping()
    m.add("Jean Dupont", "Alice Martin", "PERSON")
    m.add("Jean Lefebvre", "Bob Martin", "PERSON")

    assert m.get_original("Alice") == "Jean"
    assert m.get_original("Martin") == "Dupont"
