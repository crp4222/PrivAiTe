"""Only the OpenAI parameters (and an operator's forward_params) reach litellm.

The endpoints used to spread the whole body into litellm's call. litellm reads
call settings from those keyword arguments, so a client-sent `api_base` sent
the operator's API key to the client's server, and `extra_body.messages`
replaced the anonymized messages on the wire. These tests go through the real
ProviderRouter and litellm to local capture servers: nothing leaves the host.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from privaite.app import create_app
from privaite.config.schema import (
    AuthConfig,
    DetectorsConfig,
    LiteLLMParams,
    LoggingConfig,
    PIIConfig,
    PresidioDetectorConfig,
    PrivAiTeConfig,
    ProviderConfig,
    ServerConfig,
)
from privaite.pii.engine import PIIEngine
from privaite.pii.tracker import PIITracker
from privaite.providers.router import ProviderRouter
from privaite.verify import Capture
from tests.test_api.test_chat_endpoint import FakeDetector

EMAIL = "marie@acme.com"


@pytest.fixture(autouse=True)
def _no_default_openai_endpoint(monkeypatch):
    # A provider without api_base falls back to litellm's default OpenAI URL. Point
    # it at a closed local port, so a regression fails here instead of going out.
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_API_BASE", "http://127.0.0.1:9/v1")


@pytest.fixture
def capture():
    server = Capture().start()
    yield server
    server.stop()


@pytest.fixture
def other_server():
    server = Capture().start()
    yield server
    server.stop()


def _provider(name: str, api_base: str | None, forward_params: list[str] | None = None):
    params = {"model": f"openai/{name}", "api_key": "sk-test-not-real"}
    if api_base:
        params["api_base"] = api_base
    return ProviderConfig(
        model_name=name,
        litellm_params=LiteLLMParams(**params),
        forward_params=forward_params or [],
    )


def _make_app(*providers: ProviderConfig):
    config = PrivAiTeConfig(
        server=ServerConfig(host="127.0.0.1", port=8400),
        auth=AuthConfig(enabled=False),
        providers=list(providers),
        pii=PIIConfig(
            enabled=True,
            detectors=DetectorsConfig(presidio=PresidioDetectorConfig(enabled=False)),
        ),
        logging=LoggingConfig(format="text", level="error"),
    )
    app = create_app(config)
    engine = PIIEngine(config.pii)
    engine.detectors = [FakeDetector({EMAIL: "EMAIL_ADDRESS"})]
    engine._ready = True
    app.state.pii_engine = engine
    app.state.pii_tracker = PIITracker()
    app.state.provider_router = ProviderRouter(config.providers)
    return app


async def _post(app, path: str, payload: dict):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, json=payload)


def _assert_refused(resp, *names: str) -> None:
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "unsupported_parameter"
    assert error["type"] == "invalid_request_error"
    for name in names:
        assert name in error["message"]
    assert EMAIL not in error["message"]


@pytest.mark.asyncio
async def test_a_client_api_base_is_refused_and_the_key_never_leaves(other_server):
    # The provider sets no api_base, the case where litellm took the client's.
    app = _make_app(_provider("m", None))
    resp = await _post(
        app,
        "/v1/chat/completions",
        {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "api_base": other_server.base_url,
        },
    )
    _assert_refused(resp, "api_base")
    assert other_server.bodies == []


@pytest.mark.asyncio
async def test_extra_body_cannot_replace_the_anonymized_messages(capture):
    app = _make_app(_provider("m", capture.base_url))
    resp = await _post(
        app,
        "/v1/chat/completions",
        {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "extra_body": {"messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
        },
    )
    _assert_refused(resp, "extra_body")
    assert capture.bodies == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/completions", {"model": "m", "prompt": "hi"}),
        ("/v1/embeddings", {"model": "m", "input": "hi"}),
    ],
)
@pytest.mark.parametrize(
    "field", ["api_base", "base_url", "api_key", "extra_body", "extra_headers"]
)
async def test_every_endpoint_refuses_call_settings(capture, path, payload, field):
    app = _make_app(_provider("m", capture.base_url))
    resp = await _post(
        app, path, {**payload, field: {"input": EMAIL} if field == "extra_body" else "x"}
    )
    _assert_refused(resp, field)
    assert capture.bodies == []


@pytest.mark.asyncio
async def test_an_unknown_field_is_refused_before_anything_is_scanned(capture):
    app = _make_app(_provider("m", capture.base_url))
    scanned = []
    real = app.state.pii_engine.process_request

    async def _spy(*args, **kwargs):
        scanned.append(True)
        return await real(*args, **kwargs)

    app.state.pii_engine.process_request = _spy
    resp = await _post(
        app,
        "/v1/chat/completions",
        {
            "model": "m",
            "messages": [{"role": "user", "content": EMAIL}],
            "top_k": 5,
            "num_retries": 9,
        },
    )
    _assert_refused(resp, "num_retries, top_k")
    assert scanned == []
    assert capture.bodies == []


@pytest.mark.asyncio
async def test_a_null_field_is_dropped_not_refused(capture):
    # Some clients send every field they know, null when unset. litellm read a
    # None keyword as unset, so those requests worked before the allowlist.
    app = _make_app(_provider("m", capture.base_url))
    resp = await _post(
        app,
        "/v1/chat/completions",
        {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "top_k": None,
            "api_base": None,
        },
    )
    assert resp.status_code == 200
    wire = json.loads(capture.bodies[-1])
    assert "top_k" not in wire
    assert "api_base" not in wire


@pytest.mark.asyncio
async def test_openai_parameters_still_reach_the_provider(capture):
    app = _make_app(_provider("m", capture.base_url))
    resp = await _post(
        app,
        "/v1/chat/completions",
        {
            "model": "m",
            "messages": [{"role": "user", "content": f"mail {EMAIL}"}],
            "temperature": 0.25,
            "seed": 7,
        },
    )
    assert resp.status_code == 200
    wire = json.loads(capture.bodies[-1])
    assert (wire["temperature"], wire["seed"]) == (0.25, 7)
    assert wire["messages"][0]["content"] == "mail <EMAIL_ADDRESS_1>"


@pytest.mark.asyncio
async def test_forward_params_adds_a_field_for_that_provider_only(capture):
    app = _make_app(
        _provider("local", capture.base_url, forward_params=["top_k"]),
        _provider("other", capture.base_url),
    )
    resp = await _post(
        app,
        "/v1/chat/completions",
        {"model": "local", "messages": [{"role": "user", "content": "hi"}], "top_k": 5},
    )
    assert resp.status_code == 200
    assert json.loads(capture.bodies[-1])["top_k"] == 5

    sent = len(capture.bodies)
    resp = await _post(
        app,
        "/v1/chat/completions",
        {"model": "other", "messages": [{"role": "user", "content": "hi"}], "top_k": 5},
    )
    _assert_refused(resp, "top_k")
    assert len(capture.bodies) == sent
