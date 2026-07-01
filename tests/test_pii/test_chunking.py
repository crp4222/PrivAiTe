"""Long-input handling: detectors backed by the HF pipeline used to silently
truncate at model_max_length, so PII past ~512 tokens was invisible."""

from __future__ import annotations

import pytest

from privaite.pii.detector_base import chunk_text


def test_short_text_is_a_single_chunk():
    assert chunk_text("hello world", max_chars=100) == [(0, "hello world")]


def test_chunks_cover_the_whole_text_with_overlap():
    text = " ".join(f"word{i}" for i in range(2000))
    chunks = chunk_text(text, max_chars=1500, overlap=200)

    assert len(chunks) > 1
    # every chunk is really at the offset it claims
    for offset, chunk in chunks:
        assert text[offset : offset + len(chunk)] == chunk
    # full coverage: consecutive chunks overlap, never leave a gap
    for (off_a, chunk_a), (off_b, _) in zip(chunks, chunks[1:]):
        assert off_b <= off_a + len(chunk_a)
    last_offset, last_chunk = chunks[-1]
    assert last_offset + len(last_chunk) == len(text)


def test_entity_on_a_boundary_is_whole_in_some_chunk():
    # place an email right where the first cut would land
    filler = "a" * 1490
    email = "marie.dupont@acme-corp.example.com"
    text = f"{filler} {email} more text " + "b" * 500
    chunks = chunk_text(text, max_chars=1500, overlap=200)

    assert any(email in chunk for _, chunk in chunks)


def test_chunking_never_loses_tail_text():
    text = "x" * 4000  # no whitespace at all: worst case for the cut heuristic
    chunks = chunk_text(text, max_chars=1500, overlap=200)
    covered_end = max(off + len(c) for off, c in chunks)
    assert covered_end == len(text)


@pytest.mark.asyncio
async def test_bert_detector_scans_past_the_old_truncation_horizon():
    # fake classifier that "sees" only its chunk: an email placed deep past
    # 512 tokens must still come back with correct absolute offsets.
    from privaite.config.schema import BertNERDetectorConfig
    from privaite.pii.detector_bert_ner import BertNERDetector

    email = "deep.target@acme.example"
    filler = " ".join(f"tok{i}" for i in range(1200))  # far beyond 512 tokens
    text = f"{filler} contact {email} thanks"

    def classifier(chunk: str):
        idx = chunk.find(email)
        if idx < 0:
            return []
        return [{
            "entity_group": "PER",  # mapped type in the default label_mapping
            "score": 0.99,
            "start": idx,
            "end": idx + len(email),
            "word": email,
        }]

    detector = BertNERDetector(BertNERDetectorConfig(enabled=True))
    detector._classifier = classifier

    entities = await detector.detect(text)

    assert len(entities) == 1
    ent = entities[0]
    assert text[ent.start : ent.end] == email
    assert ent.entity_type == "PERSON"


def test_onnx_download_tolerates_single_file_variants(monkeypatch, tmp_path):
    # a variant without externalized weights has no .onnx_data side file; the
    # download must not fail on it (it used to require both files).
    from huggingface_hub.errors import EntryNotFoundError

    from privaite.pii import detector_onnx

    model_file = tmp_path / "model_q4.onnx"
    model_file.write_bytes(b"onnx")

    def fake_download(repo_id, filename, cache_dir=None, revision=None):
        if filename.endswith(".onnx_data"):
            raise EntryNotFoundError("no side file")
        return str(model_file)

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)

    path = detector_onnx.download_onnx_model(variant="q4")
    assert path == model_file
