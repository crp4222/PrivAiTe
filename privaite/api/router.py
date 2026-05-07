from __future__ import annotations

from fastapi import APIRouter

from privaite.api import chat, completions, embeddings, health, models

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(models.router, tags=["models"])
api_router.include_router(chat.router, tags=["chat"])
api_router.include_router(completions.router, tags=["completions"])
api_router.include_router(embeddings.router, tags=["embeddings"])
