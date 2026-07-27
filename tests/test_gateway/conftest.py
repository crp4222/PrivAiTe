from __future__ import annotations

import json

import httpx
import pytest

from privaite.app import create_app
from privaite.config.schema import (
    AnonymizationConfig,
    AuthConfig,
    DeanonymizationConfig,
    DetectorsConfig,
    GatewayConfig,
    LoggingConfig,
    PIIConfig,
    PresidioDetectorConfig,
    PrivAiTeConfig,
    ServerConfig,
)
from privaite.pii.detector_base import PIIDetector
from privaite.pii.engine import PIIEngine
from privaite.pii.entity import PIIEntity
from privaite.pii.tracker import PIITracker


class FakeDetector(PIIDetector):
    def __init__(self, terms: dict[str, str]) -> None:
        self.terms = terms

    @property
    def name(self) -> str:
        return "fake"

    async def initialize(self) -> None:
        pass

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        out: list[PIIEntity] = []
        for term, etype in self.terms.items():
            idx = text.find(term)
            if idx >= 0:
                out.append(PIIEntity(etype, term, idx, idx + len(term), 0.99, "fake"))
        return out


class BoomDetector(PIIDetector):
    @property
    def name(self) -> str:
        return "boom"

    async def initialize(self) -> None:
        pass

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        raise RuntimeError("detector exploded on: " + text)


class FakeUpstream:
    """Captures the relayed httpx request and serves a canned response."""

    def __init__(self) -> None:
        self.request: httpx.Request | None = None
        self.status = 200
        self.headers = {"content-type": "application/json"}
        self.body: bytes = b"{}"

    def set_json(self, payload: dict) -> None:
        self.body = json.dumps(payload).encode()
        self.headers = {"content-type": "application/json"}

    def set_sse(self, events: list[tuple[str, dict]]) -> None:
        blocks = [
            f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            for name, data in events
        ]
        self.body = "".join(blocks).encode()
        self.headers = {"content-type": "text/event-stream"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        return httpx.Response(self.status, headers=dict(self.headers), content=self.body)

    def sent_json(self) -> dict:
        assert self.request is not None
        return json.loads(self.request.content)


def make_gateway_app(
    pii_enabled: bool = True,
    auth_enabled: bool = False,
    deanonymize: bool = True,
    gateway_enabled: bool = True,
    detector: PIIDetector | None = None,
    block_entities: list[str] | None = None,
):
    config = PrivAiTeConfig(
        server=ServerConfig(host="127.0.0.1", port=8400),
        auth=AuthConfig(enabled=auth_enabled),
        providers=[],
        pii=PIIConfig(
            enabled=pii_enabled,
            detectors=DetectorsConfig(presidio=PresidioDetectorConfig(enabled=False)),
            anonymization=AnonymizationConfig(method="placeholder"),
            deanonymization=DeanonymizationConfig(enabled=deanonymize),
            block_entities=block_entities or [],
        ),
        gateway=GatewayConfig(enabled=gateway_enabled),
        logging=LoggingConfig(format="text", level="debug"),
    )
    app = create_app(config)

    upstream = FakeUpstream()
    app.state.gateway_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))

    if pii_enabled:
        engine = PIIEngine(config.pii)
        engine.detectors = [
            detector or FakeDetector({"Marie Dupont": "PERSON", "marie@acme.com": "EMAIL_ADDRESS"})
        ]
        engine._ready = True
        app.state.pii_engine = engine
        app.state.pii_tracker = PIITracker()
    else:
        app.state.pii_engine = None
        app.state.pii_tracker = None

    return app, upstream


@pytest.fixture
def gateway_app():
    return make_gateway_app()
