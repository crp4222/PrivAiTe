"""The ONNX variant of the Privacy Filter that ships by default, and every place
that has to fetch that same variant.

q4 and q4f16 hold the same 4-bit weights; q4f16 computes in fp16, which CPUs do
not run natively, q4 in fp32. Measured on the comparative benchmark: q4 runs the
default preset 1.5x faster on CPU with the same detections (247 of 258 outputs
identical, no labelled value lost). q4f16 stays selectable for GPU sessions.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from privaite.config.schema import OnnxDetectorConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_default_variant_is_q4() -> None:
    assert OnnxDetectorConfig().onnx_variant == "q4"


def test_download_without_a_variant_fetches_the_configured_default(monkeypatch, tmp_path) -> None:
    """The download helper used to carry its own default, so a caller that did
    not pass a variant (the Docker image prefetch) fetched a different file than
    the one the detector then asked for."""
    import huggingface_hub

    from privaite.pii import detector_onnx

    requested: list[str] = []

    def fake_download(repo_id, filename, cache_dir=None, revision=None):
        requested.append(filename)
        path = tmp_path / Path(filename).name
        path.write_bytes(b"onnx")
        return str(path)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)

    detector_onnx.download_onnx_model()

    default = OnnxDetectorConfig().onnx_variant
    assert requested == [f"onnx/model_{default}.onnx", f"onnx/model_{default}.onnx_data"]


def _dockerfile_prefetch_code() -> str:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    snippets = re.findall(r'python -c "([^"]*download_onnx_model[^"]*)"', dockerfile)
    assert len(snippets) == 1, "expected exactly one model prefetch in the Dockerfile"
    return snippets[0]


def _variant_baked_by_dockerfile(monkeypatch) -> str:
    from privaite.pii import detector_onnx

    calls: list[dict] = []
    monkeypatch.setattr(detector_onnx, "download_onnx_model", lambda **kwargs: calls.append(kwargs))
    exec(_dockerfile_prefetch_code(), {})
    assert len(calls) == 1
    return calls[0]["variant"]


def test_docker_image_bakes_the_configured_default_variant(monkeypatch) -> None:
    """The image runs offline, so the variant it prefetches must be the one the
    detector loads at startup; otherwise the first boot downloads again."""
    monkeypatch.delenv("ONNX_VARIANT", raising=False)
    assert _variant_baked_by_dockerfile(monkeypatch) == OnnxDetectorConfig().onnx_variant


@pytest.mark.parametrize("empty", ["", None])
def test_an_unset_build_arg_keeps_the_default(monkeypatch, empty) -> None:
    # `ARG ONNX_VARIANT` with no value reaches the build as an empty string.
    if empty is None:
        monkeypatch.delenv("ONNX_VARIANT", raising=False)
    else:
        monkeypatch.setenv("ONNX_VARIANT", empty)
    assert _variant_baked_by_dockerfile(monkeypatch) == OnnxDetectorConfig().onnx_variant


def test_the_build_arg_bakes_another_variant(monkeypatch) -> None:
    monkeypatch.setenv("ONNX_VARIANT", "q4f16")
    assert _variant_baked_by_dockerfile(monkeypatch) == "q4f16"
    assert re.search(r"^ARG ONNX_VARIANT", (REPO_ROOT / "Dockerfile").read_text(), re.M)


async def test_a_missing_model_stops_the_engine_instead_of_running_without_it(monkeypatch) -> None:
    """An upgraded install on a host with no network and only the old variant in
    its cache must fail at startup: starting without the detector would forward
    PII, which fail-closed forbids. The docs promise this, so it is pinned."""
    from huggingface_hub.errors import LocalEntryNotFoundError

    from privaite.config.schema import (
        DetectorsConfig,
        OnnxDetectorConfig,
        PIIConfig,
        PresidioDetectorConfig,
    )
    from privaite.pii import detector_onnx
    from privaite.pii.engine import PIIEngine

    asked: list[str] = []

    def offline(repo_id, variant=None, cache_dir=None, revision=None):
        asked.append(variant)
        raise LocalEntryNotFoundError("cannot reach the hub and the file is not cached")

    monkeypatch.setattr(detector_onnx, "download_onnx_model", offline)

    # preset=None: the default preset would switch Presidio back on, and this
    # test wants the ONNX detector to be the only one that matters.
    config = PIIConfig(
        enabled=True,
        preset=None,
        detectors=DetectorsConfig(
            presidio=PresidioDetectorConfig(enabled=False),
            onnx=OnnxDetectorConfig(enabled=True),
        ),
    )
    engine = PIIEngine(config)
    with pytest.raises(LocalEntryNotFoundError):
        await engine.initialize()
    assert asked == [OnnxDetectorConfig().onnx_variant]
    assert engine.detectors == []
