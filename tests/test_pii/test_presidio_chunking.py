"""Bounded spaCy input (`max_chunk_chars`).

spaCy's memory grows with the length of the text it parses, and an agent
request can carry a whole document in one leaf. With a piece size set, spaCy
parses consecutive pieces and the recognizers still run once over the whole
text, so no pattern ever sees a cut: only spaCy's own analysis next to one can
differ from parsing the text whole.
"""

from __future__ import annotations

import base64
import json

import pytest

from privaite.config.schema import PresidioDetectorConfig
from privaite.pii import detector_presidio
from privaite.pii.detector_presidio import PresidioDetector, nlp_pieces

PARAGRAPH = (
    "Het gesprek met de klant verliep rustig en er zijn geen vervolgafspraken "
    "gemaakt over de offerte of de levering van de nieuwe toestellen.\n"
)

VALUES = [
    ("EMAIL_ADDRESS", "marie.dupont@example.org"),
    ("PHONE_NUMBER", "+33 6 12 34 56 78"),
    ("IBAN_CODE", "DE89 3704 0044 0532 0130 00"),
    ("IP_ADDRESS", "203.0.113.7"),
    ("EMAIL_ADDRESS", "j.devries@example.nl"),
]

# Types only spaCy (or a recognizer reading spaCy's tokens and lemmas for
# context) emits. Everything else comes from patterns over the whole text.
SPACY_TYPES = {"PERSON", "LOCATION", "DATE_TIME", "NRP", "ORGANIZATION"}


def _long_text(target: int) -> str:
    """Filler with a known value every ~2,900 characters."""
    parts: list[str] = []
    length = 0
    i = 0
    while length < target:
        block = PARAGRAPH * 20
        _, value = VALUES[i % len(VALUES)]
        block += f"Contact: {value} voor vragen.\n"
        parts.append(block)
        length += len(block)
        i += 1
    return "".join(parts)


async def _detector(**config) -> PresidioDetector:
    detector = PresidioDetector(PresidioDetectorConfig(languages=["fr", "en"], **config))
    await detector.initialize()
    return detector


def _found(entities) -> set[tuple[str, int, int, str]]:
    return {(e.entity_type, e.start, e.end, e.text) for e in entities}


def _patterns(found):
    return {f for f in found if f[0] not in SPACY_TYPES}


def test_pieces_are_off_by_default():
    assert PresidioDetectorConfig().max_chunk_chars is None


def test_pieces_cover_the_text_back_to_back_and_cut_at_whitespace():
    text = _long_text(50_000)
    pieces = nlp_pieces(text, 5_000)

    assert pieces[0][0] == 0 and pieces[-1][1] == len(text)
    assert all(a_end == b_start for (_, a_end), (b_start, _) in zip(pieces, pieces[1:]))
    assert all(end - start <= 5_000 for start, end in pieces)
    assert all(text[end - 1].isspace() for _, end in pieces[:-1])


def test_a_run_without_whitespace_is_cut_where_it_is():
    text = "x" * 12_000
    assert nlp_pieces(text, 5_000) == [(0, 5_000), (5_000, 10_000), (10_000, 12_000)]


@pytest.mark.asyncio
async def test_no_spacy_call_sees_more_than_a_piece():
    detector = await _detector(max_chunk_chars=5_000)
    engine = detector._analyzer.nlp_engine
    seen: list[int] = []

    class Spy:
        def __init__(self, nlp):
            self._nlp = nlp

        def __call__(self, text, *args, **kwargs):
            seen.append(len(text))
            return self._nlp(text, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._nlp, name)

    for lang, nlp in list(engine.nlp.items()):
        engine.nlp[lang] = Spy(nlp)

    await detector.detect(_long_text(40_000), "fr")

    assert seen
    assert max(seen) <= 5_000


@pytest.mark.asyncio
async def test_pattern_detections_match_whole_text_detection():
    """spaCy's named entities depend on the words around them, so a cut can
    change one, on ordinary text next to the cut. Everything the patterns find
    is the same, because they run over the whole text either way."""
    text = _long_text(40_000)
    max_chars = 5_000
    whole = _found(await (await _detector()).detect(text, "fr"))
    pieced = _found(await (await _detector(max_chunk_chars=max_chars)).detect(text, "fr"))

    assert _patterns(pieced) == _patterns(whole)
    for _, value in VALUES:
        assert any(t == value and text[s:e] == value for _, s, e, t in pieced)

    cuts = [end for _, end in nlp_pieces(text, max_chars)[:-1]]
    for _, start, end, _ in pieced ^ whole:
        assert any(cut - 500 <= start and end <= cut + 500 for cut in cuts)


def _pem_block(lines: int) -> str:
    """PEM armor around a synthetic body (no real key): 64-character base64
    lines with no space anywhere inside."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    body = ["".join(alphabet[(i * 7 + j * 13) % 64] for j in range(64)) for i in range(lines)]
    return "-----BEGIN PRIVATE KEY-----\n" + "\n".join(body) + "\n-----END PRIVATE KEY-----"


def _jwt(payload_chars: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    payload = ("eyJzdWIiOiIxMjM0NTY3ODkwIn0" * (payload_chars // 27 + 1))[:payload_chars]
    return f"{header}.{payload}.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"


def _escaped_password_line(spaces: int) -> tuple[str, str]:
    """A long JSON line whose string value holds a CLI call with a quoted
    --password: a piece starting inside the string would read the escaped
    quotes the other way round."""
    value = "S3cr3t-Pa55w0rd-0123456789"
    command = "echo " + "x " * spaces + f'&& mysql --password="{value}" -h db --user app'
    return json.dumps({"command": command}), value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["rsa8192_key_block", "long_jwt", "escaped_password_in_json"],
)
async def test_a_long_or_quoted_value_across_a_cut_is_found_as_on_the_whole_text(case):
    """Values a chunked analysis could miss across a cut: longer than any fixed
    overlap, or inside a string a chunk would start in the middle of. The
    recognizers see the whole text, so the result is the whole-text result."""
    max_chars = 20_000
    if case == "rsa8192_key_block":
        value = _pem_block(100)  # ~6,500 characters, like an RSA-8192 key
        text = "woord " * 2_800 + value + " einde" * 1_000
        straddled = value
    elif case == "long_jwt":
        value = _jwt(6_000)  # covers the whole window a cut may move back into
        text = "woord " * 2_410 + "Authorization: Bearer " + value + " einde" * 1_000
        straddled = value
    else:
        straddled, value = _escaped_password_line(10_200)
        text = "woord " * 500 + "\n" + straddled + "\n" + " einde" * 1_000
    start = text.index(value)
    # A cut falls inside the value, or inside the JSON string before it.
    region = text.index(straddled)
    cuts = [end for _, end in nlp_pieces(text, max_chars)[:-1]]
    assert any(region < cut < start + len(value) for cut in cuts)

    whole = _found(await (await _detector()).detect(text, "en"))
    pieced = _found(await (await _detector(max_chunk_chars=max_chars)).detect(text, "en"))

    assert _patterns(pieced) == _patterns(whole)
    secrets = [(s, e) for t, s, e, _ in pieced if t == "SECRET"]
    assert any(s <= start and start + len(value) <= e for s, e in secrets)


@pytest.mark.asyncio
async def test_text_longer_than_spacy_max_length_is_analysed():
    detector = await _detector(max_chunk_chars=20_000)
    for nlp in detector._analyzer.nlp_engine.nlp.values():
        nlp.max_length = 50_000  # stands in for spaCy's 1,000,000 default
    text = _long_text(120_000)

    found = _found(await detector.detect(text, "fr"))

    assert any(t == "j.devries@example.nl" and s > 100_000 for _, s, _, t in found)


@pytest.mark.asyncio
async def test_pieces_that_do_not_rejoin_to_the_text_fail_the_detector(monkeypatch):
    """Offsets are only right if the pieces rejoin to the text exactly. When
    they do not, detection fails, so the request is blocked rather than masked
    at shifted offsets."""
    detector = await _detector(max_chunk_chars=5_000)
    monkeypatch.setattr(detector_presidio, "nlp_pieces", lambda text, _: [(0, len(text) - 1)])

    with pytest.raises(RuntimeError, match="did not rejoin"):
        await detector.detect(_long_text(10_000), "fr")
