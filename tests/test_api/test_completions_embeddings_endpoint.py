from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from privaite.app import create_app
from privaite.config.schema import (
    AnonymizationConfig,
    AuthConfig,
    DeanonymizationConfig,
    DetectorsConfig,
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


class FakeRouter:
    def __init__(self) -> None:
        self.completion_messages = None
        self.embedding_input = None

    def has_model(self, model: str) -> bool:
        return True

    async def completion(self, model_alias, messages, **kwargs):
        self.completion_messages = messages
        content = messages[-1]["content"]
        echoed = content if isinstance(content, str) else json.dumps(content)
        return {
            "object": "text_completion",
            "model": model_alias,
            "choices": [{"index": 0, "text": f"Echo: {echoed}"}],
        }

    async def embedding(self, model_alias, input_text, **kwargs):
        self.embedding_input = input_text
        return {
            "object": "list",
            "model": model_alias,
            "data": [{"index": 0, "embedding": [0.0, 0.1]}],
        }


def _make_app() -> tuple[object, FakeRouter]:
    config = PrivAiTeConfig(
        server=ServerConfig(host="127.0.0.1", port=8400),
        auth=AuthConfig(enabled=False),
        providers=[],
        pii=PIIConfig(
            enabled=True,
            detectors=DetectorsConfig(presidio=PresidioDetectorConfig(enabled=False)),
            anonymization=AnonymizationConfig(method="placeholder"),
            deanonymization=DeanonymizationConfig(enabled=True, fuzzy_matching=False),
        ),
        logging=LoggingConfig(format="text", level="debug"),
    )
    app = create_app(config)
    engine = PIIEngine(config.pii)
    engine.detectors = [
        FakeDetector({"Marie Dupont": "PERSON", "marie@acme.com": "EMAIL_ADDRESS"})
    ]
    engine._ready = True
    app.state.pii_engine = engine
    app.state.pii_tracker = PIITracker()
    router = FakeRouter()
    app.state.provider_router = router
    return app, router


@pytest.mark.asyncio
async def test_completions_string_prompt_anonymized_and_restored():
    app, router = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/completions",
            json={"model": "m", "prompt": "Contact Marie Dupont at marie@acme.com"},
        )

    assert resp.status_code == 200
    sent = router.completion_messages[-1]["content"]
    assert "Marie Dupont" not in sent
    assert "marie@acme.com" not in sent

    text = resp.json()["choices"][0]["text"]
    assert "Marie Dupont" in text
    assert "marie@acme.com" in text


@pytest.mark.asyncio
async def test_completions_list_prompt_anonymized():
    app, router = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/completions",
            json={"model": "m", "prompt": ["I am Marie Dupont", "nothing here"]},
        )

    assert resp.status_code == 200
    sent = json.dumps(router.completion_messages[-1]["content"])
    assert "Marie Dupont" not in sent


@pytest.mark.asyncio
async def test_embeddings_string_input_anonymized():
    app, router = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/embeddings",
            json={"model": "m", "input": "my email is marie@acme.com"},
        )

    assert resp.status_code == 200
    assert "marie@acme.com" not in router.embedding_input


@pytest.mark.asyncio
async def test_embeddings_list_input_anonymized():
    app, router = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/embeddings",
            json={"model": "m", "input": ["Marie Dupont", "clean"]},
        )

    assert resp.status_code == 200
    assert "Marie Dupont" not in json.dumps(router.embedding_input)
