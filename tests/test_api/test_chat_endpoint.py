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
        self.received_kwargs: dict | None = None
        self.response_message_extra: dict = {}

    def has_model(self, model: str) -> bool:
        return True

    async def completion(self, model_alias, messages, **kwargs):
        self.received_messages = messages
        self.received_kwargs = kwargs
        last = messages[-1]
        message: dict = {"role": "assistant", "content": None}
        if isinstance(last.get("content"), str):
            message["content"] = f"Echo: {last['content']}"
        if last.get("tool_calls"):
            message["tool_calls"] = last["tool_calls"]
        message.update(self.response_message_extra)
        return {
            "id": "fake",
            "object": "chat.completion",
            "model": model_alias,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        }


def _make_app(
    strict: bool = False,
    block_entities: list[str] | None = None,
    on_error: str = "block",
) -> tuple[object, FakeProviderRouter]:
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
            block_entities=block_entities or [],
            on_error=on_error,  # type: ignore[arg-type]
        ),
        logging=LoggingConfig(format="text", level="debug"),
    )
    app = create_app(config)

    engine = PIIEngine(config.pii)
    engine.detectors = [FakeDetector({"Marie Dupont": "PERSON", "marie@acme.com": "EMAIL_ADDRESS"})]
    engine._ready = True

    app.state.pii_engine = engine
    app.state.pii_tracker = PIITracker()
    router = FakeProviderRouter()
    app.state.provider_router = router
    return app, router


@pytest.mark.asyncio
async def test_provider_receives_anonymized_and_response_restored():
    app, router = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "Contact Marie Dupont at marie@acme.com"}],
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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

    out_args = resp.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(out_args) == json.loads(args)


@pytest.mark.asyncio
async def test_clean_message_passes_through_unchanged():
    app, router = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hello there"}]},
        )

    assert resp.status_code == 200
    assert router.received_messages[-1]["content"] == "hello there"


@pytest.mark.asyncio
async def test_strict_mode_returns_400_on_uninspectable_payload():
    app, router = _make_app(strict=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "Marie Dupont marie@acme.com"}],
            },
        )

    assert resp.status_code == 500
    assert router.received_messages is None


@pytest.mark.asyncio
async def test_on_error_allow_forwards_despite_engine_failure():
    # the explicit opt-out: when anonymization raises and on_error="allow", the
    # request goes through with the ORIGINAL (raw) messages. Untested until now.
    app, router = _make_app(on_error="allow")

    async def _boom(_messages):
        raise RuntimeError("detector exploded")

    app.state.pii_engine.process_request = _boom

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "Marie Dupont here"}],
            },
        )

    assert resp.status_code == 200
    assert router.received_messages[-1]["content"] == "Marie Dupont here"


@pytest.mark.asyncio
async def test_block_entities_rejects_and_forwards_nothing():
    # A blocked PII type in the request -> hard 400, provider never called, and
    # the error names the TYPE, never the value.
    app, router = _make_app(block_entities=["EMAIL_ADDRESS"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "reach marie@acme.com"}],
            },
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "pii_blocked"
    assert "EMAIL_ADDRESS" in body["error"]["message"]
    assert "marie@acme.com" not in body["error"]["message"]  # value never leaked
    assert router.received_messages is None


@pytest.mark.asyncio
async def test_refusal_and_audio_transcript_restored_in_response():
    # Restore parity: a refusal or an audio transcript that echoes a placeholder
    # must come back with the real value, like content and reasoning do.
    app, router = _make_app()
    router.response_message_extra = {
        "refusal": "I cannot email <PERSON_1>",
        "audio": {"id": "a1", "transcript": "sorry <PERSON_1>", "data": "abc"},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "I am Marie Dupont"}],
            },
        )

    assert resp.status_code == 200
    message = resp.json()["choices"][0]["message"]
    assert message["refusal"] == "I cannot email Marie Dupont"
    assert message["audio"]["transcript"] == "sorry Marie Dupont"
    assert message["audio"]["data"] == "abc"  # non-text audio fields untouched


@pytest.mark.asyncio
async def test_prediction_content_string_scrubbed_before_forward():
    # OpenAI predicted outputs (`prediction.content`) carry the client's current
    # document. It used to ride the kwargs passthrough to the provider verbatim.
    app, router = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "update the doc"}],
                "temperature": 0.2,
                "prediction": {
                    "type": "content",
                    "content": "Report written by Marie Dupont (marie@acme.com).",
                },
            },
        )

    assert resp.status_code == 200
    sent = router.received_kwargs["prediction"]["content"]
    assert "Marie Dupont" not in sent
    assert "marie@acme.com" not in sent
    assert "<PERSON_1>" in sent
    assert router.received_kwargs["prediction"]["type"] == "content"
    assert router.received_kwargs["temperature"] == 0.2  # other kwargs still pass


@pytest.mark.asyncio
async def test_prediction_content_part_list_scrubbed_before_forward():
    # prediction.content may also be an array of {"type": "text", "text": ...}.
    app, router = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "update the doc"}],
                "prediction": {
                    "type": "content",
                    "content": [{"type": "text", "text": "Author: Marie Dupont"}],
                },
            },
        )

    assert resp.status_code == 200
    parts = router.received_kwargs["prediction"]["content"]
    assert "Marie Dupont" not in json.dumps(parts)
    assert parts[0]["type"] == "text"


@pytest.mark.asyncio
async def test_web_search_user_location_scrubbed_before_forward():
    app, router = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "search the news"}],
                "web_search_options": {
                    "user_location": {
                        "type": "approximate",
                        "approximate": {"city": "chez Marie Dupont", "country": "FR"},
                    }
                },
            },
        )

    assert resp.status_code == 200
    sent = router.received_kwargs["web_search_options"]["user_location"]
    assert "Marie Dupont" not in json.dumps(sent)
    assert sent["approximate"]["country"] == "FR"


@pytest.mark.asyncio
async def test_block_entities_gate_covers_prediction_content():
    # The block gate is part of the single choke point, so a blocked type found
    # ONLY in prediction.content must reject the whole request.
    app, router = _make_app(block_entities=["EMAIL_ADDRESS"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "no pii here"}],
                "prediction": {"type": "content", "content": "reach marie@acme.com"},
            },
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "pii_blocked"
    assert router.received_messages is None  # nothing reached the provider


@pytest.mark.asyncio
async def test_block_entities_lets_non_blocked_pii_through_masked():
    # A non-blocked type (PERSON) is still masked and forwarded as usual.
    app, router = _make_app(block_entities=["EMAIL_ADDRESS"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "hi Marie Dupont"}],
            },
        )
    assert resp.status_code == 200
    sent = router.received_messages[-1]["content"]
    assert "Marie Dupont" not in sent
    assert "<PERSON_1>" in sent
