"""pii.allow_list through the engine: every scanned surface, the cache, the
propagation of known credentials, the block gate and the inspect view."""

from __future__ import annotations

import json
import logging
import re

import pytest

from privaite.config.schema import (
    AllowListEntry,
    DetectionCacheConfig,
    DetectorsConfig,
    PIIConfig,
    PresidioDetectorConfig,
)
from privaite.pii.allow_list import compile_rules
from privaite.pii.detector_base import PIIDetector
from privaite.pii.engine import DetectionReport, PIIBlockedError, PIIEngine
from privaite.pii.entity import PIIEntity

ALGO = "chacha20-poly1305@openssh.com"
MAIL = "jane.doe@example.org"
SSHD_CONFIG = f"""# maintainer {MAIL}
KexAlgorithms sntrup761x25519-sha512@openssh.com,curve25519-sha256@libssh.org
Ciphers {ALGO},aes256-gcm@openssh.com
MACs hmac-sha2-512-etm@openssh.com,umac-128-etm@openssh.com
HostKeyAlgorithms ssh-ed25519-cert-v01@openssh.com,ssh-ed25519
"""
ALGORITHM_NAMES = re.findall(r"[A-Za-z0-9.+_-]+@(?:openssh\.com|libssh\.org)", SSHD_CONFIG)
# The recipe documented in docs/configuration.md: the names, and the hostnames
# Presidio's URL pattern sees inside them.
SSH_ENTRIES = [
    {"pattern": r"[A-Za-z0-9.+_-]+@(openssh\.com|libssh\.org)"},
    {"pattern": r"(openssh\.com|libssh\.org)"},
]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class RegexDetector(PIIDetector):
    """Emits one entity type per regex match; a `value` group narrows the span."""

    def __init__(self, pattern: str, entity_type: str, source: str = "fake") -> None:
        self.pattern = re.compile(pattern)
        self.entity_type = entity_type
        self.source = source
        self.calls = 0

    @property
    def name(self) -> str:
        return self.source

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        self.calls += 1
        out = []
        for match in self.pattern.finditer(text):
            group = "value" if "value" in self.pattern.groupindex else 0
            start, end = match.span(group)
            out.append(PIIEntity(self.entity_type, text[start:end], start, end, 0.9, self.source))
        return out


def make_engine(
    allow_list: list[dict],
    *,
    detectors: list[PIIDetector] | None = None,
    block: list[str] | None = None,
    cache: bool = False,
) -> PIIEngine:
    config = PIIConfig(
        preset=None,
        detectors=DetectorsConfig(
            presidio=PresidioDetectorConfig(enabled=True, languages=["en"], nlp_engine="blank")
        ),
        allow_list=allow_list,
        block_entities=block or [],
        detection_cache=DetectionCacheConfig(enabled=cache),
    )
    engine = PIIEngine(config)
    if detectors is not None:
        engine.detectors = detectors
        engine._ready = True
    return engine


async def scrub(engine: PIIEngine, content: str) -> str:
    out, _mapping = await engine.process_request([{"role": "user", "content": content}])
    return out[0]["content"]


@pytest.mark.asyncio
async def test_sshd_config_algorithm_names_pass_and_the_maintainer_address_stays_masked():
    engine = make_engine(SSH_ENTRIES)
    await engine.initialize()  # Presidio on a blank pipeline: its patterns, no model
    out = await scrub(engine, SSHD_CONFIG)
    assert len(ALGORITHM_NAMES) == 7
    for name in ALGORITHM_NAMES:
        assert name in out
    assert MAIL not in out
    report = await engine.inspect_text_detailed(SSHD_CONFIG)
    assert [e.text for e in report.entities] == [MAIL]
    assert sorted(a.text for a in report.allowed) == sorted(ALGORITHM_NAMES)
    assert report.not_allowed == []


@pytest.mark.asyncio
async def test_inspect_names_the_detection_that_keeps_a_listed_value_redacted():
    engine = make_engine(SSH_ENTRIES[:1])  # the names only, not the hostnames inside them
    await engine.initialize()
    assert ALGO not in await scrub(engine, SSHD_CONFIG)
    report = await engine.inspect_text_detailed(SSHD_CONFIG)
    assert len(report.entities) == 8 and report.allowed == []
    assert {(e.entity_type, e.text) for e in report.not_allowed} == {
        ("URL", "openssh.com"),
        ("URL", "libssh.org"),
    }


@pytest.mark.asyncio
async def test_without_the_list_the_same_text_is_fully_masked():
    engine = make_engine([])
    await engine.initialize()
    out = await scrub(engine, SSHD_CONFIG)
    assert ALGO not in out and MAIL not in out
    report = await engine.inspect_text_detailed(SSHD_CONFIG)
    assert report == DetectionReport(await engine.inspect_text(SSHD_CONFIG), [], [])


@pytest.mark.asyncio
async def test_the_list_applies_to_tool_call_arguments():
    engine = make_engine(SSH_ENTRIES, detectors=[RegexDetector(EMAIL_RE.pattern, "EMAIL_ADDRESS")])
    arguments = json.dumps({"path": "/etc/ssh/sshd_config", "line": f"Ciphers {ALGO}", "cc": MAIL})
    out, _ = await engine.process_request(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "edit", "arguments": arguments},
                    }
                ],
            }
        ]
    )
    forwarded = out[0]["tool_calls"][0]["function"]["arguments"]
    assert ALGO in forwarded
    assert MAIL not in forwarded


SECRET = "Xk9mQ2vLp7Rt4wZs"
ENV_LINE = f"VAULT_PASSWORD={SECRET}"


def secret_detector() -> RegexDetector:
    return RegexDetector(r"VAULT_PASSWORD=(?P<value>\S+)", "SECRET", "model")


@pytest.mark.asyncio
async def test_an_allowed_credential_is_neither_masked_nor_propagated():
    messages = [
        {"role": "tool", "tool_call_id": "t1", "content": ENV_LINE},
        {"role": "user", "content": f"use {SECRET} then"},
    ]
    # Without the list, the bare repeat is masked by propagation (no detector sees it).
    out, _ = await make_engine([], detectors=[secret_detector()]).process_request(messages)
    assert SECRET not in json.dumps(out)
    # Listed with SECRET named: never detected, so never remembered, so never propagated.
    explicit = [{"value": SECRET, "entity_types": ["SECRET"]}]
    out, _ = await make_engine(explicit, detectors=[secret_detector()]).process_request(messages)
    assert out[0]["content"] == ENV_LINE
    assert out[1]["content"] == f"use {SECRET} then"


@pytest.mark.asyncio
async def test_an_entry_without_entity_types_never_touches_a_credential():
    engine = make_engine([{"value": SECRET}], detectors=[secret_detector()])
    assert SECRET not in await scrub(engine, ENV_LINE)


@pytest.mark.asyncio
async def test_a_blocked_type_passes_the_gate_only_with_an_explicit_entry():
    detector = RegexDetector(EMAIL_RE.pattern, "EMAIL_ADDRESS")
    with pytest.raises(PIIBlockedError):
        await scrub(
            make_engine([{"value": ALGO}], detectors=[detector], block=["EMAIL_ADDRESS"]), ALGO
        )
    explicit = [{"value": ALGO, "entity_types": ["EMAIL_ADDRESS"]}]
    engine = make_engine(explicit, detectors=[detector], block=["EMAIL_ADDRESS"])
    assert await scrub(engine, f"Ciphers {ALGO}") == f"Ciphers {ALGO}"
    with pytest.raises(PIIBlockedError):
        await scrub(engine, f"Ciphers {ALGO} cc {MAIL}")


@pytest.mark.asyncio
async def test_naming_a_blocked_type_is_said_at_startup(caplog: pytest.LogCaptureFixture):
    engine = make_engine(
        [{"value": ALGO, "entity_types": ["EMAIL_ADDRESS"]}, {"value": "openssh.com"}],
        block=["EMAIL_ADDRESS"],
    )
    with caplog.at_level(logging.WARNING, logger="privaite.pii.engine"):
        await engine.initialize()
    messages = [r.getMessage() for r in caplog.records if "allow_list" in r.getMessage()]
    assert messages == [
        "allow_list entries name blocked type(s) EMAIL_ADDRESS: a value they match is "
        "forwarded instead of rejected"
    ]
    assert ALGO not in caplog.text


@pytest.mark.asyncio
async def test_the_cache_serves_the_same_report_and_the_list_is_part_of_the_key():
    detector = RegexDetector(EMAIL_RE.pattern, "EMAIL_ADDRESS")
    engine = make_engine(SSH_ENTRIES, detectors=[detector], cache=True)
    first = await engine.inspect_text_detailed(SSHD_CONFIG)
    second = await engine.inspect_text_detailed(SSHD_CONFIG)
    assert detector.calls == 1
    assert engine._cache is not None and engine._cache.hits == 1
    assert second == first
    assert [e.text for e in first.entities] == [MAIL] and len(first.allowed) == 7
    # A different list must never be served the spans of the old one.
    engine.config.allow_list.append(AllowListEntry(value=MAIL, entity_types=["EMAIL_ADDRESS"]))
    engine._allow_rules = compile_rules(engine.config.allow_list)
    report = await engine.inspect_text_detailed(SSHD_CONFIG)
    assert detector.calls == 2
    assert report.entities == [] and len(report.allowed) == 8


@pytest.mark.asyncio
async def test_an_entry_that_matches_nothing_changes_nothing():
    detector = RegexDetector(EMAIL_RE.pattern, "EMAIL_ADDRESS")
    plain = await make_engine([], detectors=[detector]).inspect_text_detailed(SSHD_CONFIG)
    listed = make_engine([{"value": "nothing-here"}], detectors=[detector])
    report = await listed.inspect_text_detailed(SSHD_CONFIG)
    assert report == plain and report.allowed == [] and report.not_allowed == []
