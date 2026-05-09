from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from privaite.api.router import api_router
from privaite.config.schema import PrivAiTeConfig
from privaite.middleware.auth import AuthMiddleware
from privaite.providers.router import ProviderRouter
from privaite.utils.logging import setup_logging

logger = logging.getLogger("privaite.app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config: PrivAiTeConfig = app.state.config

    app.state.provider_router = ProviderRouter(config.providers)
    logger.info(
        "Provider router ready with %d model(s)", len(config.providers)
    )

    if config.pii.enabled:
        from privaite.pii.engine import PIIEngine
        from privaite.pii.tracker import PIITracker

        engine = PIIEngine(config.pii)
        await engine.initialize()
        app.state.pii_engine = engine
        app.state.pii_tracker = PIITracker()
        logger.info("PII engine initialized")
    else:
        app.state.pii_engine = None
        app.state.pii_tracker = None
        logger.info("PII processing disabled")

    yield

    if app.state.pii_engine is not None:
        await app.state.pii_engine.shutdown()
    logger.info("PrivAiTe shutdown complete")


def create_app(config: PrivAiTeConfig | None = None) -> FastAPI:
    if config is None:
        from privaite.config.loader import load_config
        config = load_config()

    setup_logging(level=config.logging.level, fmt=config.logging.format)

    app = FastAPI(
        title="PrivAiTe",
        description="Privacy-first LLM proxy with transparent PII anonymization",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.config = config

    app.add_middleware(AuthMiddleware)
    app.include_router(api_router)

    return app
