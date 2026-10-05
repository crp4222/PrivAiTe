"""A value identified as a secret once is a secret wherever the request repeats it.

An agent reads a credential from a file (where a rule recognises it), receives
the restored value back, and then repeats it bare: in its reasoning, in the next
command, in a tool output. No detector has the context to recognise it there, so
re-detection alone leaks it. Measured on a real OpenCode session: the vault
password came back inside the assistant's reasoning ("the output shows a
placeholder `...`") and reached the provider on the next turn.
"""

from __future__ import annotations

import json

import pytest

from privaite.config.schema import PIIConfig
from privaite.gateway.scrub import scrub_anthropic_request, scrub_responses_request
from privaite.pii.engine import PIIBlockedError, PIIEngine
from privaite.pii.entity import PIIEntity
from tests.test_pii.test_structured_secrets import make_engine

SECRET = "Xk9mQ2vLp7Rt4wZs"
METHODS = ["placeholder", "redact", "mask"]


def history(echo: dict) -> list[dict]:
    return [
        {"role": "user", "content": "Open the vault."},
        {"role": "tool", "tool_call_id": "call-1", "content": f"VAULT_PASSWORD={SECRET}"},
        echo,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize(
    "echo",
    [
        {"role": "assistant", "content": f"The output shows a placeholder `{SECRET}`."},
        {"role": "assistant", "content": "", "reasoning_content": f"I will pass {SECRET} on."},
        {"role": "user", "content": f"{SECRET}"},
        {"role": "tool", "tool_call_id": "call-2", "content": f"echo: {SECRET}\n"},
    ],
)
async def test_a_known_secret_is_scrubbed_where_no_detector_recognises_it(method, echo):
    engine, detector = make_engine(method=method)
    for _ in range(2):  # cold, then served from the detection cache
        out, mapping = await engine.process_request(history(echo))
        assert SECRET not in json.dumps(out)
        assert mapping.get_entity_type(SECRET) == "SECRET"
    if method == "placeholder":
        fake = mapping.get_fake(SECRET)
        assert json.dumps(out).count(fake) == 2
        restored = engine.deanonymizer.deanonymize(json.dumps(out), mapping)
        assert restored == json.dumps(history(echo))


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
async def test_a_secret_repeated_in_the_same_text_is_scrubbed_everywhere(method):
    engine, _ = make_engine(method=method)
    text = f"password={SECRET}\n$ run --with {SECRET}\nthe value was {SECRET}."
    out, _ = await engine.process_request([{"role": "user", "content": text}])
    assert SECRET not in out[0]["content"]
    assert "password=" in out[0]["content"] and "$ run --with " in out[0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
async def test_gateway_scrub_propagates_a_known_secret_to_later_items(method):
    engine, _ = make_engine(method=method)
    body = {
        "input": [
            {
                "type": "custom_tool_call_output",
                "output": [{"type": "text", "text": f"VAULT_PASSWORD={SECRET}"}],
            },
            {"type": "message", "role": "assistant", "content": f"It printed {SECRET}."},
            {"type": "custom_tool_call", "name": "exec", "input": f"open_vault {SECRET}"},
        ]
    }
    out, _ = await scrub_responses_request(engine, body)
    assert SECRET not in json.dumps(out)
    assert "open_vault " in out["input"][2]["input"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
async def test_anthropic_scrub_propagates_a_known_secret_to_later_blocks(method):
    engine, _ = make_engine(method=method)
    body = {
        "model": "m",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": f"VAULT_PASSWORD={SECRET}"}],
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"It printed {SECRET}."},
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Bash",
                        "input": {"command": f"open_vault {SECRET}"},
                    },
                ],
            },
        ],
    }
    out, _ = await scrub_anthropic_request(engine, body)
    assert SECRET not in json.dumps(out)
    assert out["messages"][1]["content"][1]["input"]["command"].startswith("open_vault ")


@pytest.mark.asyncio
async def test_a_known_secret_keeps_the_block_gate_closed():
    engine, _ = make_engine(block=["SECRET"])
    with pytest.raises(PIIBlockedError):
        await engine.process_request(history({"role": "assistant", "content": f"got {SECRET}"}))


class FragmentDetector:
    """Recognises the assignment, then only a fragment of the same value."""

    name = "presidio"

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        if "VAULT_PASSWORD=" in text:
            start = text.index(SECRET)
            return [PIIEntity("SECRET", SECRET, start, start + len(SECRET), 0.9, self.name)]
        if SECRET in text:
            start = text.index(SECRET) + 2
            return [PIIEntity("SECRET", SECRET[2:7], start, start + 5, 0.6, self.name)]
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
async def test_a_fragment_detected_inside_a_known_secret_gives_way_to_the_whole_value(method):
    engine = PIIEngine(
        PIIConfig(preset=None, anonymization={"entity_overrides": {"SECRET": {"method": method}}})
    )
    engine.detectors = [FragmentDetector()]
    echo = {"role": "assistant", "content": f"value: {SECRET}!"}
    out, mapping = await engine.process_request(history(echo))
    assert SECRET[:2] not in out[2]["content"].replace("value", "")
    assert SECRET[7:] not in out[2]["content"]
    assert out[2]["content"].startswith("value: ") and out[2]["content"].endswith("!")
    if method == "placeholder":
        assert out[2]["content"] == f"value: {mapping.get_fake(SECRET)}!"


class StraddlingDetector:
    """Recognises the assignment, then a span that starts inside the value and runs past it."""

    name = "onnx"

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        start = text.index(SECRET) if SECRET in text else -1
        if "VAULT_PASSWORD=" in text:
            return [PIIEntity("SECRET", SECRET, start, start + len(SECRET), 0.9, self.name)]
        if start != -1:
            tail = start + 10
            end = text.index("Moreau") + len("Moreau")
            return [PIIEntity("PERSON", text[tail:end], tail, end, 0.99, self.name)]
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("block", [[], ["PERSON"]])
async def test_a_detection_straddling_a_known_secret_leaves_no_part_of_it(method, block):
    engine = PIIEngine(
        PIIConfig(
            preset=None,
            anonymization={"entity_overrides": {"SECRET": {"method": method}}},
            block_entities=block,
        )
    )
    engine.detectors = [StraddlingDetector()]
    echo = {"role": "assistant", "content": f"value: {SECRET} Moreau, done"}
    if block:
        # The overlapping type keeps its place at the gate.
        with pytest.raises(PIIBlockedError):
            await engine.process_request(history(echo))
        return
    out, mapping = await engine.process_request(history(echo))
    assert SECRET[:10] not in out[2]["content"]
    assert SECRET[10:] not in out[2]["content"] and "Moreau" not in out[2]["content"]
    assert out[2]["content"].startswith("value: ") and out[2]["content"].endswith(", done")
    if method == "placeholder":
        restored = engine.deanonymizer.deanonymize(out[2]["content"], mapping)
        assert restored == echo["content"]


class OtherTypeDetector:
    """Recognises the assignment, then reports the next copy under another type."""

    name = "onnx"

    def __init__(self, other: str, wider: bool = False) -> None:
        self.other = other
        self.wider = wider

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        start = text.find(SECRET)
        end = start + len(SECRET)
        if "VAULT_PASSWORD=" in text:
            return [PIIEntity("SECRET", SECRET, start, end, 0.9, self.name)]
        if text.startswith("typed "):
            if self.wider:
                end = text.index(".invalid") + len(".invalid")
            return [PIIEntity(self.other, text[start:end], start, end, 1.0, self.name)]
        return []


def other_type_engine(secret_method: str, other: str, other_method: str, **kwargs):
    engine = PIIEngine(
        PIIConfig(
            preset=None,
            anonymization={
                "entity_overrides": {
                    "SECRET": {"method": secret_method},
                    other: {"method": other_method},
                }
            },
            block_entities=kwargs.pop("block", []),
        )
    )
    engine.detectors = [OtherTypeDetector(other, **kwargs)]
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("secret_method", "other_method"),
    [
        ("placeholder", "placeholder"),
        ("redact", "placeholder"),
        ("placeholder", "mask"),
        ("redact", "mask"),
    ],
)
async def test_a_secret_stays_known_after_a_copy_is_detected_as_another_type(
    secret_method, other_method
):
    """The later detection must not make the request forget the value is a secret."""
    engine = other_type_engine(secret_method, "EMAIL_ADDRESS", other_method)
    messages = [
        *history({"role": "assistant", "content": f"typed {SECRET}"}),
        {"role": "assistant", "content": f"and again {SECRET}."},
    ]
    out, _ = await engine.process_request(messages)
    assert SECRET not in json.dumps(out)
    assert out[3]["content"].startswith("and again ") and out[3]["content"].endswith(".")


@pytest.mark.asyncio
async def test_the_secret_policy_outranks_another_type_on_a_copy():
    """A redacted secret must not become restorable because a copy looked like an email."""
    engine = other_type_engine("redact", "EMAIL_ADDRESS", "placeholder")
    out, mapping = await engine.process_request(
        history({"role": "assistant", "content": f"typed {SECRET}"})
    )
    assert out[2]["content"] == "typed [SECRET]"
    assert mapping.get_all_fakes() == {}


@pytest.mark.asyncio
async def test_a_blocked_type_on_a_copy_of_a_known_secret_still_blocks():
    engine = other_type_engine("placeholder", "US_SSN", "placeholder", block=["US_SSN"])
    with pytest.raises(PIIBlockedError):
        await engine.process_request(history({"role": "assistant", "content": f"typed {SECRET}"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("secret_method", ["placeholder", "redact"])
async def test_a_wider_detection_around_a_known_secret_covers_both(secret_method):
    engine = other_type_engine(secret_method, "EMAIL_ADDRESS", "placeholder", wider=True)
    echo = {"role": "assistant", "content": f"typed {SECRET}@db.example.invalid now"}
    out, mapping = await engine.process_request(history(echo))
    assert SECRET not in out[2]["content"] and "db.example" not in out[2]["content"]
    assert out[2]["content"].startswith("typed ") and out[2]["content"].endswith(" now")
    if secret_method == "redact":
        assert out[2]["content"] == "typed [SECRET] now"
    else:
        restored = engine.deanonymizer.deanonymize(out[2]["content"], mapping)
        assert restored == echo["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "later"),
    [
        # Too short to be propagated safely: it would hit unrelated text.
        ("password=ab12", "the code ab12 is public"),
        ("password=hunter2", "hunter2 is a film"),
        # Embedded in a longer identifier: a different string, not the secret.
        ("password=demo-0nly-value7", "the id xdemo-0nly-value7z is public"),
        # A plain word or an identifier after a credential field is as often code
        # as a credential: spreading it would rewrite the source the agent reads.
        ("password=changeme", "changeme before going live"),
        ("connect(api_key=api_key, timeout=30)", "def connect(api_key, timeout): ..."),
        ("password: required", "this field is required"),
        ("auth_token=12345678", "order 12345678 shipped"),
    ],
)
async def test_propagation_is_limited_to_token_shaped_values(source, later):
    engine, _ = make_engine(method="placeholder")
    out, _ = await engine.process_request(
        [
            {"role": "tool", "tool_call_id": "c", "content": source},
            {"role": "assistant", "content": later},
        ]
    )
    assert out[1]["content"] == later


class ModelOnlyDetector:
    """A model labels an identifier as a secret, with nothing naming it a credential."""

    name = "onnx"

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        marker = "plugin: figma@curated-remote1"
        if marker in text:
            start = text.index("figma@")
            end = start + len("figma@curated-remote1")
            return [PIIEntity("SECRET", text[start:end], start, end, 0.97, self.name)]
        return []


@pytest.mark.asyncio
async def test_a_secret_no_credential_rule_names_is_not_propagated():
    """Measured on Codex: a plugin identifier the model mislabelled was copied 24 times."""
    engine = PIIEngine(PIIConfig(preset=None))
    engine.detectors = [ModelOnlyDetector()]
    later = "enabled: figma@curated-remote1, gmail@curated-remote1"
    out, mapping = await engine.process_request(
        [
            {"role": "user", "content": "plugin: figma@curated-remote1"},
            {"role": "user", "content": later},
        ]
    )
    assert out[0]["content"] == f"plugin: {mapping.get_fake('figma@curated-remote1')}"
    assert out[1]["content"] == later


@pytest.mark.asyncio
async def test_disabling_the_secret_rules_disables_the_propagation():
    engine = PIIEngine(
        PIIConfig(
            preset=None,
            detectors={"presidio": {"disabled_recognizers": ["StructuredSecretRecognizer"]}},
        )
    )
    engine.detectors = [OtherTypeDetector("EMAIL_ADDRESS")]
    out, _ = await engine.process_request(
        history({"role": "assistant", "content": f"bare {SECRET}"})
    )
    assert out[2]["content"] == f"bare {SECRET}"


@pytest.mark.asyncio
async def test_a_rule_match_no_detection_kept_is_not_propagated():
    """The rules only qualify a value some detector reported for this text."""
    engine = PIIEngine(PIIConfig(preset=None))
    engine.detectors = [NameOnceDetector()]
    out, _ = await engine.process_request(
        [
            {"role": "tool", "tool_call_id": "c", "content": f"VAULT_PASSWORD={SECRET}"},
            {"role": "assistant", "content": f"bare {SECRET}"},
        ]
    )
    assert out[1]["content"] == f"bare {SECRET}"


class NameOnceDetector:
    name = "presidio"

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        marker = "Customer: Camille Moreau"
        if marker in text:
            start = text.index("Camille Moreau")
            return [PIIEntity("PERSON", "Camille Moreau", start, start + 14, 0.9, self.name)]
        return []


@pytest.mark.asyncio
async def test_only_secrets_are_propagated():
    """A name is common text: one false detection must not spread over the request."""
    engine = PIIEngine(PIIConfig(preset=None))
    engine.detectors = [NameOnceDetector()]
    out, _ = await engine.process_request(
        [
            {"role": "user", "content": "Customer: Camille Moreau"},
            {"role": "assistant", "content": "Camille Moreau was contacted."},
        ]
    )
    assert "Camille Moreau" not in out[0]["content"]
    assert out[1]["content"] == "Camille Moreau was contacted."
