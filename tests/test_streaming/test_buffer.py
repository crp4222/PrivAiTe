from privaite.pii.mapping import PIIMapping
from privaite.streaming.buffer import StreamingDeAnonymizer


def _make_deanon(*pairs: tuple[str, str, str]) -> StreamingDeAnonymizer:
    mapping = PIIMapping()
    for original, fake, etype in pairs:
        mapping.add(original, fake, etype)
    return StreamingDeAnonymizer(mapping)


def test_no_fakes_passthrough():
    mapping = PIIMapping()
    deanon = StreamingDeAnonymizer(mapping)

    assert deanon.feed("Hello world") == "Hello world"
    assert deanon.flush() == ""


def test_complete_fake_in_one_token():
    deanon = _make_deanon(("Jean Eude", "Michel Deus", "PERSON"))

    result = deanon.feed("Michel Deus")
    assert result == "Jean Eude"
    assert deanon.flush() == ""


def test_fake_split_across_tokens():
    deanon = _make_deanon(("Jean Eude", "Michel Deus", "PERSON"))

    out1 = deanon.feed("Mic")
    out2 = deanon.feed("hel")
    out3 = deanon.feed(" De")
    out4 = deanon.feed("us")

    combined = out1 + out2 + out3 + out4
    assert combined == "Jean Eude"


def test_fake_prefix_false_alarm():
    deanon = _make_deanon(("Jean Eude", "Michel Deus", "PERSON"))

    out1 = deanon.feed("Mic")
    out2 = deanon.feed("key Mouse")

    combined = out1 + out2 + deanon.flush()
    assert "Mickey Mouse" in combined
    assert "Jean Eude" not in combined


def test_multiple_fakes_in_sequence():
    deanon = _make_deanon(
        ("Jean", "Michel", "PERSON"),
        ("jean@acme.com", "michel@example.net", "EMAIL_ADDRESS"),
    )

    out = deanon.feed("Michel at michel@example.net")
    out += deanon.flush()
    assert "Jean" in out
    assert "jean@acme.com" in out


def test_flush_emits_remaining():
    deanon = _make_deanon(("Jean Eude", "Michel Deus", "PERSON"))

    out1 = deanon.feed("Mic")
    remaining = deanon.flush()

    assert out1 + remaining == "Mic"


def test_interleaved_text_and_fakes():
    deanon = _make_deanon(("Jean Eude", "Michel Deus", "PERSON"))

    tokens = ["Hello ", "Michel", " Deus", ", how are you?"]
    output = ""
    for token in tokens:
        output += deanon.feed(token)
    output += deanon.flush()

    assert output == "Hello Jean Eude, how are you?"


def test_placeholder_in_one_token():
    deanon = _make_deanon(("jean michel", "<PERSON_1>", "PERSON"))

    output = deanon.feed("Bonjour <PERSON_1> !")
    output += deanon.flush()
    assert "jean michel" in output
    assert "<PERSON_1>" not in output


def test_placeholder_split_across_tokens():
    deanon = _make_deanon(("jean michel", "<PERSON_1>", "PERSON"))

    out = ""
    out += deanon.feed("Bonjour <PER")
    out += deanon.feed("SON_1")
    out += deanon.feed("> !")
    out += deanon.flush()
    assert "jean michel" in out
    assert "<PERSON_1>" not in out


def test_multiple_placeholders():
    deanon = _make_deanon(
        ("jean", "<PERSON_1>", "PERSON"),
        ("06123", "<PHONE_NUMBER_1>", "PHONE_NUMBER"),
    )

    out = deanon.feed("<PERSON_1> a le numero <PHONE_NUMBER_1>.")
    out += deanon.flush()
    assert "jean" in out
    assert "06123" in out
    assert "<PERSON_1>" not in out
    assert "<PHONE_NUMBER_1>" not in out
