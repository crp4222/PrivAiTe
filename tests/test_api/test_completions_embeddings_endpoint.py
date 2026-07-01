from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from privaite.app import create_app
from privaite.config.schema import (
    AnonymizationConfig,
    AuthConfig,
    DeanonymizationConfig,
    DetectorsConfig,
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


class FakeStreamChunk:
    def __init__(self, data: dict) -> None:
        self._data = data

    def model_dump(self) -> dict:
        return self._data


class FakeRouter:
    """Mirrors the real ProviderRouter contract: /v1/completions goes through
    text_completion (prompt in, text_completion shape out), never through the
    chat-shaped completion()."""

    def __init__(self) -> None:
        self.prompt = None
        self.embedding_input = None
        self.stream_texts: list[str] | None = None

    def has_model(self, model: str) -> bool:
        return True

    async def text_completion(self, model_alias, prompt, **kwargs):
        self.prompt = prompt
        echoed = prompt if isinstance(prompt, str) else json.dumps(prompt)
        return {
            "object": "text_completion",
            "model": model_alias,
            "choices": [{"index": 0, "text": f"Echo: {echoed}"}],
        }

    async def streaming_text_completion(self, model_alias, prompt, **kwargs):
        self.prompt = prompt

        async def _stream():
            texts = self.stream_texts or []
            for i, text in enumerate(texts):
                finish = "stop" if i == len(texts) - 1 else None
                yield FakeStreamChunk({
                    "object": "text_completion",
                    "model": model_alias,
                    "choices": [{"index": 0, "text": text, "finish_reason": finish}],
                })

        return _stream()

    async def embedding(self, model_alias, input_text, **kwargs):
        self.embedding_input = input_text
        return {
            "object": "list",
            "model": model_alias,
            "data": [{"index": 0, "embedding": [0.0, 0.1]}],
        }


def _make_app(
    block_entities: list[str] | None = None,
) -> tuple[object, FakeRouter]:
    config = PrivAiTeConfig(
        server=ServerConfig(host="127.0.0.1", port=8400),
        auth=AuthConfig(enabled=False),
        providers=[],
        pii=PIIConfig(
            enabled=True,
            detectors=DetectorsConfig(presidio=PresidioDetectorConfig(enabled=False)),
            anonymization=AnonymizationConfig(method="placeholder"),
            deanonymization=DeanonymizationConfig(enabled=True, fuzzy_matching=False),
            block_entities=block_entities or [],
        ),
        logging=LoggingConfig(format="text", level="debug"),
    )
    app = create_app(config)
    engine = PIIEngine(config.pii)
    engine.detectors = [
        FakeDetector({"Marie Dupont": "PERSON", "marie@acme.com": "EMAIL_ADDRESS"})
    ]
    engine._ready = True
    app.state.pii_engine = engine
    app.state.pii_tracker = PIITracker()
    router = FakeRouter()
    app.state.provider_router = router
    return app, router


@pytest.mark.asyncio
async def test_completions_string_prompt_anonymized_and_restored():
    app, router = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/completions",
            json={"model": "m", "prompt": "Contact Marie Dupont at marie@acme.com"},
        )

    assert resp.status_code == 200
    assert isinstance(router.prompt, str)
    assert "Marie Dupont" not in router.prompt
    assert "marie@acme.com" not in router.prompt

    body = resp.json()
    assert body["object"] == "text_completion"
    text = body["choices"][0]["text"]
    assert "Marie Dupont" in text
    assert "marie@acme.com" in text


@pytest.mark.asyncio
async def test_completions_list_prompt_stays_a_list_of_strings():
    # A batch prompt must reach the provider as a list of anonymized strings,
    # not be smuggled into a chat message whose content is a bare list.
    app, router = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/completions",
            json={"model": "m", "prompt": ["I am Marie Dupont", "nothing here"]},
        )

    assert resp.status_code == 200
    assert isinstance(router.prompt, list)
    assert all(isinstance(p, str) for p in router.prompt)
    assert "Marie Dupont" not in json.dumps(router.prompt)
    assert router.prompt[1] == "nothing here"


@pytest.mark.asyncio
async def test_completions_streaming_restores_split_placeholder():
    # The placeholder is split across two text_completion chunks; the trie
    # buffer must reassemble and restore it, and the stream must end with [DONE].
    app, router = _make_app()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Prime the mapping by anonymizing the prompt, then have the "provider"
        # echo the placeholder split across chunks.
        prime = await client.post(
            "/v1/completions",
            json={"model": "m", "prompt": "I am Marie Dupont"},
        )
        assert prime.status_code == 200
        placeholder = router.prompt.replace("I am ", "")
        assert placeholder.startswith("<")

        mid = len(placeholder) // 2
        router.stream_texts = [f"Hello {placeholder[:mid]}", f"{placeholder[mid:]} bye"]

        resp = await client.post(
            "/v1/completions",
            json={"model": "m", "prompt": "I am Marie Dupont", "stream": True},
        )

    assert resp.status_code == 200
    payloads = [
        line[len("data: "):]
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    assert payloads[-1] == "[DONE]"
    text = "".join(
        choice.get("text") or ""
        for payload in payloads[:-1]
        for choice in json.loads(payload).get("choices", [])
    )
    assert "Marie Dupont" in text
    assert placeholder not in text
    # provider chunk fields survive (the handler rewrites text only)
    assert json.loads(payloads[0])["object"] == "text_completion"


@pytest.mark.asyncio
async def test_embeddings_string_input_anonymized():
    app, router = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/embeddings",
            json={"model": "m", "input": "my email is marie@acme.com"},
        )

    assert resp.status_code == 200
    assert "marie@acme.com" not in router.embedding_input


@pytest.mark.asyncio
async def test_block_entities_rejects_on_completions_and_embeddings():
    # the policy gate must hold on ALL entry points, not just chat.
    app, router = _make_app(block_entities=["EMAIL_ADDRESS"])
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        comp = await client.post(
            "/v1/completions",
            json={"model": "m", "prompt": "reach marie@acme.com"},
        )
        emb = await client.post(
            "/v1/embeddings",
            json={"model": "m", "input": "reach marie@acme.com"},
        )

    for resp in (comp, emb):
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "pii_blocked"
        assert "EMAIL_ADDRESS" in body["error"]["message"]
        assert "marie@acme.com" not in body["error"]["message"]
    assert router.prompt is None  # nothing ever reached the provider
    assert router.embedding_input is None


@pytest.mark.asyncio
async def test_completions_and_embeddings_count_in_stats():
    # /stats used to only ever see the chat endpoint.
    app, router = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/v1/completions",
            json={"model": "m", "prompt": "I am Marie Dupont"},
            headers={"x-session-id": "s1"},
        )
        await client.post(
            "/v1/embeddings",
            json={"model": "m", "input": "mail: marie@acme.com"},
            headers={"x-session-id": "s1"},
        )

    stats = app.state.pii_tracker.get("s1")
    assert stats is not None
    assert stats.pii_count["PERSON"] == 1
    assert stats.pii_count["EMAIL_ADDRESS"] == 1
    assert stats.request_count == 2


@pytest.mark.asyncio
async def test_embeddings_list_input_anonymized():
    app, router = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/embeddings",
            json={"model": "m", "input": ["Marie Dupont", "clean"]},
        )

    assert resp.status_code == 200
    assert "Marie Dupont" not in json.dumps(router.embedding_input)
