from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from privaite.api.dependencies import (
    get_config,
    get_pii_engine,
    get_provider_router,
    record_pii_counts,
    record_pii_stats,
)
from privaite.config.schema import PrivAiTeConfig
from privaite.pii.engine import PIIBlockedError, UnsupportedContentError
from privaite.providers.router import ProviderRouter
from privaite.utils.errors import openai_error, provider_error_response

logger = logging.getLogger("privaite.api.embeddings")

router = APIRouter(prefix="/v1")


@router.post("/embeddings", response_model=None)
async def embeddings(
    request: Request,
    config: PrivAiTeConfig = Depends(get_config),
    pii_engine: Any = Depends(get_pii_engine),
    provider_router: ProviderRouter = Depends(get_provider_router),
):
    body = await request.json()
    model = body.get("model")
    input_text = body.get("input", "")

    if not model:
        return openai_error("model is required", "invalid_request_error", 400)

    if not provider_router.has_model(model):
        return openai_error(f"Model '{model}' not found", "not_found_error", 404)

    if config.pii.enabled and pii_engine is not None:
        try:
            if isinstance(input_text, str):
                msgs = [{"role": "user", "content": input_text}]
                msgs, mapping = await pii_engine.process_request(msgs)
                input_text = msgs[0]["content"]
                record_pii_stats(request, mapping)
            elif isinstance(input_text, list):
                anonymized = []
                # Merge per-item counts and record ONCE: the tracker also counts
                # requests, and a 10-item batch is one request, not ten.
                batch_counts: dict[str, int] = {}
                for text in input_text:
                    msgs = [{"role": "user", "content": text}]
                    msgs, mapping = await pii_engine.process_request(msgs)
                    anonymized.append(msgs[0]["content"])
                    for etype, count in mapping.entity_type_counts().items():
                        batch_counts[etype] = batch_counts.get(etype, 0) + count
                input_text = anonymized
                record_pii_counts(request, batch_counts)
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

    kwargs = {k: v for k, v in body.items() if k not in ("model", "input")}

    try:
        response = await provider_router.embedding(
            model_alias=model, input_text=input_text, **kwargs
        )
    except Exception as exc:
        logger.exception("Provider error for model %s", model)
        return provider_error_response(exc)

    if hasattr(response, "model_dump"):
        return response.model_dump()
    return dict(response)
