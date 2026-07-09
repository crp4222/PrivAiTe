from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from privaite.app import create_app
from privaite.config.schema import (
    AuthConfig,
    LoggingConfig,
    PIIConfig,
    PrivAiTeConfig,
    ServerConfig,
)
from privaite.middleware.limits import RequestSizeLimitMiddleware
from privaite.providers.router import ProviderRouter


def _app(max_bytes: int):
    config = PrivAiTeConfig(
        server=ServerConfig(host="127.0.0.1", port=8400, max_request_bytes=max_bytes),
        auth=AuthConfig(enabled=False),
        providers=[],
        pii=PIIConfig(enabled=False),
        logging=LoggingConfig(format="text", level="debug"),
    )
    app = create_app(config)
    app.state.pii_engine = None
    app.state.pii_tracker = None
    app.state.provider_router = ProviderRouter([])
    return app


@pytest.mark.asyncio
async def test_auth_rejects_before_body_is_buffered(monkeypatch):
    # Auth must be the OUTER middleware: an unauthenticated oversized request
    # gets 401 from the headers alone, never a 413 that proves the size limiter
    # (and its full-body buffering) ran first for an anonymous caller.
    monkeypatch.setenv("PRIVAITE_API_KEYS", "goodkey")
    config = PrivAiTeConfig(
        server=ServerConfig(host="127.0.0.1", port=8400, max_request_bytes=100),
        auth=AuthConfig(enabled=True),
        providers=[],
        pii=PIIConfig(enabled=False),
        logging=LoggingConfig(format="text", level="debug"),
    )
    app = create_app(config)
    app.state.pii_engine = None
    app.state.pii_tracker = None
    app.state.provider_router = ProviderRouter([])

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            content="x" * 500,
            headers={
                "content-type": "application/json",
                "authorization": "Bearer wrongkey",
            },
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_oversized_request_is_rejected():
    app = _app(max_bytes=100)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            content="x" * 500,
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_request_within_limit_passes_size_check():
    app = _app(max_bytes=10_000_000)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions", json={"model": "x", "messages": []}
        )
    # The size middleware lets it through; the request fails later for another
    # reason (unknown model), never with 413.
    assert resp.status_code != 413


@pytest.mark.asyncio
async def test_chunked_body_without_content_length_is_rejected():
    """A body streamed in chunks with no Content-Length still hits the limit."""

    async def downstream(scope, receive, send):  # pragma: no cover - must not run
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = RequestSizeLimitMiddleware(downstream, max_bytes=100)

    chunks = [
        {"type": "http.request", "body": b"x" * 60, "more_body": True},
        {"type": "http.request", "body": b"x" * 60, "more_body": False},
    ]

    async def receive():
        return chunks.pop(0)

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await mw({"type": "http", "headers": []}, receive, send)

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_body_within_limit_is_replayed_intact():
    """A body under the limit reaches the downstream app unchanged."""
    received = bytearray()

    async def downstream(scope, receive, send):
        while True:
            message = await receive()
            received.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = RequestSizeLimitMiddleware(downstream, max_bytes=1000)

    chunks = [
        {"type": "http.request", "body": b"hello ", "more_body": True},
        {"type": "http.request", "body": b"world", "more_body": False},
    ]

    async def receive():
        return chunks.pop(0)

    async def send(message):
        pass

    await mw({"type": "http", "headers": []}, receive, send)

    assert bytes(received) == b"hello world"


@pytest.mark.asyncio
async def test_non_http_scope_passes_through():
    """A non-http scope (websocket, lifespan) bypasses the size check untouched."""
    seen = {}

    async def downstream(scope, receive, send):
        seen["type"] = scope["type"]

    mw = RequestSizeLimitMiddleware(downstream, max_bytes=10)

    await mw({"type": "lifespan"}, None, None)

    assert seen["type"] == "lifespan"


@pytest.mark.asyncio
async def test_non_integer_content_length_is_ignored():
    """A malformed Content-Length header does not crash; the body is still counted."""
    received = bytearray()

    async def downstream(scope, receive, send):
        while True:
            message = await receive()
            received.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break

    mw = RequestSizeLimitMiddleware(downstream, max_bytes=1000)
    chunks = [{"type": "http.request", "body": b"hi", "more_body": False}]

    async def receive():
        return chunks.pop(0)

    async def send(message):
        pass

    await mw(
        {"type": "http", "headers": [(b"content-length", b"not-a-number")]},
        receive,
        send,
    )

    assert bytes(received) == b"hi"


@pytest.mark.asyncio
async def test_control_message_is_replayed_to_app():
    """An http.disconnect arriving before the body is replayed to the downstream app."""
    seen = []

    async def downstream(scope, receive, send):
        message = await receive()
        seen.append(message["type"])

    mw = RequestSizeLimitMiddleware(downstream, max_bytes=1000)
    chunks = [{"type": "http.disconnect"}]

    async def receive():
        return chunks.pop(0)

    async def send(message):
        pass

    await mw({"type": "http", "headers": []}, receive, send)

    assert seen == ["http.disconnect"]
