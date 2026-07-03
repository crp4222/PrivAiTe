"""Shared request pipeline for the OpenAI-compatible endpoints.

Chat, completions and embeddings all run the same policy-sensitive plumbing:
validate the model, anonymize with the fail-closed error policy, forward to the
provider, serialize the response. That logic lives here ONCE so a security fix
cannot land in one endpoint and miss the others (it happened: the bare-string
content scan had to be fixed in more than one handler). Endpoint-specific
transforms (which field is anonymized, how a response is restored) stay in the
endpoint modules.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar

from fastapi.responses import JSONResponse, StreamingResponse

from privaite.config.schema import PrivAiTeConfig
from privaite.pii.engine import PIIBlockedError, UnsupportedContentError
from privaite.providers.router import ProviderRouter
from privaite.utils.errors import openai_error, provider_error_response

T = TypeVar("T")

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def validate_model(body: dict, provider_router: ProviderRouter) -> JSONResponse | None:
    """400 when model is missing, 404 when unknown, None when valid."""
    model = body.get("model")
    if not model:
        return openai_error("model is required", "invalid_request_error", 400)
    if not provider_router.has_model(model):
        return openai_error(f"Model '{model}' not found", "not_found_error", 404)
    return None


async def anonymize_or_error(
    anonymize: Callable[[], Awaitable[T]],
    config: PrivAiTeConfig,
    log: logging.Logger,
) -> tuple[T | None, JSONResponse | None]:
    """Run an endpoint's anonymization step under the fail-closed error policy.

    Returns (result, None) on success and (None, error_response) when the request
    must be rejected. The third state, (None, None), is the explicit
    ``on_error: "allow"`` opt-out: the caller keeps its ORIGINAL (raw) payload.
    """
    try:
        return await anonymize(), None
    except UnsupportedContentError as exc:
        return None, openai_error(str(exc), "invalid_request_error", 400)
    except PIIBlockedError as exc:
        # Policy gate: a blocked PII type was found -> reject hard, forward
        # nothing. Independent of on_error. The message names TYPES, not values.
        return None, openai_error(str(exc), "invalid_request_error", 400, "pii_blocked")
    except Exception:
        log.exception("PII processing failed")
        if config.pii.on_error != "allow":  # fail closed unless explicit opt-out
            return None, openai_error(
                "PII anonymization failed. Request blocked for privacy.",
                "server_error", 500, "pii_error",
            )
        return None, None


async def call_provider(
    call: Callable[[], Awaitable[T]],
    model: str,
    log: logging.Logger,
) -> tuple[T | None, JSONResponse | None]:
    """Await a provider call, mapping any failure to the OpenAI error shape."""
    try:
        return await call(), None
    except Exception as exc:
        log.exception("Provider error for model %s", model)
        return None, provider_error_response(exc)


def sse_response(generator: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        generator, media_type="text/event-stream", headers=dict(SSE_HEADERS)
    )


def dump_response(response: Any) -> dict:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return dict(response)
