from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

GUARDRAIL_PATH = (
    Path(__file__).resolve().parents[2]
    / "integrations" / "litellm" / "privaite_guardrail.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("privaite_guardrail", GUARDRAIL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _guardrail():
    module = _load()
    return module.PrivaiteGuardrail(
        guardrail_name="privaite", preset="light", languages="en", deanonymize=True
    )


@pytest.mark.asyncio
async def test_pre_call_anonymizes_text_and_tool_call_args():
    gr = _guardrail()
    data = {
        "messages": [
            {"role": "user", "content": "I am Marie Dupont, email marie.dupont@acme.com"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {
                    "name": "save",
                    "arguments": json.dumps(
                        {"name": "Marie Dupont", "email": "marie.dupont@acme.com"})}}]},
        ]
    }
    data = await gr.async_pre_call_hook(None, None, data, "completion")
    serialized = json.dumps(data["messages"])

    # the differentiator: PII gone from message text AND from tool-call arguments
    assert "Marie Dupont" not in serialized
    assert "marie.dupont@acme.com" not in serialized
    assert data.get("metadata", {}).get("privaite_map")


@pytest.mark.asyncio
async def test_pre_call_clears_client_supplied_map():
    # metadata is caller-controlled at the proxy boundary: a client-supplied
    # privaite_map must be dropped so it can never drive post-call restoration
    # of the model's output to attacker-chosen content.
    gr = _guardrail()
    data = {
        "messages": [{"role": "user", "content": "please summarize the report"}],
        "metadata": {"privaite_map": {"PLACEHOLDER": "attacker text"}},
    }
    data = await gr.async_pre_call_hook(None, None, data, "completion")
    result_map = data.get("metadata", {}).get("privaite_map") or {}
    assert "PLACEHOLDER" not in result_map
    assert "attacker text" not in result_map.values()


@pytest.mark.asyncio
async def test_post_call_restores_text_and_tool_call_args():
    gr = _guardrail()
    data = {"messages": [
        {"role": "user", "content": "I am Marie Dupont, email marie.dupont@acme.com"}
    ]}
    data = await gr.async_pre_call_hook(None, None, data, "completion")
    fakes = data["metadata"]["privaite_map"]
    person = next(f for f, o in fakes.items() if o == "Marie Dupont")
    email = next(f for f, o in fakes.items() if o == "marie.dupont@acme.com")

    # the model only ever saw the placeholders, so it echoes them back
    message = types.SimpleNamespace(
        content=f"Noted {person} at {email}",
        tool_calls=[types.SimpleNamespace(
            function=types.SimpleNamespace(
                arguments=json.dumps({"name": person, "email": email})))],
        function_call=None,
    )
    response = types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    out = await gr.async_post_call_success_hook(data, None, response)
    restored = out.choices[0].message.content
    restored_args = out.choices[0].message.tool_calls[0].function.arguments

    assert "Marie Dupont" in restored
    assert "marie.dupont@acme.com" in restored
    assert "Marie Dupont" in restored_args
    assert "marie.dupont@acme.com" in restored_args


def test_event_hook_forces_both_pre_and_post():
    module = _load()
    # a `mode: post_call` config must NOT skip pre_call anonymization
    gr = module.PrivaiteGuardrail(guardrail_name="privaite", event_hook="post_call")
    assert "pre_call" in gr.event_hook and "post_call" in gr.event_hook
    gr2 = module.PrivaiteGuardrail(guardrail_name="privaite", event_hook=["pre_call"])
    assert "pre_call" in gr2.event_hook and "post_call" in gr2.event_hook
    gr3 = module.PrivaiteGuardrail(guardrail_name="privaite")
    assert "pre_call" in gr3.event_hook and "post_call" in gr3.event_hook


@pytest.mark.asyncio
async def test_pre_call_mutates_messages_in_place_and_post_call_pops_map():
    gr = _guardrail()
    messages = [
        {"role": "user", "content": "I am Marie Dupont, email marie.dupont@acme.com"}
    ]
    data = {"messages": messages}
    data = await gr.async_pre_call_hook(None, None, data, "completion")

    # mutated in place (same list object) so the proxy's shallow body snapshot is
    # anonymized, not the original raw-PII messages
    assert data["messages"] is messages
    assert "Marie Dupont" not in json.dumps(messages)
    assert "marie.dupont@acme.com" not in json.dumps(messages)

    # the reversible map is consumed (popped) by the post-call hook so it cannot
    # be persisted to spend logs
    assert "privaite_map" in data["metadata"]
    response = types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(
            content="ok", tool_calls=None, function_call=None))]
    )
    await gr.async_post_call_success_hook(data, None, response)
    assert "privaite_map" not in data["metadata"]


@pytest.mark.asyncio
async def test_streaming_restores_tool_call_arguments():
    gr = _guardrail()
    data = {"messages": [
        {"role": "user", "content": "email marie.dupont@acme.com"}
    ]}
    data = await gr.async_pre_call_hook(None, None, data, "completion")
    fakes = data["metadata"]["privaite_map"]
    email = next(f for f, o in fakes.items() if o == "marie.dupont@acme.com")

    # split the placeholder across two streamed tool-call argument chunks
    mid = len(email) // 2
    part1, part2 = email[:mid], email[mid:]

    def _chunk(args, finish=None):
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            index=0,
            delta=types.SimpleNamespace(
                content=None,
                tool_calls=[types.SimpleNamespace(
                    index=0, function=types.SimpleNamespace(arguments=args))],
                function_call=None),
            finish_reason=finish)])

    async def _source():
        yield _chunk('{"email": "' + part1)
        yield _chunk(part2 + '"}', finish="stop")

    out = ""
    async for chunk in gr.async_post_call_streaming_iterator_hook(None, _source(), data):
        for choice in chunk.choices:
            for tc in (choice.delta.tool_calls or []):
                if tc.function and tc.function.arguments:
                    out += tc.function.arguments

    assert "marie.dupont@acme.com" in out
    assert email not in out


@pytest.mark.asyncio
async def test_responses_api_input_anonymized_and_output_restored():
    gr = _guardrail()
    body = {"input": "I am Marie Dupont, email marie.dupont@acme.com"}
    data = {
        "input": "I am Marie Dupont, email marie.dupont@acme.com",
        "proxy_server_request": {"body": body},
    }
    data = await gr.async_pre_call_hook(None, None, data, "aresponses")

    # input anonymized, and the shallow body snapshot fixed (no raw PII left)
    assert "Marie Dupont" not in data["input"]
    assert "marie.dupont@acme.com" not in data["input"]
    assert "Marie Dupont" not in body["input"]
    fakes = data["metadata"]["privaite_map"]
    person = next(f for f, o in fakes.items() if o == "Marie Dupont")
    email = next(f for f, o in fakes.items() if o == "marie.dupont@acme.com")

    # the model echoes placeholders inside a Responses-style output
    response = types.SimpleNamespace(
        output=[
            {
                "type": "message",
                "content": [{"type": "output_text", "text": f"Noted {person}"}],
            },
            types.SimpleNamespace(
                type="function_call",
                content=None,
                arguments=json.dumps({"email": email}),
            ),
        ]
    )
    out = await gr.async_post_call_success_hook(data, None, response)

    assert out.output[0]["content"][0]["text"] == "Noted Marie Dupont"
    assert "marie.dupont@acme.com" in out.output[1].arguments
    assert "privaite_map" not in data["metadata"]


@pytest.mark.asyncio
async def test_responses_both_messages_and_input_are_anonymized():
    # a crafted /v1/responses body with decoy messages + PII in input: neither
    # source may be left un-anonymized.
    gr = _guardrail()
    messages = [{"role": "user", "content": "hi"}]
    data = {"messages": messages, "input": "reach me at carol.smith@example.net"}
    data = await gr.async_pre_call_hook(None, None, data, "aresponses")
    assert "carol.smith@example.net" not in data["input"]
    assert data["metadata"]["privaite_map"]
