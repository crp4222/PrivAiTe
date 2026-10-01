from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import Response

from privaite.api.dependencies import get_config, get_provider_router

router = APIRouter(prefix="/v1")


@router.get("/models", response_model=None)
async def list_models(request: Request) -> dict | Response:
    # In gateway mode this path is shared with Codex, which wants its upstream
    # catalog. Only a caller the auth middleware verified as holding a PrivAiTe
    # key gets PrivAiTe's own list; with auth disabled nobody can prove that, so
    # the route always relays (documented in docs/gateway.md).
    if get_config(request).gateway.enabled and not getattr(request.state, "privaite_client", False):
        from privaite.gateway.routes import relay_models

        return await relay_models(request)

    # Resolved here rather than as a dependency: the relay branch above has no
    # use for the core router, which a gateway-only app may not even have.
    models = []
    for model_name in get_provider_router(request).models:
        models.append(
            {
                "id": model_name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "privaite",
            }
        )
    return {"object": "list", "data": models}
