from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from privaite.api.dependencies import get_provider_router
from privaite.providers.router import ProviderRouter

router = APIRouter(prefix="/v1")


@router.get("/models")
async def list_models(
    provider_router: ProviderRouter = Depends(get_provider_router),
) -> dict:
    models = []
    for model_name in provider_router.models:
        models.append(
            {
                "id": model_name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "privaite",
            }
        )
    return {"object": "list", "data": models}
