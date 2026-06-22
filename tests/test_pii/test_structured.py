from __future__ import annotations

import json

import pytest

from privaite.config.schema import (
    AnonymizationConfig,
    DeanonymizationConfig,
    DetectorsConfig,
    PassthroughConfig,
    PIIConfig,
    PresidioDetectorConfig,
)
from privaite.pii.detector_base import PIIDetector
from privaite.pii.engine import PIIEngine
from privaite.pii.entity import PIIEntity


class FakeDetector(PIIDetector):
    """Deterministic substring detector, so tests need no spaCy/Presidio models."""

    def __init__(self, terms: dict[str, str]) -> None:
        self.terms = terms

    @property
    def name(self) -> str:
        return "fake"

    async def initialize(self) -> None:  # pragma: no cover - trivial
        pass

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        entities: list[PIIEntity] = []
        for term, entity_type in self.terms.items():
            start = 0
            while True:
                idx = text.find(term, start)
                if idx < 0:
                    break
                entities.append(
                    PIIEntity(
                        entity_type=entity_type,
                        text=term,
                        start=idx,
                        end=idx + len(term),
                        score=0.99,
                        source="fake",
                    )
                )
                start = idx + len(term)
        return entities


def _make_engine(passthrough: PassthroughConfig | None = None) -> PIIEngine:
    config = PIIConfig(
        enabled=True,
        detectors=DetectorsConfig(presidio=PresidioDetectorConfig(enabled=False)),
        anonymization=AnonymizationConfig(method="placeholder"),
        deanonymization=DeanonymizationConfig(enabled=True, fuzzy_matching=False),
        passthrough=passthrough or PassthroughConfig(),
    )
    engine = PIIEngine(config)
    engine.detectors = [
        FakeDetector({"Marie Dupont": "PERSON", "marie@acme.com": "EMAIL_ADDRESS"})
    ]
    engine._ready = True
    return engine


@pytest.mark.asyncio
async def test_multimodal_text_part_anonymized():
    engine = _make_engine()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Je suis Marie Dupont"},
                {"type": "image_url", "image_url": {"url": "http://example.com/a.png"}},
            ],
        }
    ]

    out, mapping = await engine.process_request(messages)
    parts = out[0]["content"]

    assert "Marie Dupont" not in parts[0]["text"]
    assert "<PERSON_1>" in parts[0]["text"]
    # Non-text parts are passed through untouched.
    assert parts[1] == messages[0]["content"][1]
    assert not mapping.is_empty


@pytest.mark.asyncio
async def test_tool_call_arguments_anonymized():
    engine = _make_engine()
    args = json.dumps({"to": "marie@acme.com", "name": "Marie Dupont", "count": 3})
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "send_email", "arguments": args},
                }
            ],
        }
    ]

    out, mapping = await engine.process_request(messages)
    fn = out[0]["tool_calls"][0]["function"]
    parsed = json.loads(fn["arguments"])

    assert fn["name"] == "send_email"  # function name is never touched
    assert parsed["count"] == 3  # non-string values preserved
    assert parsed["name"] == "<PERSON_1>"
    assert "marie@acme.com" not in fn["arguments"]
    assert out[0]["content"] is None  # None content preserved as-is


@pytest.mark.asyncio
async def test_tool_call_roundtrip_restores_original():
    engine = _make_engine()
    original = {"to": "marie@acme.com", "name": "Marie Dupont"}
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "f", "arguments": json.dumps(original)}}
            ],
        }
    ]

    out, mapping = await engine.process_request(messages)
    anon_calls = out[0]["tool_calls"]
    restored = await engine.process_response_tool_calls(anon_calls, mapping)

    assert json.loads(restored[0]["function"]["arguments"]) == original


@pytest.mark.asyncio
async def test_passthrough_tool_calls_flag_skips_anonymization():
    engine = _make_engine(PassthroughConfig(tool_calls=True))
    args = json.dumps({"name": "Marie Dupont"})
    messages = [
        {"role": "assistant", "tool_calls": [{"function": {"arguments": args}}]}
    ]

    out, _ = await engine.process_request(messages)

    assert out[0]["tool_calls"][0]["function"]["arguments"] == args


@pytest.mark.asyncio
async def test_invalid_json_arguments_anonymized_as_text():
    engine = _make_engine()
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"arguments": "send to Marie Dupont now (not json)"}}
            ],
        }
    ]

    out, _ = await engine.process_request(messages)

    assert "Marie Dupont" not in out[0]["tool_calls"][0]["function"]["arguments"]


@pytest.mark.asyncio
async def test_missing_content_key_preserved():
    engine = _make_engine()
    messages = [{"role": "assistant", "tool_calls": [{"function": {"arguments": "{}"}}]}]

    out, _ = await engine.process_request(messages)

    assert "content" not in out[0]


@pytest.mark.asyncio
async def test_plain_string_content_still_works():
    engine = _make_engine()
    messages = [{"role": "user", "content": "email marie@acme.com"}]

    out, mapping = await engine.process_request(messages)

    assert "marie@acme.com" not in out[0]["content"]
    assert "<EMAIL_ADDRESS_1>" in out[0]["content"]
    restored = await engine.process_response(out[0]["content"], mapping)
    assert "marie@acme.com" in restored


@pytest.mark.asyncio
async def test_legacy_function_call_anonymized_and_roundtrip():
    engine = _make_engine()
    args = json.dumps({"name": "Marie Dupont", "to": "marie@acme.com"})
    messages = [{"role": "assistant", "function_call": {"name": "f", "arguments": args}}]

    out, mapping = await engine.process_request(messages)
    fc = out[0]["function_call"]

    assert fc["name"] == "f"  # function name untouched
    assert "Marie Dupont" not in fc["arguments"]
    assert "marie@acme.com" not in fc["arguments"]

    restored = await engine.process_response_function_call(fc, mapping)
    assert json.loads(restored["arguments"]) == json.loads(args)


@pytest.mark.asyncio
async def test_legacy_function_call_passthrough_flag():
    engine = _make_engine(PassthroughConfig(tool_calls=True))
    args = json.dumps({"name": "Marie Dupont"})
    messages = [{"role": "assistant", "function_call": {"name": "f", "arguments": args}}]

    out, _ = await engine.process_request(messages)

    assert out[0]["function_call"]["arguments"] == args
