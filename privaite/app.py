from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from privaite.api.router import api_router
from privaite.config.schema import PrivAiTeConfig
from privaite.middleware.auth import AuthMiddleware
from privaite.middleware.limits import RequestSizeLimitMiddleware
from privaite.providers.router import ProviderRouter
from privaite.utils.logging import setup_logging
from privaite.utils.security import get_api_keys

logger = logging.getLogger("privaite.app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config: PrivAiTeConfig = app.state.config

    app.state.provider_router = ProviderRouter(config.providers)
    logger.info("Provider router ready with %d model(s)", len(config.providers))

    if config.gateway.enabled:
        from privaite.gateway.relay import create_gateway_client

        app.state.gateway_client = create_gateway_client()
        logger.info("Gateway mode enabled")

    if config.auth.enabled and not get_api_keys():
        logger.warning(
            "Auth is enabled but PRIVAITE_API_KEYS is empty; all requests will be "
            "rejected with 401 until a key is set (or set auth.enabled=false)."
        )

    if config.pii.enabled:
        from privaite.pii.engine import PIIEngine
        from privaite.pii.tracker import PIITracker

        engine = PIIEngine(config.pii)
        await engine.initialize()
        await engine.warmup()
        app.state.pii_engine = engine
        app.state.pii_tracker = PIITracker()
        logger.info("PII engine initialized")

        # Warn loudly when running a low-recall configuration, so an operator
        # does not silently leak most PII. The light preset is ~62% recall; if
        # presidio.entities is also pinned to a short allowlist it drops to ~35%.
        presidio = config.pii.detectors.presidio
        if config.pii.preset == "light":
            if presidio.entities:
                logger.warning(
                    "PII preset 'light' with a pinned presidio.entities allowlist "
                    "detects only those types (~35% recall on the benchmark). "
                    "Remove the entities pin for full light recall (~62%), or use "
                    "preset 'onnx' for ~84%."
                )
            else:
                logger.warning(
                    "PII preset 'light' is the fast Presidio-only path (~62% recall "
                    "on the benchmark). Use preset 'onnx' for ~84% if recall matters."
                )
    else:
        app.state.pii_engine = None
        app.state.pii_tracker = None
        logger.info("PII processing disabled")

    yield

    gateway_client = getattr(app.state, "gateway_client", None)
    if gateway_client is not None:
        await gateway_client.aclose()
    if app.state.pii_engine is not None:
        await app.state.pii_engine.shutdown()
    logger.info("PrivAiTe shutdown complete")


def create_app(config: PrivAiTeConfig | None = None) -> FastAPI:
    if config is None:
        from privaite.config.loader import load_config

        config = load_config()

    setup_logging(level=config.logging.level, fmt=config.logging.format)

    from privaite import __version__

    app = FastAPI(
        title="PrivAiTe",
        description="Privacy-first LLM proxy with transparent PII anonymization",
        version=__version__,
        lifespan=lifespan,
    )

    app.state.config = config

    # Starlette runs the LAST added middleware first. Auth must be outermost:
    # it only reads headers, so an unauthenticated request is rejected before
    # the size limiter buffers up to max_request_bytes of body for it.
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=config.server.max_request_bytes)
    app.add_middleware(AuthMiddleware)
    app.include_router(api_router)

    from privaite.gateway import build_gateway_router

    gateway_router = build_gateway_router(config)
    if gateway_router is not None:
        app.include_router(gateway_router)

    return app
