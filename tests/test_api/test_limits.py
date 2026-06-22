from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from privaite.app import create_app
from privaite.config.schema import (
    AuthConfig,
    LoggingConfig,
    PIIConfig,
    PrivAiTeConfig,
    ServerConfig,
)
from privaite.providers.router import ProviderRouter


def _app(max_bytes: int):
    config = PrivAiTeConfig(
        server=ServerConfig(host="127.0.0.1", port=8400, max_request_bytes=max_bytes),
        auth=AuthConfig(enabled=False),
        providers=[],
        pii=PIIConfig(enabled=False),
        logging=LoggingConfig(format="text", level="debug"),
    )
    app = create_app(config)
    app.state.pii_engine = None
    app.state.pii_tracker = None
    app.state.provider_router = ProviderRouter([])
    return app


@pytest.mark.asyncio
async def test_oversized_request_is_rejected():
    app = _app(max_bytes=100)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            content="x" * 500,
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_request_within_limit_passes_size_check():
    app = _app(max_bytes=10_000_000)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions", json={"model": "x", "messages": []}
        )
    # The size middleware lets it through; the request fails later for another
    # reason (unknown model), never with 413.
    assert resp.status_code != 413
