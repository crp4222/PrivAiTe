"""Nothing PrivAiTe loads may contact a third party on its own.

Measured on the 0.7.1 image with a packet capture: ONNX Runtime 1.29+ uploaded
telemetry to mobile.events.data.microsoft.com, LiteLLM fetched its price table
from raw.githubusercontent.com, the Hub was asked about files already in the
cache, and tldextract downloaded the public suffix list. None of it carried
request data. All of it contradicted "only your provider request leaves".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from privaite.config.schema import OnnxDetectorConfig, PIIConfig
from privaite.pii import detector_onnx
from tests.test_pii.test_detector_onnx import _fake_runtime

REPO_ROOT = Path(__file__).resolve().parents[1]
SWITCHES = {
    "ORT_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
}
PINNED = "a" * 40


def _environment_after_import(extra: dict[str, str]) -> dict[str, str | None]:
    env = {k: v for k, v in os.environ.items() if k not in SWITCHES}
    env.update(extra)
    code = (
        "import json, os, privaite; "
        f"print(json.dumps({{k: os.environ.get(k) for k in {sorted(SWITCHES)!r}}}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_importing_privaite_switches_dependency_telemetry_off() -> None:
    """Before onnxruntime, huggingface_hub or litellm read their environment."""
    assert _environment_after_import({}) == SWITCHES


def test_a_value_the_operator_exported_is_kept() -> None:
    kept = _environment_after_import({"LITELLM_LOCAL_MODEL_COST_MAP": "False"})
    assert kept["LITELLM_LOCAL_MODEL_COST_MAP"] == "False"
    assert kept["ORT_DISABLE_TELEMETRY"] == "1"


def test_the_docker_image_sets_the_same_switches() -> None:
    """The image also runs Python before privaite is imported (the model prefetch)."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    for name, value in SWITCHES.items():
        assert f"{name}={value}" in dockerfile, name


@pytest.mark.asyncio
async def test_the_onnx_session_turns_runtime_telemetry_off(monkeypatch, tmp_path) -> None:
    """A host that initialised ONNX Runtime before importing privaite (Open
    WebUI does) never reads the environment variable again."""
    _fake_runtime(monkeypatch, tmp_path, ["CPUExecutionProvider"])
    calls: list[str] = []
    sys.modules["onnxruntime"].disable_telemetry_events = lambda: calls.append("off")  # type: ignore[attr-defined]

    await detector_onnx.OnnxPrivacyFilterDetector(OnnxDetectorConfig()).initialize()

    assert calls == ["off"]


def _hub(monkeypatch, cache: dict[str, object]) -> list[str]:
    """Fake the two Hub entry points; return the files a download was asked for."""
    import huggingface_hub

    downloaded: list[str] = []

    def lookup(repo_id, filename, cache_dir=None, revision=None, repo_type=None):
        return cache.get(filename)

    def download(repo_id, filename, cache_dir=None, revision=None):
        downloaded.append(filename)
        path = Path(cache_dir) / Path(filename).name
        path.write_bytes(b"onnx")
        return str(path)

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", lookup)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    return downloaded


def test_a_pinned_model_in_the_cache_is_loaded_without_asking_the_hub(
    monkeypatch, tmp_path
) -> None:
    """A commit never changes: what the cache knows about it is final, including
    that the single-file variant has no side file. That last question used to be
    asked on every start."""
    import huggingface_hub

    model = tmp_path / "model_q4.onnx"
    model.write_bytes(b"onnx")
    downloaded = _hub(
        monkeypatch,
        {
            "onnx/model_q4.onnx": str(model),
            "onnx/model_q4.onnx_data": huggingface_hub._CACHED_NO_EXIST,
        },
    )

    path = detector_onnx.download_onnx_model(variant="q4", cache_dir=str(tmp_path), revision=PINNED)

    assert path.read_bytes() == b"onnx"
    assert downloaded == []


def test_a_pinned_model_missing_from_the_cache_is_downloaded(monkeypatch, tmp_path) -> None:
    downloaded = _hub(monkeypatch, {})

    detector_onnx.download_onnx_model(variant="q4", cache_dir=str(tmp_path), revision=PINNED)

    assert downloaded == ["onnx/model_q4.onnx", "onnx/model_q4.onnx_data"]


def test_a_moving_revision_still_asks_the_hub(monkeypatch, tmp_path) -> None:
    """A branch name can move: only the Hub knows what it points at today."""
    model = tmp_path / "model_q4.onnx"
    model.write_bytes(b"stale")
    downloaded = _hub(monkeypatch, {"onnx/model_q4.onnx": str(model)})

    detector_onnx.download_onnx_model(variant="q4", cache_dir=str(tmp_path), revision="main")

    assert downloaded == ["onnx/model_q4.onnx", "onnx/model_q4.onnx_data"]


def _tokenizer_calls(monkeypatch, tmp_path, cached: bool) -> list[dict]:
    _fake_runtime(monkeypatch, tmp_path, ["CPUExecutionProvider"])
    calls: list[dict] = []

    class Tokenizer:
        @staticmethod
        def from_pretrained(model_name, **kwargs):
            calls.append(kwargs)
            if kwargs.get("local_files_only") and not cached:
                raise OSError("not in the cache")
            return types.SimpleNamespace(num_special_tokens_to_add=lambda: 0)

    sys.modules["transformers"].AutoTokenizer = Tokenizer  # type: ignore[attr-defined]
    return calls


@pytest.mark.asyncio
async def test_a_cached_tokenizer_is_loaded_without_asking_the_hub(monkeypatch, tmp_path) -> None:
    calls = _tokenizer_calls(monkeypatch, tmp_path, cached=True)

    await detector_onnx.OnnxPrivacyFilterDetector(OnnxDetectorConfig()).initialize()

    assert [c.get("local_files_only", False) for c in calls] == [True]


@pytest.mark.asyncio
async def test_a_tokenizer_missing_from_the_cache_is_downloaded(monkeypatch, tmp_path) -> None:
    calls = _tokenizer_calls(monkeypatch, tmp_path, cached=False)

    await detector_onnx.OnnxPrivacyFilterDetector(OnnxDetectorConfig()).initialize()

    assert [c.get("local_files_only", False) for c in calls] == [True, False]


@pytest.mark.asyncio
async def test_a_moving_tokenizer_revision_still_asks_the_hub(monkeypatch, tmp_path) -> None:
    calls = _tokenizer_calls(monkeypatch, tmp_path, cached=True)

    config = OnnxDetectorConfig(revision="main")
    await detector_onnx.OnnxPrivacyFilterDetector(config).initialize()

    assert [c.get("local_files_only", False) for c in calls] == [False]


@pytest.mark.asyncio
async def test_presidio_validates_domains_with_the_bundled_suffix_list(monkeypatch) -> None:
    """Presidio's email recognizer calls tldextract, which downloads the public
    suffix list on first use."""
    import requests
    import tldextract
    from tldextract import tldextract as tld_module

    from privaite.pii.detector_presidio import PresidioDetector

    monkeypatch.setattr(
        tld_module.TLD_EXTRACTOR,
        "suffix_list_urls",
        ("https://publicsuffix.org/list/public_suffix_list.dat",),
    )
    monkeypatch.setattr(tld_module.TLD_EXTRACTOR, "_extractor", None)

    def no_network(*args, **kwargs):
        raise AssertionError("tldextract tried to download the suffix list")

    monkeypatch.setattr(requests.sessions.Session, "request", no_network)

    detector = PresidioDetector(PIIConfig(preset="light").detectors.presidio)
    await detector.initialize()

    assert tld_module.TLD_EXTRACTOR.suffix_list_urls == ()
    assert tldextract.extract("marie@mail.example.co.uk").suffix == "co.uk"
    found = await detector.detect("write to marie.dupont@example.co.uk today")
    assert "EMAIL_ADDRESS" in [e.entity_type for e in found]


def test_a_variant_the_hub_does_not_have_still_fails_with_the_hub_error(
    monkeypatch, tmp_path
) -> None:
    """Reading the cache first must not turn a wrong variant name into a silent
    success or a different error: the Hub's own answer is what the operator reads."""
    import huggingface_hub
    from huggingface_hub.errors import EntryNotFoundError

    def missing(repo_id, filename, cache_dir=None, revision=None):
        raise EntryNotFoundError(f"{filename} is not in {repo_id}")

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", lambda *a, **k: None)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", missing)

    with pytest.raises(EntryNotFoundError):
        detector_onnx.download_onnx_model(variant="nope", cache_dir=str(tmp_path), revision=PINNED)


def test_a_cached_side_file_is_used_without_asking_the_hub(monkeypatch, tmp_path) -> None:
    model = tmp_path / "model_q4f16.onnx"
    model.write_bytes(b"onnx")
    weights = tmp_path / "model_q4f16.onnx_data"
    weights.write_bytes(b"weights")
    downloaded = _hub(
        monkeypatch,
        {"onnx/model_q4f16.onnx": str(model), "onnx/model_q4f16.onnx_data": str(weights)},
    )

    path = detector_onnx.download_onnx_model(
        variant="q4f16", cache_dir=str(tmp_path), revision=PINNED
    )

    assert downloaded == []
    assert (path.parent / "model_q4f16.onnx_data").read_bytes() == b"weights"
