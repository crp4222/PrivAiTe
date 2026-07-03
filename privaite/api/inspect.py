from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from privaite.api.dependencies import get_config, get_pii_engine
from privaite.config.schema import PrivAiTeConfig
from privaite.pii.engine import PIIBlockedError
from privaite.utils.errors import openai_error

# Dry-run inspection: shows what the proxy WOULD redact in a text, so operators
# can verify detection before trusting it. Nothing is forwarded to any provider,
# nothing is logged (this module deliberately has no logger: an exception here
# must not drag message content into a log record), nothing is counted in /stats
# (it is a test probe, not traffic), and no mapping outlives the request.
# The caller already knows the text it submitted, so returning its own
# detections leaks nothing. Off by default (pii.inspect.enabled).

router = APIRouter(prefix="/v1")


@router.post("/pii/inspect", response_model=None)
async def pii_inspect(
    request: Request,
    config: PrivAiTeConfig = Depends(get_config),
    pii_engine: Any = Depends(get_pii_engine),
):
    if not config.pii.inspect.enabled:
        return openai_error(
            "The PII inspect endpoint is disabled. Set pii.inspect.enabled: true "
            "to use it.",
            "permission_error", 403, "inspect_disabled",
        )
    if not config.pii.enabled or pii_engine is None:
        return openai_error(
            "PII processing is disabled; there is nothing to inspect.",
            "invalid_request_error", 400,
        )

    body = await request.json()
    text = body.get("text")
    if not isinstance(text, str) or not text:
        return openai_error(
            "'text' (non-empty string) is required", "invalid_request_error", 400
        )

    entities = await pii_engine.inspect_text(text)

    blocked = set(config.pii.block_entities or [])
    would_block = sorted({e.entity_type for e in entities} & blocked)

    # The anonymized preview runs the REAL request path (same placeholders the
    # provider would see). Skipped when a blocked type is present: in production
    # that request would be rejected, so there is no "what would be sent".
    anonymized = None
    mapping = None
    if not would_block:
        try:
            anon_messages, mapping = await pii_engine.process_request(
                [{"role": "user", "content": text}]
            )
            anonymized = anon_messages[0]["content"]
        except PIIBlockedError as exc:
            # Defensive: the gate fired anyway; report it the dry-run way.
            would_block = list(exc.entity_types)

    return {
        "language": pii_engine._language(),
        "entities": [
            {
                "type": e.entity_type,
                "text": e.text,
                "start": e.start,
                "end": e.end,
                "score": round(e.score, 3),
                "source": e.source,
                "replacement": mapping.get_fake(e.text) if mapping else None,
            }
            for e in entities
        ],
        "anonymized": anonymized,
        "would_block": would_block,
    }
