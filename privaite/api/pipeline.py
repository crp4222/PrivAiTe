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


# The request fields each endpoint forwards to the provider: the OpenAI
# parameters, minus the ones the endpoint handles itself (model, the input,
# stream). The rest of the body is refused. Every forwarded field becomes a
# litellm keyword argument, and litellm also reads call settings from those: a
# client-sent `api_base` sent the operator's API key to the client's server, and
# `extra_body.messages` replaced the anonymized messages on the wire. An
# operator adds provider-specific fields with providers[].forward_params.
CHAT_PARAMS = frozenset(
    {
        "audio",
        "frequency_penalty",
        "function_call",
        "functions",
        "logit_bias",
        "logprobs",
        "max_completion_tokens",
        "max_tokens",
        "metadata",
        "modalities",
        "n",
        "parallel_tool_calls",
        "prediction",
        "presence_penalty",
        "prompt_cache_key",
        "prompt_cache_retention",
        "reasoning_effort",
        "response_format",
        "safety_identifier",
        "seed",
        "service_tier",
        "stop",
        "store",
        "stream_options",
        "temperature",
        "thinking",
        "tool_choice",
        "tools",
        "top_logprobs",
        "top_p",
        "user",
        "verbosity",
        "web_search_options",
    }
)
COMPLETION_PARAMS = frozenset(
    {
        "best_of",
        "echo",
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "max_tokens",
        "n",
        "presence_penalty",
        "seed",
        "stop",
        "stream_options",
        "suffix",
        "temperature",
        "top_p",
        "user",
    }
)
EMBEDDING_PARAMS = frozenset({"dimensions", "encoding_format", "user"})


def forwarded_params(
    body: dict,
    handled: tuple[str, ...],
    allowed: frozenset[str],
    config: PrivAiTeConfig,
    model: str,
) -> tuple[dict, JSONResponse | None]:
    """The body fields this endpoint forwards to the provider, or a 400.

    `handled` are the fields the endpoint reads itself (model, the input,
    stream). Any other field outside `allowed` and the provider's
    forward_params is refused, unless it is null: some clients send every field
    they know, and litellm treated a None keyword as unset, so a null is dropped
    rather than refused. Checked before anything is scanned, so a refused
    request touches nothing. The message names the fields (the caller's own
    keys, never a value) and is not logged.
    """
    extra: set[str] = set()
    for provider in config.providers:
        if provider.model_name == model:
            extra.update(provider.forward_params)
    kwargs: dict = {}
    unknown: list[str] = []
    for key, value in body.items():
        if key in handled:
            continue
        if key in allowed or key in extra:
            kwargs[key] = value
        elif value is not None:
            unknown.append(key)
    if not unknown:
        return kwargs, None
    return kwargs, openai_error(
        f"Unsupported parameter(s): {', '.join(sorted(unknown))}. PrivAiTe forwards the "
        "OpenAI parameters only; the operator can allow more per provider with "
        "providers[].forward_params.",
        "invalid_request_error",
        400,
        "unsupported_parameter",
    )


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
        # Detector/anonymizer exceptions can contain the source text. Do not
        # attach a traceback to a log record on a privacy-sensitive path.
        log.error("PII processing failed")
        if config.pii.on_error != "allow":  # fail closed unless explicit opt-out
            return None, openai_error(
                "PII anonymization failed. Request blocked for privacy.",
                "server_error",
                500,
                "pii_error",
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
        # A provider may echo request fields in an exception. It receives
        # anonymized text in the normal path, but logging it is still unsafe.
        log.error("Provider request failed")
        return None, provider_error_response(exc)


async def resolve_input(
    pii_enabled: bool,
    raw: T,
    anonymize: Callable[[], Awaitable[tuple[T, Any]]],
    config: PrivAiTeConfig,
    log: logging.Logger,
) -> tuple[T, Any, JSONResponse | None]:
    """Anonymize an endpoint's input under the fail-closed policy, returning
    (input_to_forward, mapping, error). The input is the ``raw`` payload unchanged
    when PII is off, when ``on_error: allow`` opts out, or on error (unused then);
    otherwise it is the anonymized payload with its reversible mapping.
    """
    if not pii_enabled:
        return raw, None, None
    result, error = await anonymize_or_error(anonymize, config, log)
    if error is not None:
        return raw, None, error
    if result is None:
        return raw, None, None
    payload, mapping = result
    return payload, mapping, None


def sse_response(generator: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(generator, media_type="text/event-stream", headers=dict(SSE_HEADERS))


async def stream_or_error(
    provider_call: Callable[[], Awaitable[Any]],
    make_stream: Callable[[Any], AsyncIterator[str]],
    model: str,
    log: logging.Logger,
) -> StreamingResponse | JSONResponse:
    """Open a provider stream (mapping any failure to the OpenAI error shape) and
    wrap the restored generator as an SSE response."""
    litellm_stream, error = await call_provider(provider_call, model, log)
    if error is not None:
        return error
    return sse_response(make_stream(litellm_stream))


def dump_response(response: Any) -> dict:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return dict(response)
