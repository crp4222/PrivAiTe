from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from privaite.api.dependencies import get_config, get_pii_engine, get_provider_router
from privaite.config.schema import PrivAiTeConfig
from privaite.pii.engine import PIIBlockedError, UnsupportedContentError
from privaite.providers.router import ProviderRouter
from privaite.utils.errors import openai_error, provider_error_response

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

    if not model:
        return openai_error("model is required", "invalid_request_error", 400)

    if not provider_router.has_model(model):
        return openai_error(f"Model '{model}' not found", "not_found_error", 404)

    mapping = None

    if config.pii.enabled and pii_engine is not None:
        try:
            if isinstance(prompt, list) and all(isinstance(p, str) for p in prompt):
                msgs = [{"role": "user", "content": p} for p in prompt]
                msgs, mapping = await pii_engine.process_request(msgs)
                prompt = [m["content"] for m in msgs]
            else:
                msgs = [{"role": "user", "content": prompt}]
                msgs, mapping = await pii_engine.process_request(msgs)
                prompt = msgs[0]["content"]
        except UnsupportedContentError as exc:
            return openai_error(str(exc), "invalid_request_error", 400)
        except PIIBlockedError as exc:
            # Policy gate: a blocked PII type was found -> reject hard, forward
            # nothing. Independent of on_error. The message names TYPES, not values.
            return openai_error(str(exc), "invalid_request_error", 400, "pii_blocked")
        except Exception:
            logger.exception("PII processing failed")
            if config.pii.on_error != "allow":  # fail closed unless explicit opt-out
                return openai_error(
                    "PII anonymization failed. Request blocked for privacy.",
                    "server_error", 500, "pii_error",
                )

    kwargs = {k: v for k, v in body.items() if k not in ("model", "prompt", "stream")}

    try:
        if stream:
            litellm_stream = await provider_router.streaming_completion(
                model_alias=model,
                messages=[{"role": "user", "content": prompt}],
                **kwargs,
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
            model_alias=model,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
    except Exception as exc:
        logger.exception("Provider error for model %s", model)
        return provider_error_response(exc)

    if mapping and config.pii.deanonymization.enabled and pii_engine is not None:
        response_dict = response.model_dump() if hasattr(response, "model_dump") else dict(response)
        for choice in response_dict.get("choices", []):
            text = choice.get("text")
            if text:
                choice["text"] = await pii_engine.process_response(text, mapping)
        return response_dict

    if hasattr(response, "model_dump"):
        return response.model_dump()
    return dict(response)
