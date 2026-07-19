from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from privaite.api.dependencies import (
    get_config,
    get_pii_engine,
    get_provider_router,
    record_pii_stats,
)
from privaite.api.pipeline import (
    call_provider,
    dump_response,
    resolve_input,
    stream_or_error,
    validate_model,
)
from privaite.config.schema import PrivAiTeConfig
from privaite.providers.router import ProviderRouter

logger = logging.getLogger("privaite.api.chat")

router = APIRouter(prefix="/v1")


async def _scrub_forwarded_fields(kwargs: dict, pii_engine: Any, mapping: Any) -> dict:
    """Scrub the text-bearing auxiliary request fields that the kwargs
    passthrough would otherwise forward verbatim: `prediction.content` (OpenAI
    predicted outputs carry the client's current document) and
    `web_search_options.user_location`. Request inputs only, nothing to restore."""
    out = dict(kwargs)
    prediction = out.get("prediction")
    if isinstance(prediction, dict) and "content" in prediction:
        new_prediction = dict(prediction)
        new_prediction["content"] = await pii_engine.process_request_value(
            prediction["content"], mapping
        )
        out["prediction"] = new_prediction
    web_search = out.get("web_search_options")
    if isinstance(web_search, dict) and "user_location" in web_search:
        new_web_search = dict(web_search)
        new_web_search["user_location"] = await pii_engine.process_request_value(
            web_search["user_location"], mapping
        )
        out["web_search_options"] = new_web_search
    return out


async def _restore_message(msg: dict, pii_engine: Any, mapping: Any) -> None:
    """De-anonymize one response message in place: content, the reasoning trace,
    and tool/function call arguments (restore parity with the streaming path)."""
    content = msg.get("content")
    if content:
        msg["content"] = await pii_engine.process_response(content, mapping)
    # Reasoning models echo placeholders in their traces too; the streaming path
    # restores these, keep parity here.
    for field in ("reasoning_content", "reasoning"):
        value = msg.get(field)
        if isinstance(value, str) and value:
            msg[field] = await pii_engine.process_response(value, mapping)
    tool_calls = msg.get("tool_calls")
    if tool_calls:
        msg["tool_calls"] = await pii_engine.process_response_tool_calls(tool_calls, mapping)
    function_call = msg.get("function_call")
    if function_call:
        msg["function_call"] = await pii_engine.process_response_function_call(
            function_call, mapping
        )


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

    error = validate_model(body, provider_router)
    if error is not None:
        return error

    kwargs = {k: v for k, v in body.items() if k not in ("model", "messages", "stream")}

    async def _anonymize() -> tuple[tuple[list, dict], Any]:
        anon, mapping = await pii_engine.process_request(messages)
        anon_kwargs = await _scrub_forwarded_fields(kwargs, pii_engine, mapping)
        record_pii_stats(request, mapping)
        return (anon, anon_kwargs), mapping

    (messages, kwargs), mapping, error = await resolve_input(
        config.pii.enabled and pii_engine is not None,
        (messages, kwargs),
        _anonymize,
        config,
        logger,
    )
    if error is not None:
        return error

    if stream:
        from privaite.streaming.handler import StreamingHandler

        deanon_config = config.pii.deanonymization if config.pii.enabled else None
        return await stream_or_error(
            lambda: provider_router.streaming_completion(
                model_alias=model, messages=messages, **kwargs
            ),
            lambda s: StreamingHandler.stream_response(
                litellm_stream=s, mapping=mapping, deanonymizer_config=deanon_config
            ),
            model,
            logger,
        )

    response, error = await call_provider(
        lambda: provider_router.completion(model_alias=model, messages=messages, **kwargs),
        model,
        logger,
    )
    if error is not None:
        return error

    if mapping and config.pii.deanonymization.enabled and pii_engine is not None:
        response_dict = dump_response(response)
        for choice in response_dict.get("choices", []):
            await _restore_message(choice.get("message", {}), pii_engine, mapping)
        return response_dict

    return dump_response(response)
