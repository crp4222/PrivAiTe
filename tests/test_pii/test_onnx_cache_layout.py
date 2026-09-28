"""Loading the ONNX model from the Hugging Face cache layout.

onnxruntime refuses an external data file that resolves outside the model
file's real folder. huggingface_hub 1.33 moved cached files into a cache-wide
blob store sharded by hash prefix (hub/blobs/60/..., hub/blobs/88/...), so the
model and its .onnx_data resolve to different folders and the default preset
stopped starting from a fresh cache. These tests rebuild that layout with a
real (tiny) ONNX model and a real onnxruntime.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
from pathlib import Path

import numpy as np
import pytest

from privaite.pii import detector_onnx
from privaite.pii.detector_onnx import colocate_external_data

ort = pytest.importorskip("onnxruntime")

# y = x @ w with w = [[0, 1], [2, 3]] stored as external data in tiny.onnx_data
# (built with onnx.save_model(..., save_as_external_data=True), opset 13, IR 8).
_TINY_ONNX = base64.b64decode(
    "CAg6hwEKEQoBeAoBdxIBeSIGTWF0TXVsEgR0aW55KkIIAggCEAFCAXdqGgoIbG9jYXRpb24SDnRpbnkub25u"
    "eF9kYXRhagsKBm9mZnNldBIBMGoMCgZsZW5ndGgSAjE2cAFaEwoBeBIOCgwIARIICgIIAQoCCAJiEwoBeRIO"
    "CgwIARIICgIIAQoCCAJCBAoAEA0="
)
_TINY_DATA = base64.b64decode("AAAAAAAAgD8AAABAAABAQA==")


def _hub_cache(tmp_path: Path, *, shared_store: bool) -> tuple[Path, Path, Path]:
    """Lay the model out the way huggingface_hub does: snapshot symlinks to
    blobs, in one folder per repo (before 1.33) or in hash-prefix shards of a
    cache-wide store (1.33 and later). Returns (hub root, model, data)."""
    hub = tmp_path / "hub"
    repo = hub / "models--org--tiny"
    if shared_store:
        blob_model, blob_data = hub / "blobs" / "60" / ("6" * 64), hub / "blobs" / "88" / ("8" * 64)
    else:
        blob_model, blob_data = repo / "blobs" / ("6" * 64), repo / "blobs" / ("8" * 64)
    for blob, content in ((blob_model, _TINY_ONNX), (blob_data, _TINY_DATA)):
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(content)
    snapshot = repo / "snapshots" / "rev" / "onnx"
    snapshot.mkdir(parents=True)
    (snapshot / "tiny.onnx").symlink_to(blob_model)
    (snapshot / "tiny.onnx_data").symlink_to(blob_data)
    return hub, snapshot / "tiny.onnx", snapshot / "tiny.onnx_data"


def _run(model: Path) -> list:
    session = ort.InferenceSession(str(model))
    return session.run(None, {"x": np.ones((1, 2), dtype=np.float32)})[0].tolist()


def test_shared_blob_store_layout_is_refused_by_onnxruntime_as_is(tmp_path):
    # The failure this file exists for, reproduced: without colocation the
    # default model does not load from a hub >= 1.33 cache.
    _, model, _ = _hub_cache(tmp_path, shared_store=True)
    with pytest.raises(Exception, match="External data"):
        ort.InferenceSession(str(model))


def test_shared_blob_store_layout_loads_after_colocation(tmp_path):
    hub, model, data = _hub_cache(tmp_path, shared_store=True)

    loadable = colocate_external_data(model, data)

    assert loadable.parent.parent == hub / "models--org--tiny" / "privaite-onnx"
    assert loadable.name == "tiny.onnx"
    assert _run(loadable) == [[2.0, 4.0]]
    # Hard links, not copies: no second 800 MB on disk for the real model.
    assert os.path.samefile(loadable, model)
    assert os.path.samefile(loadable.with_name("tiny.onnx_data"), data)


def test_colocation_is_idempotent(tmp_path, caplog):
    _, model, data = _hub_cache(tmp_path, shared_store=True)

    with caplog.at_level(logging.INFO, logger="privaite.pii.detector_onnx"):
        first = colocate_external_data(model, data)
        placed = [r.getMessage() for r in caplog.records]
        caplog.clear()
        second = colocate_external_data(model, data)

    assert second == first
    # Logged when the files are placed, not on every later start.
    assert placed == [f"Placed tiny.onnx, tiny.onnx_data side by side in {first.parent}"]
    assert caplog.records == []
    assert sorted(p.name for p in first.parent.iterdir()) == ["tiny.onnx", "tiny.onnx_data"]
    assert _run(second) == [[2.0, 4.0]]


def test_per_repo_blob_layout_is_left_alone(tmp_path):
    # Before hub 1.33 both blobs share the repo's blobs folder, which
    # onnxruntime accepts: nothing is linked or created.
    hub, model, data = _hub_cache(tmp_path, shared_store=False)

    assert colocate_external_data(model, data) == model
    assert not (hub / "models--org--tiny" / "privaite-onnx").exists()
    assert _run(model) == [[2.0, 4.0]]


def test_single_file_model_is_left_alone(tmp_path):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"onnx")
    assert colocate_external_data(model, None) == model


def test_colocation_copies_when_the_filesystem_refuses_links(tmp_path, monkeypatch):
    _, model, data = _hub_cache(tmp_path, shared_store=True)

    def no_links(*args, **kwargs):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", no_links)
    loadable = colocate_external_data(model, data)

    assert not os.path.samefile(loadable, model)
    assert _run(loadable) == [[2.0, 4.0]]


def test_colocation_failure_says_where_and_what_to_do(tmp_path, monkeypatch):
    # A read-only cache: startup must fail with the way out, not a bare errno.
    _, model, data = _hub_cache(tmp_path, shared_store=True)

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "link", refuse)
    monkeypatch.setattr(shutil, "copyfile", refuse)

    with pytest.raises(RuntimeError, match="Permission denied") as info:
        colocate_external_data(model, data)
    assert "privaite-onnx" in str(info.value)
    assert "HF_HUB_DISABLE_SHARED_BLOBS=1" in str(info.value)


def test_download_returns_a_loadable_path_from_a_shared_store_cache(tmp_path, monkeypatch):
    # The wiring: download_onnx_model (also run by the Dockerfile at build
    # time) hands the colocated path to the detector.
    _, model, data = _hub_cache(tmp_path, shared_store=True)

    def fake_download(repo_id, filename, cache_dir=None, revision=None):
        return str(data if filename.endswith(".onnx_data") else model)

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)

    path = detector_onnx.download_onnx_model(variant="q4f16")

    assert path != model
    assert _run(path) == [[2.0, 4.0]]
