from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from privaite.app import create_app
from privaite.config.schema import (
    AnonymizationConfig,
    AuthConfig,
    DeanonymizationConfig,
    DetectorsConfig,
    InspectConfig,
    LoggingConfig,
    PIIConfig,
    PresidioDetectorConfig,
    PrivAiTeConfig,
    ServerConfig,
)
from privaite.pii.detector_base import PIIDetector
from privaite.pii.engine import PIIEngine
from privaite.pii.entity import PIIEntity
from privaite.pii.tracker import PIITracker


class FakeDetector(PIIDetector):
    def __init__(self, terms: dict[str, str]) -> None:
        self.terms = terms

    @property
    def name(self) -> str:
        return "fake"

    async def initialize(self) -> None:
        pass

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        out: list[PIIEntity] = []
        for term, etype in self.terms.items():
            idx = text.find(term)
            if idx >= 0:
                out.append(PIIEntity(etype, term, idx, idx + len(term), 0.99, "fake"))
        return out


class FakeProviderRouter:
    """Records any call so tests can prove inspect never touches a provider."""

    def __init__(self) -> None:
        self.calls = 0

    def has_model(self, model: str) -> bool:
        return True

    async def completion(self, model_alias, messages, **kwargs):
        self.calls += 1
        return {}


def _make_app(
    inspect_enabled: bool = True,
    pii_enabled: bool = True,
    block_entities: list[str] | None = None,
) -> tuple[object, FakeProviderRouter]:
    config = PrivAiTeConfig(
        server=ServerConfig(host="127.0.0.1", port=8400),
        auth=AuthConfig(enabled=False),
        providers=[],
        pii=PIIConfig(
            enabled=pii_enabled,
            detectors=DetectorsConfig(presidio=PresidioDetectorConfig(enabled=False)),
            anonymization=AnonymizationConfig(method="placeholder"),
            deanonymization=DeanonymizationConfig(enabled=True),
            block_entities=block_entities or [],
            inspect=InspectConfig(enabled=inspect_enabled),
        ),
        logging=LoggingConfig(format="text", level="debug"),
    )
    app = create_app(config)

    if pii_enabled:
        engine = PIIEngine(config.pii)
        engine.detectors = [
            FakeDetector({"Marie Dupont": "PERSON", "marie@acme.com": "EMAIL_ADDRESS"})
        ]
        engine._ready = True
        app.state.pii_engine = engine
    else:
        app.state.pii_engine = None
    app.state.pii_tracker = PIITracker()
    router = FakeProviderRouter()
    app.state.provider_router = router
    return app, router


async def _post(app, payload) -> object:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/v1/pii/inspect", json=payload)


@pytest.mark.asyncio
async def test_inspect_disabled_by_default_returns_403():
    app, router = _make_app(inspect_enabled=False)
    resp = await _post(app, {"text": "Contact Marie Dupont"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "inspect_disabled"
    assert router.calls == 0


@pytest.mark.asyncio
async def test_inspect_returns_entities_spans_and_anonymized_preview():
    app, router = _make_app()
    text = "Contact Marie Dupont at marie@acme.com please"
    resp = await _post(app, {"text": text})

    assert resp.status_code == 200
    data = resp.json()

    types = {e["type"] for e in data["entities"]}
    assert types == {"PERSON", "EMAIL_ADDRESS"}
    for entity in data["entities"]:
        # Span offsets point at the exact substring in the submitted text.
        assert text[entity["start"] : entity["end"]] == entity["text"]
        # Each detected value maps to the placeholder the provider would see.
        assert entity["replacement"]
        assert entity["replacement"] in data["anonymized"]

    # The preview is what would leave for the provider: placeholders, no PII.
    assert "Marie Dupont" not in data["anonymized"]
    assert "marie@acme.com" not in data["anonymized"]
    assert data["would_block"] == []

    # Dry run: no provider was called, nothing was forwarded.
    assert router.calls == 0


@pytest.mark.asyncio
async def test_inspect_reports_would_block_without_anonymizing():
    app, router = _make_app(block_entities=["EMAIL_ADDRESS"])
    resp = await _post(app, {"text": "Reach marie@acme.com and Marie Dupont"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["would_block"] == ["EMAIL_ADDRESS"]
    # In production this request would be rejected, so there is no preview.
    assert data["anonymized"] is None
    # Detections are still reported (that is the point of the dry run).
    assert {e["type"] for e in data["entities"]} == {"PERSON", "EMAIL_ADDRESS"}
    assert router.calls == 0


@pytest.mark.asyncio
async def test_inspect_requires_nonempty_text():
    app, _ = _make_app()
    for payload in ({}, {"text": ""}, {"text": 42}, {"text": ["a"]}):
        resp = await _post(app, payload)
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_inspect_with_pii_disabled_returns_400():
    app, _ = _make_app(pii_enabled=False)
    resp = await _post(app, {"text": "whatever"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_inspect_clean_text_returns_empty_result():
    app, _ = _make_app()
    resp = await _post(app, {"text": "The build passed on the third try."})
    data = resp.json()
    assert resp.status_code == 200
    assert data["entities"] == []
    assert data["would_block"] == []
    # No detections: the preview equals the input (nothing to replace).
    assert data["anonymized"] == "The build passed on the third try."
