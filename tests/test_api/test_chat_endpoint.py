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


class FakeProviderRouter:
    """Captures the messages it is handed and echoes them back as a response."""

    def __init__(self) -> None:
        self.received_messages: list[dict] | None = None

    def has_model(self, model: str) -> bool:
        return True

    async def completion(self, model_alias, messages, **kwargs):
        self.received_messages = messages
        last = messages[-1]
        message: dict = {"role": "assistant", "content": None}
        if isinstance(last.get("content"), str):
            message["content"] = f"Echo: {last['content']}"
        if last.get("tool_calls"):
            message["tool_calls"] = last["tool_calls"]
        return {
            "id": "fake",
            "object": "chat.completion",
            "model": model_alias,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        }


def _make_app(strict: bool = False) -> tuple[object, FakeProviderRouter]:
    config = PrivAiTeConfig(
        server=ServerConfig(host="127.0.0.1", port=8400),
        auth=AuthConfig(enabled=False),
        providers=[],
        pii=PIIConfig(
            enabled=True,
            detectors=DetectorsConfig(presidio=PresidioDetectorConfig(enabled=False)),
            anonymization=AnonymizationConfig(method="placeholder"),
            deanonymization=DeanonymizationConfig(enabled=True, fuzzy_matching=False),
            strict=strict,
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
    router = FakeProviderRouter()
    app.state.provider_router = router
    return app, router


@pytest.mark.asyncio
async def test_provider_receives_anonymized_and_response_restored():
    app, router = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [
                    {"role": "user", "content": "Contact Marie Dupont at marie@acme.com"}
                ],
            },
        )

    assert resp.status_code == 200

    # The provider only ever saw placeholders, never the real PII.
    sent = router.received_messages[-1]["content"]
    assert "Marie Dupont" not in sent
    assert "marie@acme.com" not in sent
    assert "<PERSON_1>" in sent

    # The client gets the real values back.
    content = resp.json()["choices"][0]["message"]["content"]
    assert "Marie Dupont" in content
    assert "marie@acme.com" in content


@pytest.mark.asyncio
async def test_tool_call_arguments_anonymized_and_restored_through_endpoint():
    app, router = _make_app()
    args = json.dumps({"to": "marie@acme.com", "name": "Marie Dupont"})
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "send_email", "arguments": args},
                            }
                        ],
                    }
                ],
            },
        )

    assert resp.status_code == 200

    sent_args = router.received_messages[-1]["tool_calls"][0]["function"]["arguments"]
    assert "Marie Dupont" not in sent_args
    assert "marie@acme.com" not in sent_args

    out_args = resp.json()["choices"][0]["message"]["tool_calls"][0]["function"][
        "arguments"
    ]
    assert json.loads(out_args) == json.loads(args)


@pytest.mark.asyncio
async def test_clean_message_passes_through_unchanged():
    app, router = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hello there"}]},
        )

    assert resp.status_code == 200
    assert router.received_messages[-1]["content"] == "hello there"


@pytest.mark.asyncio
async def test_strict_mode_returns_400_on_uninspectable_payload():
    app, router = _make_app(strict=True)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": {"x": "y"}}]},
        )
    assert resp.status_code == 400
    assert router.received_messages is None


@pytest.mark.asyncio
async def test_pii_failure_fails_closed_by_default():
    # If anonymization raises, the default (on_error="block") must block the
    # request, never forward the raw PII to the provider.
    app, router = _make_app()

    async def _boom(_messages):
        raise RuntimeError("detector exploded")

    app.state.pii_engine.process_request = _boom

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "Marie Dupont marie@acme.com"}],
            },
        )

    assert resp.status_code == 500
    assert router.received_messages is None
