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
from privaite.api.pipeline import (
    anonymize_or_error,
    call_provider,
    dump_response,
    validate_model,
)
from privaite.config.schema import PrivAiTeConfig
from privaite.providers.router import ProviderRouter

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

    error = validate_model(body, provider_router)
    if error is not None:
        return error

    if config.pii.enabled and pii_engine is not None:

        async def _anonymize() -> Any:
            if isinstance(input_text, str):
                msgs = [{"role": "user", "content": input_text}]
                msgs, mapping = await pii_engine.process_request(msgs)
                record_pii_stats(request, mapping)
                return msgs[0]["content"]
            if isinstance(input_text, list):
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
                record_pii_counts(request, batch_counts)
                return anonymized
            # Tokenized (integer-array) input carries no scannable text; pass
            # through unchanged (documented limitation).
            return input_text

        result, error = await anonymize_or_error(_anonymize, config, logger)
        if error is not None:
            return error
        if result is not None:
            input_text = result

    kwargs = {k: v for k, v in body.items() if k not in ("model", "input")}

    response, error = await call_provider(
        lambda: provider_router.embedding(model_alias=model, input_text=input_text, **kwargs),
        model, logger,
    )
    if error is not None:
        return error

    return dump_response(response)
