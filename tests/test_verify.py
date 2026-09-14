"""Pins for `privaite verify`.

The command exists because the documented alternative (turn de-anonymization
off and read the reply) reports what came BACK, not what went OUT. These tests
run the real path: a throwaway provider on localhost, the real application over
an ASGI transport, and assertions on the recorded request body.
"""

from __future__ import annotations

import json

import pytest

from privaite.verify import (
    PLANTED,
    Capture,
    Finding,
    VerificationResult,
    agent_payload,
    format_report,
    run_verification,
)


def test_the_payload_carries_pii_in_the_three_agent_places():
    """Text-only guardrails scrub the message and forward the rest, so a payload
    without tool-call arguments and tool output would verify nothing."""
    payload = agent_payload("m")
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["user", "assistant", "tool"]

    serialized = json.dumps(payload)
    for value, _type, _where in PLANTED:
        assert value in serialized, f"{value!r} is not actually planted in the payload"

    arguments = payload["messages"][1]["tool_calls"][0]["function"]["arguments"]
    assert "marie.dupont@example.invalid" in arguments
    assert json.loads(arguments)


def test_planted_values_are_unusable():
    """This text lands in terminals, screenshots and issue reports."""
    values = " ".join(v for v, _t, _w in PLANTED)
    assert ".invalid" in values  # reserved TLD, can never resolve
    assert "not-a-real-key" in values
    assert "4111 1111 1111 1111" in values  # the published test card number


def test_capture_records_the_body_and_answers_openai_shaped():
    import httpx

    capture = Capture().start()
    try:
        response = httpx.post(
            f"{capture.base_url}/chat/completions",
            json={"messages": [{"role": "user", "content": "hello Marie"}]},
            timeout=10,
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "hello Marie"
        assert capture.bodies and "hello Marie" in capture.bodies[-1]
    finally:
        capture.stop()


def _result(leak: bool, restored: bool = True) -> VerificationResult:
    findings = [Finding(v, t, w, on_the_wire=leak) for v, t, w in PLANTED]
    return VerificationResult(
        preset="light",
        direct=[Finding(v, t, w, on_the_wire=True) for v, t, w in PLANTED],
        through_proxy=findings,
        placeholders=["<PERSON_1>"],
        restored=restored,
        elapsed_ms=1.0,
    )


def test_a_leak_makes_the_result_fail():
    assert _result(leak=False).ok is True
    assert _result(leak=True).ok is False
    assert [f.value for f in _result(leak=True).leaked] == [v for v, _t, _w in PLANTED]


def test_a_missing_restore_fails_even_with_nothing_leaked():
    """Scrubbing without restoring is not a pass: the user would lose the values."""
    assert _result(leak=False, restored=False).ok is False


def test_the_report_names_what_leaked_and_where_to_report_it():
    report = format_report(_result(leak=True))
    assert "FAILED" in report
    assert "github.com/crp4222/PrivAiTe/issues" in report
    assert "Marie Dupont" in report

    passed = format_report(_result(leak=False))
    assert "PASSED" in passed
    assert "FAILED" not in passed


@pytest.mark.asyncio
async def test_end_to_end_nothing_planted_reaches_the_wire():
    """The real thing: real app, real startup path, assertions on the recorded
    outbound body. `light` is used so the test needs no model download."""
    result = await run_verification(preset="light")

    assert all(f.on_the_wire for f in result.direct), (
        "the baseline must leak, otherwise the comparison proves nothing"
    )
    # Whatever the preset misses, the structured fields are the claim under test:
    # they are what text-only guardrails forward untouched.
    structured = [f for f in result.through_proxy if f.where != "message text"]
    assert [f.value for f in structured if f.on_the_wire] == []
    assert result.placeholders
    assert result.restored is True
