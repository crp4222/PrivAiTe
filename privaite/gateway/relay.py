"""Shared httpx passthrough for the gateway.

One long-lived AsyncClient (created in the app lifespan) relays the scrubbed
body with the client's own allowlisted headers, and maps transport failures to
the OpenAI error shape. Log lines here carry method, path and status only:
never a body, a header or an entity value.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable, Mapping

import httpx
from fastapi.responses import JSONResponse

from privaite.utils.errors import openai_error

logger = logging.getLogger("privaite.gateway.relay")

# Agent turns can run many minutes: no read timeout, only connection setup and
# writes are bounded.
UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0)

# Never forwarded upstream, even if an operator lists them: httpx derives them
# from the connection and payload it actually sends.
_NEVER_FORWARD = frozenset(
    {"host", "content-length", "content-encoding", "transfer-encoding", "connection"}
)

# Stripped from the relayed response: the restored body has a different length
# and httpx already decoded any content/transfer encoding.
_STRIP_RESPONSE_HEADERS = frozenset(
    {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"}
)


def create_gateway_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT, follow_redirects=False)


def forward_request_headers(
    incoming: Mapping[str, str], allowlist: Iterable[str] | None
) -> dict[str, str]:
    """Relay the client's request headers verbatim to the upstream.

    A transparent gateway must be invisible: the client already chose which
    provider to trust and which headers to send it, and some providers select
    their request schema from a header (a dropped one makes the backend reject
    an otherwise valid body). So by default every incoming header is forwarded
    except the ones httpx must derive from the connection it opens
    (_NEVER_FORWARD). An explicit allowlist restricts the set for operators who
    want it; _NEVER_FORWARD is still stripped in that case.
    """
    if allowlist is None:
        return {k: v for k, v in incoming.items() if k.lower() not in _NEVER_FORWARD}
    headers: dict[str, str] = {}
    for name in allowlist:
        lowered = name.lower()
        if lowered in _NEVER_FORWARD:
            continue
        value = incoming.get(lowered)
        if value is not None:
            headers[lowered] = value
    return headers


def relay_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _STRIP_RESPONSE_HEADERS}


async def send_upstream(
    client: httpx.AsyncClient,
    url: str,
    query: str,
    headers: dict[str, str],
    content: bytes,
) -> httpx.Response | JSONResponse:
    """POST the body upstream, always in streaming mode: the caller decides from
    the response content type whether to buffer+restore or stream through.
    Transport failures map to the OpenAI error shape."""
    request = client.build_request(
        "POST", url, params=query or None, headers=headers, content=content
    )
    try:
        return await client.send(request, stream=True)
    except httpx.TimeoutException:
        logger.error("gateway upstream timed out: POST %s", url)
        return openai_error("Provider request timed out.", "timeout_error", 504, "timeout")
    except httpx.HTTPError:
        logger.error("gateway upstream request failed: POST %s", url)
        return openai_error(
            "An error occurred with the provider.", "server_error", 502, "provider_error"
        )


async def passthrough_stream(upstream: httpx.Response) -> AsyncIterator[bytes]:
    """Relay the upstream body without buffering it whole."""
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk
    finally:
        await upstream.aclose()
