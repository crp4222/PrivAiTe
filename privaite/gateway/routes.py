"""Gateway endpoints: scrub -> relay -> restore.

Opt-in routes that let agent CLIs (Claude Code, Codex) point their base URL at
PrivAiTe. The client's own provider token is relayed verbatim upstream; the
request is scrubbed under the same fail-closed policy as the OpenAI-compatible
endpoints, and the response (streaming or not) is restored before it reaches
the client. With PII off, or nothing detected, the relay is a pure passthrough
that streams without buffering.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from privaite.api.dependencies import get_config, get_pii_engine, record_pii_stats
from privaite.api.pipeline import SSE_HEADERS, resolve_input
from privaite.config.schema import GatewayUpstreamConfig, PrivAiTeConfig
from privaite.gateway.protocols import ANTHROPIC_MESSAGES, OPENAI_RESPONSES, ProtocolSpec
from privaite.gateway.relay import (
    forward_request_headers,
    passthrough_stream,
    relay_response_headers,
    send_upstream,
)
from privaite.gateway.restore import restore_sse_stream, restore_tree
from privaite.gateway.scrub import scrub_anthropic_request, scrub_responses_request
from privaite.pii.engine import PIIEngine
from privaite.pii.mapping import PIIMapping
from privaite.utils.errors import openai_error

logger = logging.getLogger("privaite.gateway")

ScrubFn = Callable[[PIIEngine, dict[str, Any]], Awaitable[tuple[dict[str, Any], PIIMapping]]]


def build_gateway_router(config: PrivAiTeConfig) -> APIRouter | None:
    """The gateway routes, or None when gateway mode is disabled (the app is
    then byte-for-byte identical to a gateway-less build)."""
    if not config.gateway.enabled:
        return None
    router = APIRouter(tags=["gateway"])
    wiring: tuple[tuple[ProtocolSpec, GatewayUpstreamConfig, ScrubFn], ...] = (
        (ANTHROPIC_MESSAGES, config.gateway.anthropic, scrub_anthropic_request),
        (OPENAI_RESPONSES, config.gateway.openai_responses, scrub_responses_request),
    )
    for spec, upstream, scrub in wiring:
        for local_path, upstream_path in spec.routes:
            router.add_api_route(
                local_path,
                _make_handler(spec, upstream, upstream_path, scrub),
                methods=["POST"],
                response_model=None,
            )
    return router


def _make_handler(
    spec: ProtocolSpec, upstream: GatewayUpstreamConfig, upstream_path: str, scrub: ScrubFn
) -> Callable[[Request], Awaitable[Response]]:
    async def handler(request: Request) -> Response:
        return await _gateway_call(request, spec, upstream, upstream_path, scrub)

    handler.__name__ = f"gateway_{spec.name}{upstream_path.replace('/', '_')}"
    return handler


async def _gateway_call(
    request: Request,
    spec: ProtocolSpec,
    upstream: GatewayUpstreamConfig,
    upstream_path: str,
    scrub: ScrubFn,
) -> Response:
    config = get_config(request)
    engine = get_pii_engine(request)
    raw = await request.body()
    mapping: PIIMapping | None = None
    content = raw
    wants_stream = False

    if config.pii.enabled and engine is not None:
        try:
            body = json.loads(raw)
        except ValueError:
            return openai_error("Request body must be valid JSON.", "invalid_request_error", 400)
        if not isinstance(body, dict):
            return openai_error("Request body must be a JSON object.", "invalid_request_error", 400)
        wants_stream = bool(body.get("stream"))

        async def _anonymize() -> tuple[dict[str, Any], PIIMapping]:
            scrubbed, new_mapping = await scrub(engine, body)
            record_pii_stats(request, new_mapping)
            return scrubbed, new_mapping

        # Fail closed: on a scrub error the request is rejected here and nothing
        # is forwarded (unless pii.on_error is the explicit "allow" opt-out, in
        # which case resolve_input hands back the raw body).
        payload, mapping, error = await resolve_input(True, body, _anonymize, config, logger)
        if error is not None:
            return error
        content = json.dumps(payload, ensure_ascii=False).encode()

    url = upstream.base_url.rstrip("/") + upstream_path
    headers = forward_request_headers(request.headers, upstream.forward_headers)
    upstream_resp = await send_upstream(
        request.app.state.gateway_client, url, request.url.query, headers, content
    )
    if isinstance(upstream_resp, JSONResponse):
        return upstream_resp

    do_restore = (
        engine is not None
        and mapping is not None
        and not mapping.is_empty
        and config.pii.deanonymization.enabled
    )
    resp_headers = relay_response_headers(upstream_resp.headers)
    logger.info(
        "gateway %s -> %d (%s)",
        request.url.path,
        upstream_resp.status_code,
        "restore" if do_restore else "passthrough",
    )

    if not do_restore or engine is None or mapping is None:
        return StreamingResponse(
            passthrough_stream(upstream_resp),
            status_code=upstream_resp.status_code,
            headers=resp_headers,
        )

    # Trust the client's own `stream: true` over the response content-type: some
    # provider backends (the ChatGPT Codex backend) send an SSE body with no
    # text/event-stream content-type, and routing that into the JSON path would
    # relay it unrestored. Errors come back as JSON, so a non-200 always takes
    # the JSON path regardless of what was requested.
    content_type = upstream_resp.headers.get("content-type", "")
    is_stream = "text/event-stream" in content_type or (
        wants_stream and upstream_resp.status_code == 200
    )

    if is_stream:
        return StreamingResponse(
            restore_sse_stream(upstream_resp, engine, mapping, spec),
            status_code=upstream_resp.status_code,
            headers={**resp_headers, **SSE_HEADERS},
        )

    data = await upstream_resp.aread()
    await upstream_resp.aclose()
    try:
        parsed = json.loads(data)
    except ValueError:
        # Non-JSON body: placeholders cannot appear outside JSON text payloads
        # from these APIs, relay it unchanged.
        return Response(content=data, status_code=upstream_resp.status_code, headers=resp_headers)
    try:
        restored = restore_tree(engine, parsed, mapping, spec.skip_restore_types)
    except Exception:
        # Withhold the response rather than relay a half-restored body, and keep
        # the documented error shape: an unexpected restore failure used to reach
        # the client as a bare 500 with a traceback on stdout. The SSE path
        # already funnels everything through PIIProcessingError this way.
        logger.error("gateway restore failed; response withheld")
        return openai_error(
            "PII restore failed. Response withheld for privacy.", "server_error", 500, "pii_error"
        )
    return Response(
        content=json.dumps(restored, ensure_ascii=False).encode(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
    )
