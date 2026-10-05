"""Plaintext restored by a chat response must be scrubbed when history echoes it."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from privaite.config.schema import PIIConfig
from privaite.pii.engine import PIIBlockedError, PIIEngine, PIIProcessingError
from privaite.pii.entity import PIIEntity
from tests.test_api.test_chat_endpoint import _make_app, _sse_payloads
from tests.test_integrations.test_litellm_guardrail import _guardrail
from tests.test_integrations.test_openwebui_filter import _load_filter
from tests.test_pii.test_structured import FakeDetector

FIELDS = ("reasoning_content", "reasoning", "refusal", "audio.transcript")
FIRST = "fixture-only-primary-secret"
SECOND = "fixture-only-secondary-secret"
TEXT = f"The password is {FIRST}; the API key is {SECOND}."


def message_field(field: str, value: Any) -> dict:
    if field == "audio.transcript":
        return {"audio": {"id": "audio-id", "data": "opaque-bytes", "transcript": value}}
    return {field: value}


def field_value(message: dict, field: str) -> Any:
    if field == "audio.transcript":
        return message.get("audio", {}).get("transcript")
    return message.get(field)


class CountingDetector(FakeDetector):
    def __init__(self, fail: bool = False) -> None:
        super().__init__({FIRST: "SECRET", SECOND: "SECRET"})
        self.calls = 0
        self.fail = fail

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"detector failed on {text}")
        return await super().detect(text, language)


def make_engine(
    *, method: str = "placeholder", block: bool = False, fail: bool = False, **config: Any
) -> tuple[PIIEngine, CountingDetector]:
    engine = PIIEngine(
        PIIConfig(
            preset=None,
            anonymization={"method": method},
            block_entities=["SECRET"] if block else [],
            detection_cache={"enabled": True},
            **config,
        )
    )
    detector = CountingDetector(fail)
    engine.detectors = [detector]
    engine._ready = True
    return engine, detector


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("method", ["placeholder", "redact", "mask"])
async def test_echoed_plaintext_is_scrubbed_on_cold_and_cached_history(field: str, method: str):
    engine, detector = make_engine(method=method)
    messages = [
        {"role": "tool", "content": TEXT},
        {"role": "assistant", "content": None, **message_field(field, TEXT)},
    ]
    original = copy.deepcopy(messages)
    calls = []
    for _ in range(2):
        out, mapping = await engine.process_request(messages)
        assert FIRST not in json.dumps(out) and SECOND not in json.dumps(out)
        assert mapping.get_entity_type(FIRST) == mapping.get_entity_type(SECOND) == "SECRET"
        if method == "placeholder":
            assert field_value(out[1], field) == out[0]["content"]
            assert engine.restore_document(out, mapping) == original
        else:
            assert mapping.get_all_fakes() == {}
            assert engine.restore_document(out, mapping) == out
        if field == "audio.transcript":
            assert out[1]["audio"]["id"] == "audio-id"
            assert out[1]["audio"]["data"] == "opaque-bytes"
        calls.append(detector.calls)
    assert messages == original
    assert calls[0] == calls[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
async def test_plaintext_placeholder_literals_are_reserved_before_content_scan(field: str):
    engine, _ = make_engine()
    messages = [
        {"role": "user", "content": FIRST},
        {"role": "assistant", **message_field(field, "fixture <SECRET_1>; " + SECOND)},
    ]
    out, mapping = await engine.process_request(messages)
    assert out[0]["content"] == "<SECRET_2>"
    assert field_value(out[1], field) == "fixture <SECRET_1>; <SECRET_3>"
    assert engine.restore_document(out, mapping) == messages


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
async def test_system_passthrough_still_skips_plaintext_and_reserves_its_literals(field: str):
    engine, detector = make_engine(passthrough={"system_messages": True})
    messages = [
        {"role": "system", **message_field(field, FIRST + " <SECRET_1>")},
        {"role": "assistant", **message_field(field, SECOND)},
    ]
    out, mapping = await engine.process_request(messages)
    assert out[0] == messages[0]
    assert field_value(out[1], field) == "<SECRET_2>"
    assert mapping.get_fake(FIRST) is None
    assert detector.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
async def test_tool_passthrough_does_not_skip_plaintext_metadata(field: str):
    engine, _ = make_engine(passthrough={"tool_calls": True})
    calls = [{"function": {"name": "save", "arguments": json.dumps({"key": FIRST})}}]
    messages = [{"role": "assistant", "tool_calls": calls, **message_field(field, SECOND)}]
    out, mapping = await engine.process_request(messages)
    assert out[0]["tool_calls"] == calls
    assert field_value(out[0], field) == "<SECRET_1>"
    assert mapping.get_fake(FIRST) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", [None, "", 7, [], {"opaque": "signed-data"}])
async def test_empty_and_nonstring_metadata_is_preserved_without_detection(field: str, value: Any):
    engine, detector = make_engine(strict=True)
    messages = [{"role": "assistant", **message_field(field, value)}]
    out, mapping = await engine.process_request(messages)
    assert out == messages and mapping.is_empty
    assert detector.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("audio", [None, "opaque-audio", {}, {"id": "audio-id", "data": FIRST}])
async def test_audio_without_plaintext_transcript_remains_opaque(audio: Any):
    engine, detector = make_engine(strict=True)
    messages = [{"role": "assistant", "audio": audio}]
    out, mapping = await engine.process_request(messages)
    assert out == messages and mapping.is_empty
    assert detector.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
async def test_plaintext_metadata_block_gate_runs_on_cache_hits(field: str):
    engine, detector = make_engine(block=True)
    messages = [{"role": "assistant", **message_field(field, TEXT)}]
    for _ in range(2):
        with pytest.raises(PIIBlockedError, match="SECRET") as exc:
            await engine.process_request(messages)
        assert FIRST not in str(exc.value) and SECOND not in str(exc.value)
    assert detector.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
async def test_plaintext_metadata_failure_is_sanitized_and_never_cached(field: str, caplog):
    engine, detector = make_engine(fail=True)
    messages = [{"role": "assistant", **message_field(field, TEXT)}]
    for _ in range(2):
        with pytest.raises(PIIProcessingError, match="PII processing failed") as exc:
            await engine.process_request(messages)
        assert FIRST not in str(exc.value) and SECOND not in str(exc.value)
    assert FIRST not in caplog.text and SECOND not in caplog.text
    assert detector.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("stream", [False, True])
async def test_chat_endpoint_rescrubs_restored_metadata_before_forwarding(field: str, stream: bool):
    app, router = _make_app()
    app.state.pii_engine, _ = make_engine()
    echoed = message_field(field, "<SECRET_1>; <SECRET_2>")
    router.response_message_extra = echoed
    router.stream_deltas = [echoed]
    initial = {"role": "tool", "content": TEXT}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(
            "/v1/chat/completions", json={"model": "m", "messages": [initial]}
        )
        assert first.status_code == 200
        restored = first.json()["choices"][0]["message"]
        restored_text = field_value(restored, field)
        assert FIRST in restored_text and SECOND in restored_text
        second = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "stream": stream,
                "messages": [initial, restored, {"role": "user", "content": "Summarize."}],
            },
        )
    assert second.status_code == 200
    sent = router.received_messages[1]
    assert FIRST not in json.dumps(sent) and SECOND not in json.dumps(sent)
    assert field_value(sent, field) == "<SECRET_1>; <SECRET_2>"
    if stream:
        deltas = [p["choices"][0]["delta"] for p in _sse_payloads(second.text)]
        assert "".join(field_value(delta, field) or "" for delta in deltas) == restored_text
    else:
        assert field_value(second.json()["choices"][0]["message"], field) == restored_text


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("failure", ["block", "detector"])
async def test_chat_endpoint_metadata_failure_never_calls_provider(
    field: str, failure: str, caplog
):
    app, router = _make_app()
    app.state.pii_engine, _ = make_engine(block=failure == "block", fail=failure == "detector")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "assistant", **message_field(field, TEXT)}]},
        )
    assert response.status_code == (400 if failure == "block" else 500)
    assert router.received_messages is None
    assert FIRST not in response.text and SECOND not in response.text
    assert FIRST not in caplog.text and SECOND not in caplog.text


@pytest.mark.asyncio
async def test_chat_endpoint_pii_disabled_keeps_plaintext_passthrough():
    app, router = _make_app()
    app.state.config.pii.enabled = False
    messages = [{"role": "assistant", "reasoning_content": TEXT}]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions", json={"model": "m", "messages": messages}
        )
    assert response.status_code == 200
    assert router.received_messages == messages


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("integration", ["litellm", "openwebui"])
async def test_integrations_receive_scrubbed_core_metadata(
    field: str, integration: str, monkeypatch
):
    engine, _ = make_engine()

    async def engine_for(_languages):
        return engine

    body = {"messages": [{"role": "assistant", **message_field(field, TEXT)}]}
    if integration == "litellm":
        guardrail = _guardrail()
        monkeypatch.setattr(guardrail, "_engine_for", engine_for)
        out = await guardrail.async_pre_call_hook(None, None, body, "completion")
        fakes = out["metadata"]["privaite_map"]
    else:
        filter_ = _load_filter().Filter()
        monkeypatch.setattr(filter_, "_engine_for", engine_for)
        metadata: dict = {}
        out = await filter_.inlet(body, metadata)
        fakes = metadata["privaite_map"]
    assert FIRST not in json.dumps(out["messages"]) and SECOND not in json.dumps(out["messages"])
    fake_for = {original: fake for fake, original in fakes.items()}
    assert field_value(out["messages"][0], field) == (
        f"The password is {fake_for[FIRST]}; the API key is {fake_for[SECOND]}."
    )
