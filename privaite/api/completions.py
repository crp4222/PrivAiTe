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

logger = logging.getLogger("privaite.api.completions")

router = APIRouter(prefix="/v1")


@router.post("/completions", response_model=None)
async def completions(
    request: Request,
    config: PrivAiTeConfig = Depends(get_config),
    pii_engine: Any = Depends(get_pii_engine),
    provider_router: ProviderRouter = Depends(get_provider_router),
):
    body = await request.json()
    model = body.get("model")
    prompt = body.get("prompt", "")
    stream = body.get("stream", False)

    error = validate_model(body, provider_router)
    if error is not None:
        return error

    async def _anonymize() -> tuple[Any, Any]:
        if isinstance(prompt, list) and all(isinstance(p, str) for p in prompt):
            msgs = [{"role": "user", "content": p} for p in prompt]
            msgs, mapping = await pii_engine.process_request(msgs)
            anon_prompt: Any = [m["content"] for m in msgs]
        else:
            msgs = [{"role": "user", "content": prompt}]
            msgs, mapping = await pii_engine.process_request(msgs)
            anon_prompt = msgs[0]["content"]
        record_pii_stats(request, mapping)
        return anon_prompt, mapping

    prompt, mapping, error = await resolve_input(
        config.pii.enabled and pii_engine is not None, prompt, _anonymize, config, logger
    )
    if error is not None:
        return error

    kwargs = {k: v for k, v in body.items() if k not in ("model", "prompt", "stream")}

    if stream:
        from privaite.streaming.handler import StreamingHandler

        deanon_config = config.pii.deanonymization if config.pii.enabled else None
        return await stream_or_error(
            lambda: provider_router.streaming_text_completion(
                model_alias=model, prompt=prompt, **kwargs
            ),
            lambda s: StreamingHandler.stream_text_response(
                litellm_stream=s, mapping=mapping, deanonymizer_config=deanon_config
            ),
            model, logger,
        )

    response, error = await call_provider(
        lambda: provider_router.text_completion(model_alias=model, prompt=prompt, **kwargs),
        model, logger,
    )
    if error is not None:
        return error

    if mapping and config.pii.deanonymization.enabled and pii_engine is not None:
        response_dict = dump_response(response)
        for choice in response_dict.get("choices", []):
            text = choice.get("text")
            if text:
                choice["text"] = await pii_engine.process_response(text, mapping)
        return response_dict

    return dump_response(response)
