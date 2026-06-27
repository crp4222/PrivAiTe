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
