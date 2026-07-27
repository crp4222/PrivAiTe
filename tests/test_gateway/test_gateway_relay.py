"""Relay-level behaviour: content negotiation, header fidelity, transport errors.

The gateway has to READ the provider's body to restore it, so it may only ever
negotiate a content-encoding it can decode, and it must hand the client's own
headers to the upstream exactly as they arrived (duplicates included).
"""

from __future__ import annotations

import gzip
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from starlette.datastructures import Headers

from privaite.gateway.relay import create_gateway_client, forward_request_headers
from tests.test_gateway.conftest import make_gateway_app

_ANTHROPIC_REQUEST = {
    "model": "claude-test",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "Contact Marie Dupont"}],
}

_ANTHROPIC_REPLY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "I contacted <PERSON_1>."}],
}


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _headers(pairs: list[tuple[str, str]]) -> Headers:
    """A starlette Headers holding one entry per wire header, duplicates kept."""
    return Headers(raw=[(name.encode(), value.encode()) for name, value in pairs])


class EncodingAwareUpstream:
    """A provider that answers in the first encoding the caller advertised.

    Real providers do exactly this, so what leaves the proxy as accept-encoding
    decides what comes back compressed.
    """

    # A genuine brotli payload needs the optional brotli package, which httpx
    # also needs to decode one. These bytes stand in for it: the point pinned
    # here is that the proxy must never ask for an encoding it cannot decode,
    # whichever optional decoders happen to be installed.
    BROTLI_BODY = b"\x1b\x0e\x00\xf8not-brotli-decodable"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.request: httpx.Request | None = None
        self.encoding_used: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        raw = json.dumps(self.payload).encode()
        offered = [
            token.split(";")[0].strip().lower()
            for token in request.headers.get("accept-encoding", "").split(",")
        ]
        for token in offered:
            if token == "br":
                self.encoding_used = "br"
                return httpx.Response(
                    200,
                    headers={"content-type": "application/json", "content-encoding": "br"},
                    content=self.BROTLI_BODY,
                )
            if token == "gzip":
                self.encoding_used = "gzip"
                return httpx.Response(
                    200,
                    headers={"content-type": "application/json", "content-encoding": "gzip"},
                    content=gzip.compress(raw),
                )
        self.encoding_used = "identity"
        return httpx.Response(200, headers={"content-type": "application/json"}, content=raw)


class RaisingUpstream:
    """Transport that never answers: it raises the given httpx error."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.called = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.called = True
        raise self.exc


def _with_transport(app, handler) -> None:
    app.state.gateway_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_brotli_capable_upstream_still_yields_a_parseable_restored_body():
    # The client asks for brotli first. If that reached the provider, the body
    # would come back compressed, the restore pass would be skipped and the
    # client would get unreadable bytes with no content-encoding to explain it.
    app, _ = make_gateway_app()
    upstream = EncodingAwareUpstream(_ANTHROPIC_REPLY)
    _with_transport(app, upstream.handler)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/messages",
            json=_ANTHROPIC_REQUEST,
            headers={"accept-encoding": "br, gzip, deflate, zstd"},
        )

    assert upstream.encoding_used != "br"
    assert resp.status_code == 200
    assert resp.json()["content"][0]["text"] == "I contacted Marie Dupont."
    # The proxy decoded the body, so it must not claim an encoding downstream.
    assert "content-encoding" not in resp.headers


@pytest.mark.asyncio
async def test_client_accept_encoding_never_reaches_the_upstream():
    app, _ = make_gateway_app()
    upstream = EncodingAwareUpstream(_ANTHROPIC_REPLY)
    _with_transport(app, upstream.handler)

    async with _client(app) as client:
        resp = await client.post(
            "/v1/messages",
            json=_ANTHROPIC_REQUEST,
            headers={"accept-encoding": "br;q=1.0, x-made-up"},
        )

    assert resp.status_code == 200
    assert upstream.request is not None
    # httpx only advertises the encodings it can actually decode: relaying its
    # own header is what keeps the body readable.
    async with create_gateway_client() as probe:
        expected = probe.headers["accept-encoding"]
    assert upstream.request.headers["accept-encoding"] == expected
    assert "x-made-up" not in upstream.request.headers["accept-encoding"]


@pytest.mark.asyncio
async def test_duplicate_header_names_are_relayed_twice():
    app, upstream = make_gateway_app()
    async with _client(app) as client:
        resp = await client.post(
            "/v1/messages",
            json=_ANTHROPIC_REQUEST,
            headers=[
                ("anthropic-beta", "oauth-2025-04-20"),
                ("anthropic-beta", "fine-grained-tool-streaming-2025-05-14"),
                ("content-type", "application/json"),
            ],
        )

    assert resp.status_code == 200
    assert upstream.request is not None
    assert upstream.request.headers.get_list("anthropic-beta") == [
        "oauth-2025-04-20",
        "fine-grained-tool-streaming-2025-05-14",
    ]


@pytest.mark.asyncio
async def test_upstream_timeout_maps_to_504():
    app, _ = make_gateway_app()
    upstream = RaisingUpstream(httpx.ConnectTimeout("connect timed out"))
    _with_transport(app, upstream.handler)

    async with _client(app) as client:
        resp = await client.post("/v1/messages", json=_ANTHROPIC_REQUEST)

    assert upstream.called
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "timeout"
    assert resp.json()["error"]["type"] == "timeout_error"


@pytest.mark.asyncio
async def test_upstream_transport_error_maps_to_502():
    app, _ = make_gateway_app()
    upstream = RaisingUpstream(httpx.ConnectError("connection refused"))
    _with_transport(app, upstream.handler)

    async with _client(app) as client:
        resp = await client.post("/v1/responses", json={"model": "m", "input": "Marie Dupont"})

    assert upstream.called
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "provider_error"
    assert resp.json()["error"]["type"] == "server_error"


def test_transparent_branch_keeps_duplicates_and_strips_never_forwarded():
    relayed = forward_request_headers(
        _headers(
            [
                ("host", "127.0.0.1:8400"),
                ("content-length", "42"),
                ("accept-encoding", "br, gzip"),
                ("transfer-encoding", "chunked"),
                ("connection", "keep-alive"),
                ("content-encoding", "gzip"),
                ("authorization", "Bearer client-token"),
                ("anthropic-beta", "oauth-2025-04-20"),
                ("anthropic-beta", "context-1m-2025-08-07"),
                ("content-type", "application/json"),
            ]
        ),
        None,
    )

    assert relayed == [
        ("authorization", "Bearer client-token"),
        ("anthropic-beta", "oauth-2025-04-20"),
        ("anthropic-beta", "context-1m-2025-08-07"),
        ("content-type", "application/json"),
    ]


def test_allowlist_branch_keeps_allowed_duplicates_and_protocol_headers():
    relayed = forward_request_headers(
        _headers(
            [
                ("host", "127.0.0.1:8400"),
                ("accept-encoding", "br"),
                ("authorization", "Bearer client-token"),
                ("anthropic-beta", "oauth-2025-04-20"),
                ("anthropic-beta", "context-1m-2025-08-07"),
                ("x-internal-tracing", "trace-1"),
                ("cookie", "session=1"),
                ("content-type", "application/json"),
                ("anthropic-version", "2023-06-01"),
            ]
        ),
        ["Authorization", "anthropic-beta"],
    )

    names = [name for name, _ in relayed]
    # Allowlisted, case-insensitively, with both values of a repeated name.
    assert relayed.count(("anthropic-beta", "oauth-2025-04-20")) == 1
    assert relayed.count(("anthropic-beta", "context-1m-2025-08-07")) == 1
    assert ("authorization", "Bearer client-token") in relayed
    # Not allowlisted: dropped.
    assert "x-internal-tracing" not in names
    assert "cookie" not in names
    # Never forwarded, even though one of them is what the client asked for.
    assert "host" not in names
    assert "accept-encoding" not in names
    # The protocols do not work without these: an allowlist cannot drop them.
    assert ("content-type", "application/json") in relayed
    assert ("anthropic-version", "2023-06-01") in relayed


def test_allowlist_cannot_re_enable_a_never_forwarded_header():
    relayed = forward_request_headers(
        _headers([("accept-encoding", "br"), ("host", "127.0.0.1:8400"), ("accept", "*/*")]),
        ["accept-encoding", "host", "accept"],
    )

    assert relayed == [("accept", "*/*")]
