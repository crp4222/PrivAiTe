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


def test_reserve_walks_strings_keys_and_nested_containers():
    m = PIIMapping()
    m.reserve(
        {
            "<LOCATION_4>": ["see <PERSON_1> and <EMAIL_ADDRESS_12>", ("<SECRET_2>",)],
            "n": 3,
            "plain": "a < b and c_1 > d",
        }
    )

    for literal in ("<LOCATION_4>", "<PERSON_1>", "<EMAIL_ADDRESS_12>", "<SECRET_2>"):
        assert m.is_taken(literal)
    assert not m.is_taken("<PERSON_2>")
    # Reserving is not a detection and nothing becomes restorable.
    assert m.is_empty
    assert not m.has_detections


def test_add_literal_is_restorable_but_not_counted():
    m = PIIMapping()
    m.add_literal("<PERSON_1>", "<PERSON_2>")

    assert m.get_original("<PERSON_2>") == "<PERSON_1>"
    assert m.is_taken("<PERSON_2>")
    assert m.entity_type_counts() == {}
    assert not m.has_detections


def test_reserve_walks_a_self_referencing_request_once():
    # The LiteLLM guardrail passes its whole request dict, which can alias a
    # container or point back at itself.
    shared = ["see <PERSON_1>"]
    data: dict = {"a": shared, "b": shared, "c": ("<SECRET_2>",)}
    data["self"] = data

    m = PIIMapping()
    m.reserve(data)

    assert m.is_taken("<PERSON_1>")
    assert m.is_taken("<SECRET_2>")
