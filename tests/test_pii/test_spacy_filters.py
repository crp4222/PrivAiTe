"""What the Presidio detector keeps from spaCy's statistical NER, with the real models.

Each case below is a false positive or a miss that was fixed once (commit
messages of May and September 2026) and was not pinned by a test until now.
"""

import asyncio

import pytest

from privaite.config.schema import PIIConfig
from privaite.pii.detector_presidio import PresidioDetector, _date_pieces

ORDERS = {"fr,en": ["fr", "en"], "en,fr": ["en", "fr"]}


@pytest.fixture(scope="module")
def detectors():
    loop = asyncio.new_event_loop()
    built = {}
    for key, languages in ORDERS.items():
        detector = PresidioDetector(
            PIIConfig(
                preset="onnx", detectors={"presidio": {"languages": languages}}
            ).detectors.presidio
        )
        loop.run_until_complete(detector.initialize())
        built[key] = detector
    yield lambda key, text: loop.run_until_complete(built[key].detect(text))
    loop.close()


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize(
    "text",
    [
        # English NER on French text read "résumer" as a person (3c1a85f).
        "Peux-tu me résumer ces informations dans un tableau ?",
        "Quel temps fait-il aujourd'hui ?",
        "Explique-moi comment fonctionne Python.",
        # Code tokens (ba27c4b).
        "I write c++ and c# code every day.",
        # Ordinary words read as places (071a7a2, 9ba5785).
        "Le PIB de la zone Euro a augmenté.",
    ],
)
def test_historical_false_positives_stay_fixed(detectors, order, text):
    assert detectors(order, text) == []


def test_a_location_needs_context(detectors):
    found = detectors("en,fr", "We deploy on Kubernetes every Saturday.")
    assert "LOCATION" not in {e.entity_type for e in found}


@pytest.mark.parametrize(
    "text",
    [
        "def connect(api_key, timeout): ...",
        "        f.write(json.dumps(entry))\n        time.sleep(0.1)",
        "    created_at = record[2026]\n    return created_at",
    ],
)
def test_spacy_dates_do_not_rewrite_code(detectors, text):
    found = detectors("en,fr", text)
    assert not any(e.entity_type == "DATE_TIME" and set(e.text) & set("(){}[]=_") for e in found)


@pytest.mark.parametrize(
    ("text", "date"),
    [
        ("The meeting is on October 9, 1954 at 10:18 PM.", "October 9, 1954"),
        ("The meeting is on October 9, 1954 at 10:18 PM.", "10:18 PM"),
        ("She was born on August 1, 1995 in Leeds.", "August 1, 1995"),
        ("The payment is due 9/29 at 1619.", "9/29"),
        ("Your appointment is on 9/29/2026.", "9/29/2026"),
        ("The incident happened on 2026-09-12.", "2026-09-12"),
    ],
)
def test_real_spacy_dates_are_kept(detectors, text, date):
    found = detectors("en,fr", text)
    assert date in [e.text for e in found if e.entity_type == "DATE_TIME"]


def pieces(span: str) -> list[str]:
    return [span[start:end] for start, end in _date_pieces(span, 0, len(span))]


@pytest.mark.parametrize(
    "span",
    ["October 9, 1954", "10:18 PM", "9/29", "2026-09-12", "tomorrow", "Saturday", "1:25pm"],
)
def test_a_date_span_with_nothing_to_cut_is_kept_whole(span):
    assert pieces(span) == [span]


@pytest.mark.parametrize(
    "span",
    [
        # Prose punctuation and line breaks are not code: such a span stays as it was.
        "twenty (20) years",
        "thirty (30) days",
        "(minutes",
        "the twelfth of\nSeptember",
        "April, August, September,\n    November",
        "May 3, 2025\n\nDear Sir/Madam",
        "32\n2026-09-27",
        "6 12 34 56 78).\nShe",
        "54 32,NL91ABNA0417164300,2026-08-21\n5,I",
        'the "best" day',
    ],
)
def test_a_span_without_code_is_kept_whole(span):
    assert pieces(span) == [span]


@pytest.mark.parametrize(
    "span",
    [
        "connect(api_key",
        "f:\n            f.write(json.dumps(entre",
        "{date}",
        "RESPONSES_JSON_FRAGMENT_EVENTS",
        "detector().name",
    ],
)
def test_code_without_digits_is_dropped(span):
    assert pieces(span) == []


@pytest.mark.parametrize(
    ("span", "kept"),
    [
        # Every number the uncut span covered stays covered.
        ("created_at=2026-09-12", ["2026-09-12"]),
        ("cfg[2026]", ["2026"]),
        ("text[end - 1", ["end - 1"]),
        ("run(at=2026-09-12, every=7)", ["2026-09-12, every", "7"]),
    ],
)
def test_a_cut_span_keeps_every_piece_with_a_digit(span, kept):
    assert pieces(span) == kept


def test_pieces_keep_absolute_offsets():
    text = "log: created_at=2026-09-12 done"
    start = text.index("created_at")
    end = text.index(" done")
    assert [text[a:b] for a, b in _date_pieces(text, start, end)] == ["2026-09-12"]


@pytest.mark.parametrize(
    ("order", "text"),
    [
        ("en,fr", "Hi, I am Marie Dupont."),
        ("fr,en", "Bonjour, je m'appelle Marie Dupont."),
    ],
)
def test_the_first_language_finds_names_in_its_own_text(detectors, order, text):
    """docs/configuration.md: spaCy's NER is kept for the first language only."""
    assert ("PERSON", "Marie Dupont") in [(e.entity_type, e.text) for e in detectors(order, text)]
