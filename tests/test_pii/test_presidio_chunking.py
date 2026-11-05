"""Bounded Presidio input (`max_chunk_chars`).

spaCy's memory grows with the length of the text it parses, and an agent
request can carry a whole document in one leaf. With a chunk size set, no
analyze() call sees more than that many characters, and a long text yields
the same detections as when it is analysed whole.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from privaite.config.schema import PresidioDetectorConfig
from privaite.pii.detector_presidio import PresidioDetector

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


def _long_text(target: int) -> str:
    """Filler with a known value every ~2,900 characters, so several of them
    land on or near a chunk cut for the chunk sizes used below."""
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


def test_chunking_is_off_by_default():
    config = PresidioDetectorConfig()
    assert config.max_chunk_chars is None


def test_an_overlap_of_half_a_chunk_or_more_is_refused():
    with pytest.raises(ValidationError, match="less than half"):
        PresidioDetectorConfig(max_chunk_chars=2_000, chunk_overlap_chars=1_000)
    PresidioDetectorConfig(max_chunk_chars=2_000, chunk_overlap_chars=999)


@pytest.mark.asyncio
async def test_no_analyze_call_sees_more_than_a_chunk():
    detector = await _detector(max_chunk_chars=5_000, chunk_overlap_chars=500)
    seen: list[int] = []
    analyze = detector._analyzer.analyze

    def spy(*args, **kwargs):
        seen.append(len(kwargs["text"]))
        return analyze(*args, **kwargs)

    detector._analyzer.analyze = spy
    await detector.detect(_long_text(40_000), "fr")

    assert seen
    assert max(seen) <= 5_000


PATTERN_TYPES = {"EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE", "IP_ADDRESS", "CREDIT_CARD"}


@pytest.mark.asyncio
async def test_chunked_detection_matches_whole_text_detection():
    """Pattern recognizers give exactly the same result. spaCy's named
    entities depend on the words around them, so a cut can change one, but
    only within the overlap of a cut: everywhere else a chunk gives spaCy the
    same context the whole text does."""
    text = _long_text(40_000)
    max_chars, overlap = 5_000, 500
    whole = _found(await (await _detector()).detect(text, "fr"))
    chunked = _found(
        await (await _detector(max_chunk_chars=max_chars, chunk_overlap_chars=overlap)).detect(
            text, "fr"
        )
    )

    def patterns(found):
        return {f for f in found if f[0] in PATTERN_TYPES}

    assert patterns(chunked) == patterns(whole)
    # Every planted value is among them, at its real offset in the whole text.
    for _, value in VALUES:
        assert any(t == value and text[s:e] == value for _, s, e, t in chunked)

    from privaite.pii.detector_base import chunk_text

    edges = {o for o, _ in chunk_text(text, max_chars, overlap)[1:]} | {
        o + len(c) for o, c in chunk_text(text, max_chars, overlap)[:-1]
    }
    for _, start, end, _ in chunked ^ whole:
        assert any(edge - overlap <= start and end <= edge + overlap for edge in edges)


@pytest.mark.asyncio
async def test_a_value_on_the_cut_is_found_whole():
    max_chars, overlap = 2_000, 400
    filler = "woord " * 330  # 1,980 characters: the first cut lands in the e-mail
    email = "marie.dupont@example.org"
    text = filler + email + " " + "einde " * 400
    assert len(filler) < max_chars < len(filler) + len(email)

    detector = await _detector(max_chunk_chars=max_chars, chunk_overlap_chars=overlap)
    found = _found(await detector.detect(text, "fr"))

    start = len(filler)
    assert ("EMAIL_ADDRESS", start, start + len(email), email) in found


def _pem_block(lines: int) -> str:
    """A PEM-armored block shaped like an RSA-4096 key (synthetic body, no
    real key): 64-character base64 lines with no space anywhere inside."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    body = ["".join(alphabet[(i * 7 + j * 13) % 64] for j in range(64)) for i in range(lines)]
    return "-----BEGIN PRIVATE KEY-----\n" + "\n".join(body) + "\n-----END PRIVATE KEY-----"


@pytest.mark.asyncio
@pytest.mark.parametrize("overlap, whole", [(1_000, False), (4_000, True)])
async def test_a_key_block_across_a_cut_needs_an_overlap_as_long_as_the_block(overlap, whole):
    """The cut falls inside the block (it has no space to move back to), so
    only an overlap at least as long as the block keeps it whole in one chunk,
    where the structured-secret recognizer sees its armor."""
    key = _pem_block(50)  # ~3,300 characters, like an RSA-4096 key
    text = "woord " * 3_130 + key + " einde" * 1_000
    start = text.index(key)
    assert start < 20_000 < start + len(key)  # the block straddles the first chunk's end

    detector = await _detector(max_chunk_chars=20_000, chunk_overlap_chars=overlap)
    secrets = [e for e in await detector.detect(text, "en") if e.entity_type == "SECRET"]

    found_whole = any(e.start <= start and start + len(key) <= e.end for e in secrets)
    assert found_whole is whole
