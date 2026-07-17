from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

GUARDRAIL_PATH = (
    Path(__file__).resolve().parents[2] / "integrations" / "litellm" / "privaite_guardrail.py"
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
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "save",
                            "arguments": json.dumps(
                                {"name": "Marie Dupont", "email": "marie.dupont@acme.com"}
                            ),
                        },
                    }
                ],
            },
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
    data = {
        "messages": [{"role": "user", "content": "I am Marie Dupont, email marie.dupont@acme.com"}]
    }
    data = await gr.async_pre_call_hook(None, None, data, "completion")
    fakes = data["metadata"]["privaite_map"]
    person = next(f for f, o in fakes.items() if o == "Marie Dupont")
    email = next(f for f, o in fakes.items() if o == "marie.dupont@acme.com")

    # the model only ever saw the placeholders, so it echoes them back
    message = types.SimpleNamespace(
        content=f"Noted {person} at {email}",
        tool_calls=[
            types.SimpleNamespace(
                function=types.SimpleNamespace(
                    arguments=json.dumps({"name": person, "email": email})
                )
            )
        ],
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


@pytest.mark.asyncio
async def test_post_call_restores_reasoning_and_legacy_function_call_arguments():
    # Non-streaming restoration has to cover the same fields as the proxy: the
    # model can echo placeholders in a reasoning trace or in legacy function
    # calls just as easily as in content/tool_calls.
    gr = _guardrail()
    data = {"messages": [{"role": "user", "content": "I am Marie Dupont"}]}
    data = await gr.async_pre_call_hook(None, None, data, "completion")
    fakes = data["metadata"]["privaite_map"]
    person = next(fake for fake, original in fakes.items() if original == "Marie Dupont")

    message = types.SimpleNamespace(
        content=f"Hello {person}",
        reasoning_content=f"Considering {person}",
        reasoning=f"Checked {person}",
        tool_calls=None,
        function_call=types.SimpleNamespace(arguments=json.dumps({"recipient": person})),
    )
    response = types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    out = await gr.async_post_call_success_hook(data, None, response)
    restored = out.choices[0].message
    assert restored.content == "Hello Marie Dupont"
    assert restored.reasoning_content == "Considering Marie Dupont"
    assert restored.reasoning == "Checked Marie Dupont"
    assert json.loads(restored.function_call.arguments) == {"recipient": "Marie Dupont"}
    assert "privaite_map" not in data["metadata"]


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
    messages = [{"role": "user", "content": "I am Marie Dupont, email marie.dupont@acme.com"}]
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
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content="ok", tool_calls=None, function_call=None)
            )
        ]
    )
    await gr.async_post_call_success_hook(data, None, response)
    assert "privaite_map" not in data["metadata"]


@pytest.mark.asyncio
async def test_streaming_restores_tool_call_arguments():
    gr = _guardrail()
    data = {"messages": [{"role": "user", "content": "email marie.dupont@acme.com"}]}
    data = await gr.async_pre_call_hook(None, None, data, "completion")
    fakes = data["metadata"]["privaite_map"]
    email = next(f for f, o in fakes.items() if o == "marie.dupont@acme.com")

    # split the placeholder across two streamed tool-call argument chunks
    mid = len(email) // 2
    part1, part2 = email[:mid], email[mid:]

    def _chunk(args, finish=None):
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    index=0,
                    delta=types.SimpleNamespace(
                        content=None,
                        tool_calls=[
                            types.SimpleNamespace(
                                index=0, function=types.SimpleNamespace(arguments=args)
                            )
                        ],
                        function_call=None,
                    ),
                    finish_reason=finish,
                )
            ]
        )

    async def _source():
        yield _chunk('{"email": "' + part1)
        yield _chunk(part2 + '"}', finish="stop")

    out = ""
    async for chunk in gr.async_post_call_streaming_iterator_hook(None, _source(), data):
        for choice in chunk.choices:
            for tc in choice.delta.tool_calls or []:
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


@pytest.mark.asyncio
async def test_responses_mixed_input_list_is_fully_scanned():
    # An agentic Responses turn: a role message + a function_call_output + a bare
    # string. The old homogeneity check wrapped the whole list as one content and
    # left the non-message items (and their PII) raw. Every item must be scanned.
    gr = _guardrail()
    data = {
        "input": [
            {"role": "user", "content": "I am Marie Dupont"},
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "tool says carol.smith@example.net",
            },
            "also reach paul@acme.org",
        ]
    }
    data = await gr.async_pre_call_hook(None, None, data, "aresponses")
    serialized = json.dumps(data["input"])

    assert "Marie Dupont" not in serialized
    assert "carol.smith@example.net" not in serialized
    assert "paul@acme.org" not in serialized
    assert data["metadata"]["privaite_map"]
    # structure preserved: the tool item keeps its type and call_id
    assert data["input"][1]["type"] == "function_call_output"
    assert data["input"][1]["call_id"] == "c1"


@pytest.mark.asyncio
async def test_responses_input_text_content_parts_are_scanned():
    # A top-level list of content parts ({"type":"input_text","text":...}) is
    # neither a role-message list nor a string; its text must still be scrubbed.
    gr = _guardrail()
    parts = [{"type": "input_text", "text": "Hi Marie Dupont"}]
    data = {"input": parts}
    data = await gr.async_pre_call_hook(None, None, data, "aresponses")
    assert "Marie Dupont" not in data["input"][0]["text"]
    assert data["metadata"]["privaite_map"]


@pytest.mark.asyncio
async def test_responses_function_call_arguments_are_anonymized_in_body_snapshot():
    # LiteLLM's Responses request body is shallow-copied before hooks run. A
    # function_call input item must therefore be scrubbed in the shared list as
    # well as in data["input"], otherwise the provider snapshot leaks PII.
    gr = _guardrail()
    input_items = [
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "lookup",
            "arguments": json.dumps({"name": "Marie Dupont", "email": "marie.dupont@acme.com"}),
        }
    ]
    body = {"input": input_items}
    data = {"input": input_items, "proxy_server_request": {"body": body}}

    out = await gr.async_pre_call_hook(None, None, data, "aresponses")
    serialized = json.dumps(out["input"])
    snapshot = json.dumps(body["input"])

    assert "Marie Dupont" not in serialized
    assert "marie.dupont@acme.com" not in serialized
    assert "Marie Dupont" not in snapshot
    assert "marie.dupont@acme.com" not in snapshot
    assert out["input"][0]["type"] == "function_call"
    assert out["input"][0]["call_id"] == "c1"
    assert out["metadata"]["privaite_map"]


@pytest.mark.asyncio
async def test_responses_mixed_input_list_enforces_block_gate():
    module = _load()
    gr = module.PrivaiteGuardrail(
        guardrail_name="privaite",
        preset="light",
        languages="en",
        block_entities=["EMAIL_ADDRESS"],
    )
    from fastapi import HTTPException

    data = {"input": [{"type": "function_call_output", "output": "bob@leak.com"}]}
    with pytest.raises(HTTPException) as ei:
        await gr.async_pre_call_hook(None, None, data, "aresponses")
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_responses_function_call_arguments_enforce_block_gate():
    # The hard policy gate must apply to Responses function_call arguments, not
    # just tool-output text. A rejected hook returns no body to LiteLLM and never
    # leaves a reversible map in metadata.
    from fastapi import HTTPException

    module = _load()
    gr = module.PrivaiteGuardrail(
        guardrail_name="privaite",
        preset="light",
        languages="en",
        block_entities=["EMAIL_ADDRESS"],
    )
    data = {
        "input": [
            {
                "type": "function_call",
                "call_id": "c1",
                "arguments": json.dumps({"email": "bob@leak.com"}),
            }
        ],
        "metadata": {"privaite_map": {"<PERSON_1>": "attacker-controlled"}},
    }

    with pytest.raises(HTTPException) as ei:
        await gr.async_pre_call_hook(None, None, data, "aresponses")

    assert ei.value.status_code == 400
    assert "bob@leak.com" not in str(ei.value.detail)
    assert "privaite_map" not in data["metadata"]


@pytest.mark.asyncio
async def test_responses_custom_tool_call_input_and_list_output_are_scanned():
    # Codex's shell tool is a custom tool: the command travels in
    # custom_tool_call.input and the file contents come back as a
    # custom_tool_call_output whose `output` is a LIST of {type, text} parts.
    # Both were forwarded raw before the gateway parity sync; the list-output
    # shape is the exact leak class that let file contents through.
    gr = _guardrail()
    input_items = [
        {
            "type": "custom_tool_call",
            "call_id": "c1",
            "name": "shell",
            "input": "grep marie.dupont@acme.com notes.txt",
        },
        {
            "type": "custom_tool_call_output",
            "call_id": "c1",
            "output": [
                {"type": "input_text", "text": "I am Marie Dupont, email marie.dupont@acme.com"}
            ],
        },
    ]
    body = {"input": input_items}
    data = {"input": input_items, "proxy_server_request": {"body": body}}
    data = await gr.async_pre_call_hook(None, None, data, "aresponses")
    serialized = json.dumps(data["input"])

    assert "Marie Dupont" not in serialized
    assert "marie.dupont@acme.com" not in serialized
    # the body snapshot aliases the same list: anonymized there too
    assert "marie.dupont@acme.com" not in json.dumps(body["input"])
    # structure preserved: part type, call ids, item types
    assert data["input"][0]["call_id"] == "c1"
    assert data["input"][1]["output"][0]["type"] == "input_text"
    assert data["metadata"]["privaite_map"]


@pytest.mark.asyncio
async def test_responses_mcp_call_scrubs_both_arguments_and_output():
    # Regression: the old scan stopped at the FIRST text field it found, so an
    # mcp_call carrying both `arguments` and `output` leaked one of them.
    gr = _guardrail()
    data = {
        "input": [
            {
                "type": "mcp_call",
                "id": "m1",
                "server_label": "crm",
                "name": "lookup",
                "arguments": json.dumps({"email": "marie.dupont@acme.com"}),
                "output": "found carol.smith@example.net",
            }
        ]
    }
    data = await gr.async_pre_call_hook(None, None, data, "aresponses")
    item = data["input"][0]

    assert "marie.dupont@acme.com" not in item["arguments"]
    assert "carol.smith@example.net" not in item["output"]
    # arguments are JSON-parsed and walked, so they stay valid JSON
    assert "email" in json.loads(item["arguments"])


@pytest.mark.asyncio
async def test_responses_computer_call_action_scrubbed_screenshot_untouched():
    # A typed action carrier is scanned (action AND actions), while the paired
    # computer_call_output screenshot is binary: rewriting its base64 corrupts
    # it, so it must be relayed byte-for-byte.
    gr = _guardrail()
    screenshot_item = {
        "type": "computer_call_output",
        "call_id": "c1",
        "output": {
            "type": "computer_screenshot",
            "image_url": "data:image/png;base64,bytes-marie.dupont@acme.com-bytes",
        },
    }
    data = {
        "input": [
            {
                "type": "computer_call",
                "call_id": "c1",
                "action": {"type": "type", "text": "email marie.dupont@acme.com"},
                "actions": [{"type": "type", "text": "cc carol.smith@example.net"}],
                "status": "completed",
            },
            dict(screenshot_item),
        ]
    }
    data = await gr.async_pre_call_hook(None, None, data, "aresponses")

    assert "marie.dupont@acme.com" not in data["input"][0]["action"]["text"]
    assert "carol.smith@example.net" not in data["input"][0]["actions"][0]["text"]
    assert data["input"][1] == screenshot_item


@pytest.mark.asyncio
async def test_responses_typed_action_carriers_are_scanned():
    # The remaining typed carriers: shell command, patch diff, file-search
    # queries/results, interpreter code/logs. None of these were scanned before
    # the gateway parity sync.
    gr = _guardrail()
    data = {
        "input": [
            {
                "type": "local_shell_call",
                "call_id": "s1",
                "action": {"type": "exec", "command": ["echo", "marie.dupont@acme.com"]},
            },
            {
                "type": "apply_patch_call",
                "call_id": "p1",
                "operation": {
                    "type": "update_file",
                    "path": "notes.txt",
                    "diff": "+contact marie.dupont@acme.com",
                },
            },
            {
                "type": "file_search_call",
                "id": "f1",
                "queries": ["contract of marie.dupont@acme.com"],
                "results": [
                    {"file_id": "file-1", "score": 0.9, "text": "signed by carol.smith@example.net"}
                ],
            },
            {
                "type": "code_interpreter_call",
                "id": "ci1",
                "code": "send('marie.dupont@acme.com')",
                "outputs": [{"type": "logs", "logs": "emailed carol.smith@example.net"}],
            },
        ]
    }
    data = await gr.async_pre_call_hook(None, None, data, "aresponses")
    serialized = json.dumps(data["input"])

    assert "marie.dupont@acme.com" not in serialized
    assert "carol.smith@example.net" not in serialized
    # typed fields keep their structure around the scrubbed leaves
    assert data["input"][0]["action"]["type"] == "exec"
    assert data["input"][1]["operation"]["path"] == "notes.txt"
    assert data["input"][2]["results"][0]["file_id"] == "file-1"
    assert data["input"][3]["outputs"][0]["type"] == "logs"


@pytest.mark.asyncio
async def test_responses_prompt_variables_scrubbed_binary_variable_untouched():
    # prompt.variables carry user data (the template id/version do not); a
    # variables-only request must not slip past the pre-call early return. File
    # variables are binary payloads: relayed whole.
    gr = _guardrail()
    file_variable = {
        "type": "input_file",
        "file_data": "AAmarie.dupont@acme.comAA",
        "filename": "x.pdf",
    }
    prompt = {
        "id": "pmpt_1",
        "version": "2",
        "variables": {
            "customer": "our customer is Marie Dupont",
            "note": {"type": "input_text", "text": "email marie.dupont@acme.com"},
            "attachment": dict(file_variable),
        },
    }
    body = {"prompt": prompt}
    data = {"prompt": prompt, "proxy_server_request": {"body": body}}
    data = await gr.async_pre_call_hook(None, None, data, "aresponses")
    variables = data["prompt"]["variables"]

    assert "Marie Dupont" not in variables["customer"]
    assert "marie.dupont@acme.com" not in variables["note"]["text"]
    assert variables["attachment"] == file_variable
    assert data["prompt"]["id"] == "pmpt_1"
    # the snapshot aliases the same prompt dict, so it is anonymized too
    assert body["prompt"] is data["prompt"]
    assert "Marie Dupont" not in json.dumps(body["prompt"])
    assert data["metadata"]["privaite_map"]


@pytest.mark.asyncio
async def test_responses_opaque_and_binary_items_relayed_byte_for_byte():
    # Encrypted reasoning is validated by the provider and an image part is
    # base64: scrubbing either corrupts the item without removing anything a
    # text detector could find. The sibling text part is still scanned.
    gr = _guardrail()
    reasoning_item = {
        "type": "reasoning",
        "id": "rs1",
        "encrypted_content": "gAAAA-marie.dupont@acme.com-opaque",
        "summary": [],
    }
    image_part = {
        "type": "input_image",
        "image_url": "data:image/png;base64,marie.dupont@acme.com",
    }
    data = {
        "input": [
            dict(reasoning_item),
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [
                    {"type": "input_text", "text": "sent to carol.smith@example.net"},
                    dict(image_part),
                ],
            },
        ]
    }
    data = await gr.async_pre_call_hook(None, None, data, "aresponses")

    assert data["input"][0] == reasoning_item
    output = data["input"][1]["output"]
    assert "carol.smith@example.net" not in output[0]["text"]
    assert output[1] == image_part


@pytest.mark.asyncio
async def test_pre_call_string_prompt_still_early_returns(monkeypatch):
    # /v1/completions sends `prompt` as a string: that is not the Responses
    # prompt-variables surface, so the hook must keep returning early without
    # spinning up the engine.
    gr = _guardrail()

    async def _boom(_languages):
        raise AssertionError("engine must not be built for a string prompt")

    monkeypatch.setattr(gr, "_engine_for", _boom)
    data = {"prompt": "say hello"}
    out = await gr.async_pre_call_hook(None, None, data, "atext_completion")
    assert out is data
    assert "metadata" not in out


@pytest.mark.asyncio
async def test_responses_custom_tool_output_list_enforces_block_gate():
    # The hard policy gate must cover the newly scanned list-of-parts output
    # path too, not only string outputs.
    from fastapi import HTTPException

    module = _load()
    gr = module.PrivaiteGuardrail(
        guardrail_name="privaite",
        preset="light",
        languages="en",
        block_entities=["EMAIL_ADDRESS"],
    )
    data = {
        "input": [
            {
                "type": "custom_tool_call_output",
                "call_id": "c1",
                "output": [{"type": "input_text", "text": "bob@leak.com"}],
            }
        ]
    }
    with pytest.raises(HTTPException) as ei:
        await gr.async_pre_call_hook(None, None, data, "aresponses")
    assert ei.value.status_code == 400
    assert "bob@leak.com" not in str(ei.value.detail)


@pytest.mark.asyncio
async def test_pre_call_failure_clears_map_and_aborts_before_returning_data(monkeypatch):
    # A detector error has no allow-through path in the guardrail. The hook
    # propagates it before returning a request to LiteLLM and discards any
    # caller-supplied reversible map at the boundary.
    gr = _guardrail()

    class FailingEngine:
        async def process_request(self, messages):
            raise RuntimeError("detector unavailable")

    async def _failing_engine_for(_languages):
        return FailingEngine()

    monkeypatch.setattr(gr, "_engine_for", _failing_engine_for)
    messages = [{"role": "user", "content": "Marie Dupont"}]
    data = {
        "messages": messages,
        "metadata": {"privaite_map": {"<PERSON_1>": "attacker-controlled"}},
    }

    with pytest.raises(RuntimeError, match="detector unavailable"):
        await gr.async_pre_call_hook(None, None, data, "completion")

    assert messages[0]["content"] == "Marie Dupont"
    assert "privaite_map" not in data["metadata"]


@pytest.mark.asyncio
async def test_streaming_restores_reasoning_content():
    gr = _guardrail()
    data = {"messages": [{"role": "user", "content": "email marie.dupont@acme.com"}]}
    data = await gr.async_pre_call_hook(None, None, data, "completion")
    fakes = data["metadata"]["privaite_map"]
    email = next(f for f, o in fakes.items() if o == "marie.dupont@acme.com")

    def _chunk(reasoning, finish=None):
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    index=0,
                    delta=types.SimpleNamespace(
                        content=None,
                        tool_calls=None,
                        function_call=None,
                        reasoning_content=reasoning,
                    ),
                    finish_reason=finish,
                )
            ]
        )

    async def _source():
        yield _chunk("the user is " + email)
        yield _chunk(None, finish="stop")

    out = []
    async for chunk in gr.async_post_call_streaming_iterator_hook(None, _source(), data):
        for choice in chunk.choices:
            rc = getattr(choice.delta, "reasoning_content", None)
            if rc:
                out.append(rc)
    joined = "".join(out)
    assert "marie.dupont@acme.com" in joined
    assert email not in joined


@pytest.mark.asyncio
async def test_streaming_flushes_tool_tail_on_same_finish_chunk():
    gr = _guardrail()
    data = {"messages": [{"role": "user", "content": "email marie.dupont@acme.com"}]}
    data = await gr.async_pre_call_hook(None, None, data, "completion")
    fakes = data["metadata"]["privaite_map"]
    email = next(f for f, o in fakes.items() if o == "marie.dupont@acme.com")
    mid = len(email) // 2

    def _chunk(args, finish=None):
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    index=0,
                    delta=types.SimpleNamespace(
                        content=None,
                        tool_calls=[
                            types.SimpleNamespace(
                                index=0, function=types.SimpleNamespace(arguments=args)
                            )
                        ],
                        function_call=None,
                    ),
                    finish_reason=finish,
                )
            ]
        )

    async def _source():
        yield _chunk('{"e": "' + email[:mid])
        # last fragment rides on the finish chunk: the held tail must flush
        yield _chunk(email[mid:] + '"}', finish="tool_calls")

    out = ""
    async for chunk in gr.async_post_call_streaming_iterator_hook(None, _source(), data):
        for choice in chunk.choices:
            for tc in choice.delta.tool_calls or []:
                if tc.function and tc.function.arguments:
                    out += tc.function.arguments
    assert "marie.dupont@acme.com" in out
    assert email not in out


@pytest.mark.asyncio
async def test_post_call_failure_hook_pops_map():
    gr = _guardrail()
    rd = {"metadata": {"privaite_map": {"<PERSON_1>": "X"}, "other": 1}}
    assert await gr.async_post_call_failure_hook(rd, RuntimeError("x"), None) is None
    assert "privaite_map" not in rd["metadata"]
    assert rd["metadata"]["other"] == 1
    assert await gr.async_post_call_failure_hook({}, RuntimeError("x"), None) is None
    assert await gr.async_post_call_failure_hook(None, RuntimeError("x"), None) is None


def test_block_entities_parsed_from_string_and_list():
    module = _load()
    g1 = module.PrivaiteGuardrail(guardrail_name="p", block_entities="US_SSN, CREDIT_CARD")
    assert g1.block_entities == ["US_SSN", "CREDIT_CARD"]
    g2 = module.PrivaiteGuardrail(guardrail_name="p", block_entities=["EMAIL_ADDRESS"])
    assert g2.block_entities == ["EMAIL_ADDRESS"]
    assert module.PrivaiteGuardrail(guardrail_name="p").block_entities == []


@pytest.mark.asyncio
async def test_block_entities_rejects_with_400():
    from fastapi import HTTPException

    module = _load()
    gr = module.PrivaiteGuardrail(
        guardrail_name="privaite",
        preset="light",
        languages="en",
        block_entities=["EMAIL_ADDRESS"],
    )
    data = {"messages": [{"role": "user", "content": "reach me at bob@example.com"}]}
    with pytest.raises(HTTPException) as ei:
        await gr.async_pre_call_hook(None, None, data, "completion")

    assert ei.value.status_code == 400
    detail = ei.value.detail
    msg = detail["error"] if isinstance(detail, dict) else str(detail)
    assert "EMAIL_ADDRESS" in msg
    assert "bob@example.com" not in msg  # the value never leaks into the error


@pytest.mark.asyncio
async def test_block_entities_ignores_types_not_present():
    # a blocked type that is absent must not affect a normal request: the other
    # PII is still masked and the request goes through.
    module = _load()
    gr = module.PrivaiteGuardrail(
        guardrail_name="privaite",
        preset="light",
        languages="en",
        block_entities=["US_SSN"],
    )
    data = {
        "messages": [{"role": "user", "content": "I am Marie Dupont, email marie.dupont@acme.com"}]
    }
    out = await gr.async_pre_call_hook(None, None, data, "completion")
    serialized = json.dumps(out["messages"])
    assert "Marie Dupont" not in serialized
    assert "marie.dupont@acme.com" not in serialized


@pytest.mark.asyncio
async def test_block_entities_fails_closed_when_privaite_too_old(monkeypatch):
    # simulate an older privaite whose PIIConfig has no block_entities field:
    # extra="allow" would swallow it silently, so the guardrail must refuse.
    from privaite.config import schema

    module = _load()
    gr = module.PrivaiteGuardrail(
        guardrail_name="privaite",
        preset="light",
        languages="en",
        block_entities=["EMAIL_ADDRESS"],
    )
    reduced = {k: v for k, v in schema.PIIConfig.model_fields.items() if k != "block_entities"}
    monkeypatch.setattr(schema.PIIConfig, "model_fields", reduced)

    data = {"messages": [{"role": "user", "content": "bob@example.com"}]}
    with pytest.raises(RuntimeError, match="block_entities"):
        await gr.async_pre_call_hook(None, None, data, "completion")
