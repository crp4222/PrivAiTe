import pytest
from httpx import ASGITransport, AsyncClient

from privaite.app import create_app
from privaite.config.schema import (
    AuthConfig,
    GatewayConfig,
    LiteLLMParams,
    LoggingConfig,
    PrivAiTeConfig,
    ProviderConfig,
    ServerConfig,
)
from privaite.providers.router import ProviderRouter


def _config(**overrides) -> PrivAiTeConfig:
    base = {
        "server": ServerConfig(host="127.0.0.1", port=8400),
        "auth": AuthConfig(enabled=False),
        "logging": LoggingConfig(format="text", level="debug"),
    }
    base.update(overrides)
    return PrivAiTeConfig(**base)


def _with_providers(app):
    # httpx's ASGITransport does not run the lifespan, so the state the readiness
    # endpoint reads is attached by hand here, exactly as lifespan would.
    app.state.provider_router = ProviderRouter(
        [ProviderConfig(model_name="test-model", litellm_params=LiteLLMParams(model="openai/gpt"))]
    )
    return app


class _StubEngine:
    def __init__(self, ready: bool) -> None:
        self.is_ready = ready


@pytest.fixture
def app():
    return create_app(_config(pii={"enabled": False}))


async def _get_ready(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/ready")


@pytest.mark.asyncio
async def test_health(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_ready_reports_not_ready_without_providers(app):
    # No provider and no gateway: the proxy cannot serve anything, and the
    # readiness status code must say so (a healthcheck reads the code).
    response = await _get_ready(app)
    assert response.status_code == 503
    assert response.json()["ready"] is False


@pytest.mark.asyncio
async def test_ready_true_when_pii_disabled_on_purpose():
    app = _with_providers(create_app(_config(pii={"enabled": False})))
    response = await _get_ready(app)
    assert response.status_code == 200
    data = response.json()
    assert data["ready"] is True
    assert data["pii"] == "disabled"
    assert data["checks"]["pii_engine_ready"] is True


@pytest.mark.asyncio
async def test_ready_false_when_pii_enabled_but_engine_absent():
    # The engine is what scrubs PII: with pii.enabled and no engine attached,
    # nothing would be redacted, so the proxy must not report itself ready.
    app = _with_providers(create_app(_config(pii={"enabled": True, "preset": "light"})))
    response = await _get_ready(app)
    assert response.status_code == 503
    data = response.json()
    assert data["ready"] is False
    assert data["checks"]["pii_engine_ready"] is False


@pytest.mark.asyncio
async def test_ready_distinguishes_pii_disabled_from_engine_missing():
    disabled = _with_providers(create_app(_config(pii={"enabled": False})))
    missing = _with_providers(create_app(_config(pii={"enabled": True, "preset": "light"})))

    disabled_body = (await _get_ready(disabled)).json()
    missing_body = (await _get_ready(missing)).json()

    assert disabled_body["pii"] == "disabled"
    assert missing_body["pii"] == "missing"
    assert disabled_body["ready"] is True
    assert missing_body["ready"] is False


@pytest.mark.asyncio
async def test_ready_true_when_engine_is_ready():
    app = _with_providers(create_app(_config(pii={"enabled": True, "preset": "light"})))
    app.state.pii_engine = _StubEngine(ready=True)
    response = await _get_ready(app)
    assert response.status_code == 200
    data = response.json()
    assert data["ready"] is True
    assert data["pii"] == "ready"


@pytest.mark.asyncio
async def test_ready_false_while_engine_still_initializing():
    app = _with_providers(create_app(_config(pii={"enabled": True, "preset": "light"})))
    app.state.pii_engine = _StubEngine(ready=False)
    response = await _get_ready(app)
    assert response.status_code == 503
    assert response.json()["pii"] == "initializing"


@pytest.mark.asyncio
async def test_ready_true_for_gateway_only_deployment_without_providers():
    # Gateway mode serves the agent CLI routes with 0 provider entries; marking
    # such a container unhealthy would restart a perfectly working proxy.
    app = create_app(_config(pii={"enabled": False}, gateway=GatewayConfig(enabled=True)))
    response = await _get_ready(app)
    assert response.status_code == 200
    data = response.json()
    assert data["ready"] is True
    assert data["checks"]["providers_configured"] is False
    assert data["gateway_enabled"] is True
