"""Synthetic credentials only; tests do not load model weights or call a provider."""

from __future__ import annotations

import json

import pytest

from privaite.config.schema import PIIConfig
from privaite.gateway.scrub import scrub_anthropic_request, scrub_responses_request
from privaite.pii.engine import PIIBlockedError, PIIEngine
from privaite.pii.entity import PIIEntity, merge_entities
from privaite.pii.recognizer_secret import StructuredSecretRecognizer


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ('export OPENAI_API_KEY="synthetic-only-123"', "synthetic-only-123"),
        ("presented_key=fixture-only smtp_secret='other fake'", "fixture-only"),
        ("presented_key=fixture-only smtp_secret='other fake'", "other fake"),
        ('{"apiKey": "fake\\"quoted\\\\value"}', 'fake\\"quoted\\\\value'),
        ("'client_secret': 'fake; space, and punctuation!'", "fake; space, and punctuation!"),
        ("DB_PASSWORD: 'demo-only'", "demo-only"),
        ("password=demo-only, status=ok", "demo-only,"),
        ("password=demo,only;{with}[punctuation]=!", "demo,only;{with}[punctuation]=!"),
        ("postgresql://service:fake%40password!@db.example.invalid:5432/app", "fake%40password!"),
        ("Authorization: Bearer synthetic.token-only==", "synthetic.token-only=="),
        ("Authorization: Bearer " + "a" * 4096, "a" * 4096),
    ],
)
def test_reports_only_the_value(text: str, value: str):
    recognizer = StructuredSecretRecognizer()
    spans = recognizer.analyze(text, ["SECRET"])
    assert any(text[s.start : s.end] == value for s in spans)
    for span in spans:
        assert span.entity_type == "SECRET"
        assert span.end > span.start


@pytest.mark.parametrize(
    "text",
    [
        "key=customer-id token_count=32 password_length=16 api_key_name=development",
        "monkey=value public_key_fingerprint=abc passwordless=true",
        "https://example.invalid/path user@example.invalid",
        "postgresql://db.example.invalid:5432/app owner@example.invalid",
        "https://example.invalid/path:word followed by owner@example.invalid",
        "password=\"\" api_key=''",
        'password="unterminated',
        "a" * 100_000 + "_API_KEY_SUFFIX=value",
    ],
)
def test_leaves_noncredential_diagnostics_alone(text: str):
    assert StructuredSecretRecognizer().analyze(text, ["SECRET"]) == []


class SecretAndEmailDetector:
    """A higher-confidence email intentionally overlaps the URI password."""

    name = "presidio"

    def __init__(self):
        self.calls = 0

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        self.calls += 1
        entities = [
            PIIEntity(s.entity_type, text[s.start : s.end], s.start, s.end, s.score, self.name)
            for s in StructuredSecretRecognizer().analyze(text, ["SECRET"])
        ]
        if "postgresql://" in text:
            start = text.index("synthetic-password")
            end = text.index(":5432")
            entities.append(PIIEntity("EMAIL_ADDRESS", text[start:end], start, end, 1.0, "onnx"))
        return entities


def make_engine(*, method: str = "redact", block: list[str] | None = None):
    engine = PIIEngine(
        PIIConfig(
            preset=None,
            anonymization={"entity_overrides": {"SECRET": {"method": method}}},
            block_entities=block or [],
            detection_cache={"enabled": True},
        )
    )
    detector = SecretAndEmailDetector()
    engine.detectors = [detector]
    return engine, detector


URI = "postgresql://service:synthetic-password@db.example.invalid:5432/app"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["redact", "mask"])
async def test_uri_overlap_is_irreversible_on_cold_and_cached_requests(method: str):
    engine, detector = make_engine(method=method)
    for _ in range(2):
        out, mapping = await engine.process_request([{"role": "tool", "content": URI}])
        assert "synthetic-password" not in out[0]["content"]
        assert mapping.get_all_fakes() == {}
        assert engine.deanonymizer.deanonymize(out[0]["content"], mapping) == out[0]["content"]
    assert detector.calls == 1


@pytest.mark.asyncio
async def test_overlap_cannot_bypass_block_gate_on_cold_or_cached_requests():
    engine, detector = make_engine(method="placeholder", block=["SECRET"])
    for _ in range(2):
        with pytest.raises(PIIBlockedError, match="SECRET") as exc:
            await engine.process_request([{"role": "tool", "content": URI}])
        assert "synthetic-password" not in str(exc.value)
    assert detector.calls == 1


@pytest.mark.asyncio
async def test_anonymization_policy_change_invalidates_cached_winner():
    engine, detector = make_engine(method="placeholder")
    _, before = await engine.process_request([{"role": "tool", "content": URI}])
    assert any("synthetic-password" in original for original in before.get_all_fakes().values())
    engine.anonymizer.config.entity_overrides["SECRET"].method = "redact"
    out, after = await engine.process_request([{"role": "tool", "content": URI}])
    assert "[SECRET]" in out[0]["content"]
    assert after.get_all_fakes() == {}
    assert detector.calls == 2


@pytest.mark.asyncio
async def test_global_irreversible_method_outranks_a_reversible_override():
    engine, _ = make_engine()
    engine.anonymizer.config.method = "redact"
    engine.anonymizer.config.entity_overrides = {
        "EMAIL_ADDRESS": engine.anonymizer.config.entity_overrides.pop("SECRET").model_copy(
            update={"method": "placeholder"}
        )
    }
    out, mapping = await engine.process_request([{"role": "tool", "content": URI}])
    assert "[SECRET]" in out[0]["content"]
    assert mapping.get_all_fakes() == {}


@pytest.mark.asyncio
async def test_block_policy_change_invalidates_cached_winner():
    engine, detector = make_engine(method="placeholder")
    await engine.process_request([{"role": "tool", "content": URI}])
    engine._blocked.add("SECRET")
    with pytest.raises(PIIBlockedError, match="SECRET"):
        await engine.process_request([{"role": "tool", "content": URI}])
    assert detector.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["chat", "anthropic", "responses"])
async def test_tool_output_surfaces_scan_secrets_and_preserve_assignment_syntax(surface: str):
    engine, _ = make_engine()
    log = 'INFO status=ok\npresented_key="fixture-only" smtp_secret=other-fixture'
    if surface == "chat":
        out, mapping = await engine.process_request([{"role": "tool", "content": log}])
    elif surface == "anthropic":
        out, mapping = await scrub_anthropic_request(
            engine,
            {"messages": [{"role": "user", "content": [{"type": "tool_result", "content": log}]}]},
        )
    else:
        out, mapping = await scrub_responses_request(
            engine, {"input": [{"type": "function_call_output", "output": log}]}
        )
    encoded = json.dumps(out)
    assert "fixture-only" not in encoded and "other-fixture" not in encoded
    assert "status=ok" in encoded and "smtp_secret=[SECRET]" in encoded
    assert 'presented_key=\\"[SECRET]\\"' in encoded
    assert mapping.get_all_fakes() == {}


@pytest.mark.parametrize("resolution", ["highest_score", "longest_span", "presidio_priority"])
@pytest.mark.parametrize("strategy", ["union", "intersection"])
def test_policy_priority_survives_transitive_overlaps(resolution: str, strategy: str):
    text = "0123456789abcdef"
    entities = [
        PIIEntity("PERSON", text[:9], 0, 9, 1.0, "presidio"),
        PIIEntity("CUSTOM_BLOCKED", text[6:10], 6, 10, 0.5, "onnx"),
        PIIEntity("EMAIL_ADDRESS", text[9:], 9, len(text), 1.0, "presidio"),
    ]
    result = merge_entities(
        entities,
        strategy=strategy,
        overlap_resolution=resolution,
        source_text=text,
        type_priorities={"CUSTOM_BLOCKED": 2, "PERSON": 1},
    )
    assert len(result) == 1
    assert result[0].entity_type == "CUSTOM_BLOCKED"
    assert result[0].text == text


def test_builtin_secret_type_is_producible_with_a_presidio_allowlist():
    engine = PIIEngine(
        PIIConfig(
            preset=None,
            detectors={"presidio": {"enabled": True, "entities": ["PERSON"]}},
            block_entities=["SECRET"],
        )
    )
    engine._validate_block_entities()
