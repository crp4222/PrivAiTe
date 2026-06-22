from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from privaite.api.dependencies import get_config, get_pii_engine, get_provider_router
from privaite.config.schema import PrivAiTeConfig
from privaite.pii.engine import UnsupportedContentError
from privaite.providers.router import ProviderRouter
from privaite.utils.errors import openai_error, provider_error_response

logger = logging.getLogger("privaite.api.chat")

router = APIRouter(prefix="/v1")


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    request: Request,
    config: PrivAiTeConfig = Depends(get_config),
    pii_engine: Any = Depends(get_pii_engine),
    provider_router: ProviderRouter = Depends(get_provider_router),
):
    body = await request.json()
    model = body.get("model")
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    if not model:
        return openai_error("model is required", "invalid_request_error", 400)

    if not provider_router.has_model(model):
        return openai_error(f"Model '{model}' not found", "not_found_error", 404)

    mapping = None

    if config.pii.enabled and pii_engine is not None:
        try:
            messages, mapping = await pii_engine.process_request(messages)
            tracker = getattr(request.app.state, "pii_tracker", None)
            if tracker and mapping and not mapping.is_empty:
                session_id = request.headers.get(
                    "x-session-id",
                    request.headers.get("authorization", "anonymous"),
                )
                counts: dict[str, int] = {}
                for orig in mapping._original_to_fake:
                    t = mapping.get_entity_type(orig)
                    if t:
                        counts[t] = counts.get(t, 0) + 1
                tracker.record(session_id, counts)
        except UnsupportedContentError as exc:
            return openai_error(str(exc), "invalid_request_error", 400)
        except Exception:
            logger.exception("PII processing failed")
            if config.pii.on_error == "block":
                return openai_error(
                    "PII anonymization failed. Request blocked for privacy.",
                    "server_error",
                    500,
                    "pii_error",
                )

    kwargs = {k: v for k, v in body.items() if k not in ("model", "messages", "stream")}

    try:
        if stream:
            litellm_stream = await provider_router.streaming_completion(
                model_alias=model, messages=messages, **kwargs
            )

            from privaite.streaming.handler import StreamingHandler

            deanon_config = config.pii.deanonymization if config.pii.enabled else None
            generator = StreamingHandler.stream_response(
                litellm_stream=litellm_stream,
                mapping=mapping,
                deanonymizer_config=deanon_config,
            )

            return StreamingResponse(
                generator,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        response = await provider_router.completion(
            model_alias=model, messages=messages, **kwargs
        )
    except Exception as exc:
        logger.exception("Provider error for model %s", model)
        return provider_error_response(exc)

    if (
        mapping
        and config.pii.deanonymization.enabled
        and pii_engine is not None
    ):
        response_dict = response.model_dump() if hasattr(response, "model_dump") else dict(response)
        for choice in response_dict.get("choices", []):
            msg = choice.get("message", {})
            content = msg.get("content")
            if content:
                msg["content"] = await pii_engine.process_response(content, mapping)
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                msg["tool_calls"] = await pii_engine.process_response_tool_calls(
                    tool_calls, mapping
                )
            function_call = msg.get("function_call")
            if function_call:
                msg["function_call"] = await pii_engine.process_response_function_call(
                    function_call, mapping
                )
        return response_dict

    if hasattr(response, "model_dump"):
        return response.model_dump()
    return dict(response)
