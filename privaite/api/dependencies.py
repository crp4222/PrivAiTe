from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

if TYPE_CHECKING:
    from privaite.config.schema import PrivAiTeConfig
    from privaite.pii.engine import PIIEngine
    from privaite.providers.router import ProviderRouter


def get_config(request: Request) -> PrivAiTeConfig:
    return request.app.state.config


def get_pii_engine(request: Request) -> PIIEngine | None:
    return getattr(request.app.state, "pii_engine", None)


def get_provider_router(request: Request) -> ProviderRouter:
    return request.app.state.provider_router
