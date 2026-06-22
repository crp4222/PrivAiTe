import os

import pytest
from httpx import ASGITransport, AsyncClient

from privaite.app import create_app
from privaite.config.schema import (
    AuthConfig,
    LiteLLMParams,
    LoggingConfig,
    PrivAiTeConfig,
    ProviderConfig,
    ServerConfig,
)


def _make_config(auth_enabled: bool) -> PrivAiTeConfig:
    return PrivAiTeConfig(
        server=ServerConfig(host="127.0.0.1", port=8400),
        auth=AuthConfig(enabled=auth_enabled),
        providers=[
            ProviderConfig(
                model_name="test",
                litellm_params=LiteLLMParams(model="openai/test"),
            )
        ],
        pii={"enabled": False},
        logging=LoggingConfig(format="text", level="debug"),
    )


def _inject_state(app):
    from privaite.providers.router import ProviderRouter

    app.state.provider_router = ProviderRouter(
        _make_config(True).providers
    )
    app.state.pii_engine = None
    return app


@pytest.fixture
def app_auth_enabled():
    os.environ["PRIVAITE_API_KEYS"] = "test-key-123,test-key-456"
    app = _inject_state(create_app(_make_config(auth_enabled=True)))
    yield app
    os.environ.pop("PRIVAITE_API_KEYS", None)


@pytest.fixture
def app_auth_disabled():
    return _inject_state(create_app(_make_config(auth_enabled=False)))


@pytest.mark.asyncio
async def test_auth_rejects_no_header(app_auth_enabled):
    async with AsyncClient(
        transport=ASGITransport(app=app_auth_enabled), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/models")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_rejects_wrong_key(app_auth_enabled):
    async with AsyncClient(
        transport=ASGITransport(app=app_auth_enabled), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/v1/models", headers={"Authorization": "Bearer wrong-key"}
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_accepts_valid_key(app_auth_enabled):
    async with AsyncClient(
        transport=ASGITransport(app=app_auth_enabled), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/v1/models", headers={"Authorization": "Bearer test-key-123"}
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_accepts_second_key(app_auth_enabled):
    async with AsyncClient(
        transport=ASGITransport(app=app_auth_enabled), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/v1/models", headers={"Authorization": "Bearer test-key-456"}
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_bypasses_auth(app_auth_enabled):
    async with AsyncClient(
        transport=ASGITransport(app=app_auth_enabled), base_url="http://test"
    ) as client:
        resp = await client.get("/health")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_disabled_allows_all(app_auth_disabled):
    async with AsyncClient(
        transport=ASGITransport(app=app_auth_disabled), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/models")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_enabled_without_keys_denies():
    # Fail closed: auth on + no configured keys must reject, not pass through.
    os.environ.pop("PRIVAITE_API_KEYS", None)
    app = _inject_state(create_app(_make_config(auth_enabled=True)))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/models")
        assert resp.status_code == 401
