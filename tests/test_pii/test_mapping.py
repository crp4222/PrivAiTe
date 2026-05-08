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


def test_placeholder_mapping():
    m = PIIMapping()
    m.add("jean michel", "<PERSON_1>", "PERSON")
    m.add("jean@test.com", "<EMAIL_ADDRESS_1>", "EMAIL_ADDRESS")

    assert m.get_original("<PERSON_1>") == "jean michel"
    assert m.get_original("<EMAIL_ADDRESS_1>") == "jean@test.com"
    assert m.get_fake("jean michel") == "<PERSON_1>"


def test_multiple_persons():
    m = PIIMapping()
    m.add("alice", "<PERSON_1>", "PERSON")
    m.add("bob", "<PERSON_2>", "PERSON")

    assert m.get_original("<PERSON_1>") == "alice"
    assert m.get_original("<PERSON_2>") == "bob"
    assert m.count == 2
