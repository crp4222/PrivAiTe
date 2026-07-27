from __future__ import annotations

import json
import logging
import os

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_gateway.conftest import BoomDetector, make_gateway_app


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _anthropic_body() -> dict:
    return {
        "model": "claude-test",
        "max_tokens": 128,
        "system": "You are an agent CLI helping Marie Dupont.",
        "tools": [
            {
                "name": "send_email",
                "description": "Send an email to Marie Dupont",
                "input_schema": {"type": "object"},
            }
        ],
        "messages": [
            {"role": "user", "content": "Contact Marie Dupont please"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Marie Dupont wants...", "signature": "sig"},
                    {"type": "text", "text": "Emailing marie@acme.com now"},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "send_email",
                        "input": {"to": "marie@acme.com", "count": 3},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "delivered to marie@acme.com"}],
                    }
                ],
            },
        ],
    }


@pytest.mark.asyncio
async def test_anthropic_request_scrubbed_system_tools_thinking_untouched(gateway_app):
    app, upstream = gateway_app
    body = _anthropic_body()
    async with _client(app) as client:
        resp = await client.post("/v1/messages", json=body)

    assert resp.status_code == 200
    sent = upstream.sent_json()

    # system and tools are the agent's own prompt/definitions: byte-for-byte.
    assert sent["system"] == body["system"]
    assert sent["tools"] == body["tools"]
    # Thinking blocks are never scrubbed.
    assert sent["messages"][1]["content"][0] == body["messages"][1]["content"][0]

    # User text, text blocks, tool_use input and tool_result content ARE scrubbed.
    assert sent["messages"][0]["content"] == "Contact <PERSON_1> please"
    assert sent["messages"][1]["content"][1]["text"] == "Emailing <EMAIL_ADDRESS_1> now"
    tool_input = sent["messages"][1]["content"][2]["input"]
    assert tool_input["to"] == "<EMAIL_ADDRESS_1>"
    assert tool_input["count"] == 3
    assert (
        sent["messages"][2]["content"][0]["content"][0]["text"] == "delivered to <EMAIL_ADDRESS_1>"
    )

    # No raw PII anywhere outside system/tools/thinking.
    forwarded_messages = json.dumps(
        [sent["messages"][0], sent["messages"][1]["content"][1:], sent["messages"][2]]
    )
    assert "Marie Dupont" not in forwarded_messages
    assert "marie@acme.com" not in forwarded_messages


@pytest.mark.asyncio
async def test_anthropic_server_and_mcp_tool_blocks_scrubbed(gateway_app):
    # Round-trip leak regression: restore rewrites every string leaf of the
    # response (thinking excepted), so on turn N the client receives
    # server_tool_use / mcp_tool_use input and mcp_tool_result content with
    # REAL values, and echoes them back on turn N+1. Scrub coverage must match
    # restore coverage or those echoes reach the provider raw.
    app, upstream = gateway_app
    thinking = {"type": "thinking", "thinking": "about Marie Dupont", "signature": "s"}
    body = {
        "model": "claude-test",
        "max_tokens": 64,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    thinking,
                    {
                        "type": "server_tool_use",
                        "id": "srvtoolu_1",
                        "name": "web_search",
                        "input": {"query": "who is Marie Dupont"},
                    },
                    {
                        "type": "mcp_tool_use",
                        "id": "mcptoolu_1",
                        "name": "lookup",
                        "server_name": "crm",
                        "input": {"email": "marie@acme.com"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "mcp_tool_result",
                        "tool_use_id": "mcptoolu_1",
                        "is_error": False,
                        "content": [{"type": "text", "text": "record of marie@acme.com"}],
                    }
                ],
            },
        ],
    }
    async with _client(app) as client:
        resp = await client.post("/v1/messages", json=body)

    assert resp.status_code == 200
    sent = upstream.sent_json()
    blocks = sent["messages"][0]["content"]
    # Thinking stays untouched, exactly as before.
    assert blocks[0] == thinking
    assert blocks[1]["input"] == {"query": "who is <PERSON_1>"}
    assert blocks[2]["input"] == {"email": "<EMAIL_ADDRESS_1>"}
    result = sent["messages"][1]["content"][0]
    assert result["content"][0]["text"] == "record of <EMAIL_ADDRESS_1>"
    assert result["is_error"] is False
    # The planted values reach the upstream nowhere outside the thinking block.
    forwarded = json.dumps([blocks[1], blocks[2], sent["messages"][1]])
    assert "Marie Dupont" not in forwarded
    assert "marie@acme.com" not in forwarded


@pytest.mark.asyncio
async def test_anthropic_document_and_search_result_plaintext_scrubbed(gateway_app):
    # document blocks with a text/content source and search_result blocks carry
    # plaintext; binary document sources (base64) must stay byte-for-byte.
    app, upstream = gateway_app
    base64_source = {
        "type": "base64",
        "media_type": "application/pdf",
        # The detector term INSIDE the payload proves the data is deliberately
        # not walked: rewriting base64 would corrupt the document.
        "data": "JVBERi-marie@acme.com-JVBERi",
    }
    body = {
        "model": "claude-test",
        "max_tokens": 64,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "text",
                            "media_type": "text/plain",
                            "data": "contact marie@acme.com",
                        },
                        "title": "notes on Marie Dupont",
                        "context": "sent by Marie Dupont",
                    },
                    {
                        "type": "document",
                        "source": {
                            "type": "content",
                            "content": [{"type": "text", "text": "call Marie Dupont"}],
                        },
                    },
                    {"type": "document", "source": base64_source, "title": "Marie Dupont file"},
                    {
                        "type": "search_result",
                        "source": "https://intranet.example/people/marie@acme.com",
                        "title": "Marie Dupont profile",
                        "content": [{"type": "text", "text": "reach marie@acme.com"}],
                    },
                ],
            }
        ],
    }
    async with _client(app) as client:
        resp = await client.post("/v1/messages", json=body)

    assert resp.status_code == 200
    blocks = upstream.sent_json()["messages"][0]["content"]
    assert blocks[0]["title"] == "notes on <PERSON_1>"
    assert blocks[0]["context"] == "sent by <PERSON_1>"
    assert blocks[0]["source"]["data"] == "contact <EMAIL_ADDRESS_1>"
    assert blocks[0]["source"]["media_type"] == "text/plain"
    assert blocks[1]["source"]["content"][0]["text"] == "call <PERSON_1>"
    # Binary source relayed whole; its sibling title is still plaintext.
    assert blocks[2]["source"] == base64_source
    assert blocks[2]["title"] == "<PERSON_1> file"
    assert blocks[3]["title"] == "<PERSON_1> profile"
    assert blocks[3]["source"] == "https://intranet.example/people/<EMAIL_ADDRESS_1>"
    assert blocks[3]["content"][0]["text"] == "reach <EMAIL_ADDRESS_1>"
    # Outside the binary payload, no planted value survives.
    forwarded = json.dumps(blocks[:2] + blocks[3:])
    assert "Marie Dupont" not in forwarded
    assert "marie@acme.com" not in forwarded


@pytest.mark.asyncio
async def test_anthropic_edge_block_shapes_pass_through_without_corruption(gateway_app):
    # Odd but legal shapes: blocks missing their payload field, a string
    # mcp_tool_result content, a media block, a document without a dict
    # source, and a non-dict part inside a result content list.
    app, upstream = gateway_app
    image_block = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AA-marie@acme.com-AA"},
    }
    body = {
        "model": "claude-test",
        "max_tokens": 64,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "noop"},
                    {"type": "tool_result", "tool_use_id": "t1"},
                    {
                        "type": "mcp_tool_result",
                        "tool_use_id": "m1",
                        "content": "found marie@acme.com",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": ["bare note for marie@acme.com", 7],
                    },
                    {"type": "tool_result", "tool_use_id": "t3", "content": 5},
                    image_block,
                    {"type": "document", "source": None, "title": "file of Marie Dupont"},
                ],
            }
        ],
    }
    async with _client(app) as client:
        resp = await client.post("/v1/messages", json=body)

    assert resp.status_code == 200
    blocks = upstream.sent_json()["messages"][0]["content"]
    assert blocks[0] == {"type": "tool_use", "id": "t1", "name": "noop"}
    assert blocks[1] == {"type": "tool_result", "tool_use_id": "t1"}
    assert blocks[2]["content"] == "found <EMAIL_ADDRESS_1>"
    # A bare string inside a result content list is user text: scanned, while
    # the non-text 7 passes through.
    assert blocks[3]["content"] == ["bare note for <EMAIL_ADDRESS_1>", 7]
    assert blocks[4] == {"type": "tool_result", "tool_use_id": "t3", "content": 5}
    assert blocks[5] == image_block
    assert blocks[6] == {"type": "document", "source": None, "title": "file of <PERSON_1>"}


@pytest.mark.asyncio
async def test_anthropic_unknown_block_types_scanned_opaque_fields_untouched(gateway_app):
    # Same class as the server_tool_use leak: a block type this build does not
    # know must not be relayed unscanned. Real shapes that were falling through
    # the fallback: web_search_tool_result (results carry a plaintext title and
    # url next to an opaque encrypted_content), code_execution_tool_result
    # (stdout/stderr), and any block the API gains after this release.
    app, upstream = gateway_app
    thinking = {"type": "thinking", "thinking": "about Marie Dupont", "signature": "sig-1"}
    redacted = {"type": "redacted_thinking", "data": "EroBCkYIBRgC-marie@acme.com-blob"}
    encrypted = "gAAAAAB-marie@acme.com-opaque-do-not-touch"
    body = {
        "model": "claude-test",
        "max_tokens": 64,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    thinking,
                    redacted,
                    {
                        "type": "web_search_tool_result",
                        "tool_use_id": "srvtoolu_1",
                        "content": [
                            {
                                "type": "web_search_result",
                                "url": "https://intranet.example/people/marie@acme.com",
                                "title": "profile of Marie Dupont",
                                "encrypted_content": encrypted,
                            }
                        ],
                    },
                    {
                        "type": "code_execution_tool_result",
                        "tool_use_id": "srvtoolu_2",
                        # A single nested block, not a list: the web_fetch and
                        # code_execution results wrap one result object.
                        "content": {
                            "type": "code_execution_result",
                            "stdout": "owner Marie Dupont <marie@acme.com>",
                            "stderr": "",
                            "return_code": 0,
                        },
                    },
                    {
                        "type": "future_block_from_a_later_api",
                        "id": "fb_1",
                        "text": "note about Marie Dupont",
                        "input": {"to": "marie@acme.com", "count": 3},
                    },
                ],
            }
        ],
    }
    async with _client(app) as client:
        resp = await client.post("/v1/messages", json=body)

    assert resp.status_code == 200
    blocks = upstream.sent_json()["messages"][0]["content"]
    # Thinking and redacted thinking (its opaque data blob) stay byte-for-byte.
    assert blocks[0] == thinking
    assert blocks[1] == redacted
    search_result = blocks[2]["content"][0]
    assert search_result["title"] == "profile of <PERSON_1>"
    assert search_result["url"] == "https://intranet.example/people/<EMAIL_ADDRESS_1>"
    # The encrypted payload is provider-validated: never read, never rewritten.
    assert search_result["encrypted_content"] == encrypted
    assert blocks[3]["content"]["stdout"] == "owner <PERSON_1> <<EMAIL_ADDRESS_1>>"
    assert blocks[3]["content"]["return_code"] == 0
    assert blocks[4]["text"] == "note about <PERSON_1>"
    assert blocks[4]["input"] == {"to": "<EMAIL_ADDRESS_1>", "count": 3}
    assert blocks[4]["id"] == "fb_1"
    # Outside thinking and the encrypted blob, no planted value reaches upstream.
    forwarded = json.dumps(blocks[2:]).replace(encrypted, "")
    assert "Marie Dupont" not in forwarded
    assert "marie@acme.com" not in forwarded


@pytest.mark.asyncio
async def test_anthropic_unknown_block_binary_source_relayed_byte_for_byte(gateway_app):
    # The generic scan must not walk a media payload: on Anthropic the binary
    # blob sits under `source.data`, so only a text/content source is scanned.
    app, upstream = gateway_app
    binary_source = {
        "type": "base64",
        "media_type": "image/webp",
        "data": "UklGR-marie@acme.com-UklGR",
    }
    body = {
        "model": "claude-test",
        "max_tokens": 64,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "future_media_block",
                        "source": binary_source,
                        "title": "photo of Marie Dupont",
                    },
                    {
                        "type": "future_document_block",
                        "source": {
                            "type": "text",
                            "media_type": "text/plain",
                            "data": "call marie@acme.com",
                        },
                    },
                    {"type": "future_url_block", "source": {"type": "url", "url": "https://x/y"}},
                ],
            }
        ],
    }
    async with _client(app) as client:
        resp = await client.post("/v1/messages", json=body)

    assert resp.status_code == 200
    blocks = upstream.sent_json()["messages"][0]["content"]
    assert blocks[0]["source"] == binary_source
    assert blocks[0]["title"] == "photo of <PERSON_1>"
    assert blocks[1]["source"]["data"] == "call <EMAIL_ADDRESS_1>"
    assert blocks[1]["source"]["media_type"] == "text/plain"
    # A pointer source is dereferenced by the provider: relayed as it came in.
    assert blocks[2] == body["messages"][0]["content"][2]


@pytest.mark.asyncio
async def test_count_tokens_scrubbed_and_query_relayed(gateway_app):
    app, upstream = gateway_app
    async with _client(app) as client:
        resp = await client.post(
            "/v1/messages/count_tokens?beta=true",
            json={
                "model": "claude-test",
                "messages": [{"role": "user", "content": "hi Marie Dupont"}],
            },
        )

    assert resp.status_code == 200
    assert upstream.request.url.path.endswith("/v1/messages/count_tokens")
    assert upstream.request.url.query == b"beta=true"
    assert upstream.sent_json()["messages"][0]["content"] == "hi <PERSON_1>"


@pytest.mark.asyncio
async def test_responses_request_scrubbed_instructions_untouched(gateway_app):
    app, upstream = gateway_app
    body = {
        "model": "gpt-test",
        "instructions": "You are Codex, assistant of Marie Dupont.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "email marie@acme.com"}],
            },
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "send_email",
                "arguments": json.dumps({"to": "marie@acme.com", "urgent": True}),
            },
            {"type": "function_call_output", "call_id": "c1", "output": "sent to marie@acme.com"},
            "note from Marie Dupont",
        ],
    }
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json=body)

    assert resp.status_code == 200
    sent = upstream.sent_json()

    assert sent["instructions"] == body["instructions"]
    assert sent["input"][0]["content"][0]["text"] == "email <EMAIL_ADDRESS_1>"
    args = json.loads(sent["input"][1]["arguments"])
    assert args == {"to": "<EMAIL_ADDRESS_1>", "urgent": True}
    assert sent["input"][2]["output"] == "sent to <EMAIL_ADDRESS_1>"
    assert sent["input"][3] == "note from <PERSON_1>"


@pytest.mark.asyncio
async def test_responses_string_input_scrubbed(gateway_app):
    app, upstream = gateway_app
    async with _client(app) as client:
        await client.post("/v1/responses", json={"model": "gpt-test", "input": "call Marie Dupont"})
    assert upstream.sent_json()["input"] == "call <PERSON_1>"


@pytest.mark.asyncio
async def test_responses_role_item_without_content_not_injected(gateway_app):
    # A role item can carry no content (Codex sends a `developer` item whose
    # payload is `tools`). The scrub must not invent a `content` key: the
    # provider rejects the whole request as an unknown parameter otherwise.
    app, upstream = gateway_app
    developer_item = {
        "type": "developer_instructions",
        "role": "developer",
        "tools": [{"type": "function", "name": "run", "description": "call Marie Dupont"}],
    }
    body = {
        "model": "gpt-test",
        "input": [
            developer_item,
            {"type": "message", "role": "user", "content": "hi Marie Dupont"},
        ],
    }
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json=body)

    assert resp.status_code == 200
    sent = upstream.sent_json()
    # First item passes through byte-for-byte: no injected content, and its
    # tool definition (not a scanned surface) is left intact.
    assert sent["input"][0] == developer_item
    assert "content" not in sent["input"][0]
    # The real user item is still scrubbed.
    assert sent["input"][1]["content"] == "hi <PERSON_1>"


@pytest.mark.asyncio
async def test_responses_tool_call_output_scrubbed_reasoning_untouched(gateway_app):
    # Codex reads files through its shell (a custom tool), so file contents come
    # back as custom_tool_call_output items whose `output` is a list of
    # {type, text} parts. That is where a real leak was found: the output was
    # not scanned. The custom_tool_call `input` (the command) and reasoning
    # blocks (opaque encrypted_content) are exercised in the same request.
    app, upstream = gateway_app
    reasoning_item = {
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "gAAAAAB-marie@acme.com-opaque-do-not-touch",
    }
    body = {
        "model": "gpt-test",
        "input": [
            {"type": "custom_tool_call", "call_id": "c1", "name": "shell", "input": "cat file"},
            {
                "type": "custom_tool_call_output",
                "call_id": "c1",
                "output": [{"type": "input_text", "text": "owner Marie Dupont, marie@acme.com"}],
            },
            reasoning_item,
        ],
    }
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json=body)

    assert resp.status_code == 200
    sent = upstream.sent_json()
    # The file contents that came back through the shell tool are scrubbed.
    assert sent["input"][1]["output"][0]["text"] == "owner <PERSON_1>, <EMAIL_ADDRESS_1>"
    # Reasoning is relayed byte-for-byte: its encrypted content is opaque and
    # must never be scanned or rewritten (doing so would corrupt it).
    assert sent["input"][2] == reasoning_item
    # No planted PII survives anywhere in the forwarded body.
    forwarded = json.dumps(sent["input"][:2])
    assert "Marie Dupont" not in forwarded
    assert "marie@acme.com" not in forwarded


async def _roundtrip_input(app, upstream, input_items: list) -> list:
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json={"model": "gpt-test", "input": input_items})
    assert resp.status_code == 200
    return upstream.sent_json()["input"]


@pytest.mark.asyncio
async def test_responses_codex_capture_shape_scrubbed_tools_item_untouched(gateway_app):
    # The exact item shapes a real Codex session sends (from the benchmark
    # capture): message items with a `phase` key, a custom_tool_call carrying
    # `status`, a custom_tool_call_output whose output is input_text parts, and
    # the additional_tools developer item (role + tools, no content).
    app, upstream = gateway_app
    tools_item = {
        "type": "additional_tools",
        "role": "developer",
        "tools": [{"type": "custom", "name": "shell", "description": "runs a command"}],
    }
    sent = await _roundtrip_input(
        app,
        upstream,
        [
            tools_item,
            {
                "type": "message",
                "role": "user",
                "phase": "input",
                "content": [{"type": "input_text", "text": "read the file of Marie Dupont"}],
            },
            {
                "type": "custom_tool_call",
                "call_id": "c1",
                "name": "shell",
                "status": "completed",
                "input": "grep marie@acme.com notes.txt",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "c1",
                "output": [{"type": "input_text", "text": "found marie@acme.com"}],
            },
        ],
    )
    # Tool definitions are not a scanned surface: byte-for-byte, no injected
    # content key on the role-bearing tools item.
    assert sent[0] == tools_item
    assert sent[1]["content"][0]["text"] == "read the file of <PERSON_1>"
    assert sent[1]["phase"] == "input"
    assert sent[2]["input"] == "grep <EMAIL_ADDRESS_1> notes.txt"
    assert sent[3]["output"][0]["text"] == "found <EMAIL_ADDRESS_1>"


@pytest.mark.asyncio
async def test_responses_computer_call_action_scrubbed_screenshot_untouched(gateway_app):
    app, upstream = gateway_app
    screenshot_item = {
        "type": "computer_call_output",
        "call_id": "c1",
        # The detector term INSIDE the payload proves the item is deliberately
        # not walked: rewriting base64 would corrupt the screenshot.
        "output": {
            "type": "computer_screenshot",
            "image_url": "data:image/png;base64,bytes-marie@acme.com-bytes",
        },
    }
    sent = await _roundtrip_input(
        app,
        upstream,
        [
            {
                "type": "computer_call",
                "call_id": "c1",
                "action": {"type": "type", "text": "Marie Dupont"},
                "actions": [
                    {"type": "keypress", "keys": ["ctrl", "v"]},
                    {"type": "type", "text": "marie@acme.com"},
                ],
                "pending_safety_checks": [],
                "status": "completed",
            },
            screenshot_item,
        ],
    )
    assert sent[0]["action"] == {"type": "type", "text": "<PERSON_1>"}
    assert sent[0]["actions"][0] == {"type": "keypress", "keys": ["ctrl", "v"]}
    assert sent[0]["actions"][1]["text"] == "<EMAIL_ADDRESS_1>"
    assert sent[1] == screenshot_item


@pytest.mark.asyncio
async def test_responses_shell_and_patch_calls_scrubbed(gateway_app):
    app, upstream = gateway_app
    sent = await _roundtrip_input(
        app,
        upstream,
        [
            {
                "type": "local_shell_call",
                "call_id": "s1",
                "action": {
                    "type": "exec",
                    "command": ["echo", "Marie Dupont"],
                    "env": {"OWNER": "marie@acme.com"},
                },
                "status": "completed",
            },
            {"type": "local_shell_call_output", "id": "s1", "output": "hello Marie Dupont"},
            {
                "type": "shell_call",
                "call_id": "s2",
                "action": {"commands": ["grep marie@acme.com notes.txt"], "timeout_ms": 1000},
            },
            {
                "type": "shell_call_output",
                "call_id": "s2",
                "output": [
                    {
                        "stdout": "notes.txt:marie@acme.com",
                        "stderr": "",
                        "outcome": {"type": "exit", "exit_code": 0},
                    }
                ],
            },
            {
                "type": "apply_patch_call",
                "call_id": "p1",
                "status": "completed",
                "operation": {
                    "type": "update_file",
                    "path": "notes.txt",
                    "diff": "+contact Marie Dupont",
                },
            },
            {
                "type": "apply_patch_call_output",
                "call_id": "p1",
                "status": "completed",
                "output": "updated the line naming Marie Dupont",
            },
        ],
    )
    assert sent[0]["action"]["command"] == ["echo", "<PERSON_1>"]
    assert sent[0]["action"]["env"] == {"OWNER": "<EMAIL_ADDRESS_1>"}
    assert sent[1]["output"] == "hello <PERSON_1>"
    assert sent[2]["action"]["commands"] == ["grep <EMAIL_ADDRESS_1> notes.txt"]
    assert sent[2]["action"]["timeout_ms"] == 1000
    assert sent[3]["output"][0]["stdout"] == "notes.txt:<EMAIL_ADDRESS_1>"
    assert sent[3]["output"][0]["outcome"] == {"type": "exit", "exit_code": 0}
    assert sent[4]["operation"]["diff"] == "+contact <PERSON_1>"
    assert sent[5]["output"] == "updated the line naming <PERSON_1>"


@pytest.mark.asyncio
async def test_responses_search_and_interpreter_calls_scrubbed(gateway_app):
    app, upstream = gateway_app
    image_output = {"type": "image", "url": "data:image/png;base64,marie@acme.com"}
    sent = await _roundtrip_input(
        app,
        upstream,
        [
            {
                "type": "web_search_call",
                "id": "w1",
                "status": "completed",
                "action": {"type": "search", "query": "who is Marie Dupont"},
            },
            {
                "type": "file_search_call",
                "id": "f1",
                "status": "completed",
                "queries": ["contract of Marie Dupont"],
                "results": [
                    {
                        "file_id": "file-1",
                        "filename": "contract.pdf",
                        "score": 0.9,
                        "text": "signed by marie@acme.com",
                        "attributes": {},
                    }
                ],
            },
            {
                "type": "code_interpreter_call",
                "id": "ci1",
                "container_id": "cont-1",
                "status": "completed",
                "code": "send('marie@acme.com')",
                "outputs": [{"type": "logs", "logs": "emailed Marie Dupont"}, image_output],
            },
        ],
    )
    assert sent[0]["action"]["query"] == "who is <PERSON_1>"
    assert sent[1]["queries"] == ["contract of <PERSON_1>"]
    assert sent[1]["results"][0]["text"] == "signed by <EMAIL_ADDRESS_1>"
    assert sent[2]["code"] == "send('<EMAIL_ADDRESS_1>')"
    assert sent[2]["outputs"][0]["logs"] == "emailed <PERSON_1>"
    # An interpreter image output is binary: relayed whole, never rewritten.
    assert sent[2]["outputs"][1] == image_output


@pytest.mark.asyncio
async def test_responses_mcp_and_program_items_scrub_every_text_field(gateway_app):
    # Regression: the old fallback stopped at the FIRST text field it found, so
    # an mcp_call with both `arguments` and `output` leaked one of them.
    app, upstream = gateway_app
    sent = await _roundtrip_input(
        app,
        upstream,
        [
            {
                "type": "mcp_call",
                "id": "m1",
                "server_label": "crm",
                "name": "lookup",
                "arguments": json.dumps({"who": "Marie Dupont"}),
                "output": "found marie@acme.com",
            },
            {
                "type": "mcp_approval_request",
                "id": "a1",
                "server_label": "crm",
                "name": "lookup",
                "arguments": json.dumps({"who": "Marie Dupont"}),
            },
            {
                "type": "mcp_approval_response",
                "approval_request_id": "a1",
                "approve": False,
                "reason": "never email marie@acme.com",
            },
            {"type": "program", "call_id": "pr1", "code": "email('marie@acme.com')"},
            {
                "type": "program_output",
                "call_id": "pr1",
                "status": "completed",
                "result": "emailed Marie Dupont",
            },
        ],
    )
    assert json.loads(sent[0]["arguments"]) == {"who": "<PERSON_1>"}
    assert sent[0]["output"] == "found <EMAIL_ADDRESS_1>"
    assert json.loads(sent[1]["arguments"]) == {"who": "<PERSON_1>"}
    assert sent[2]["reason"] == "never email <EMAIL_ADDRESS_1>"
    assert sent[3]["code"] == "email('<EMAIL_ADDRESS_1>')"
    assert sent[4]["result"] == "emailed <PERSON_1>"
    assert "Marie Dupont" not in json.dumps(sent)
    assert "marie@acme.com" not in json.dumps(sent)


@pytest.mark.asyncio
async def test_responses_opaque_items_relayed_byte_for_byte(gateway_app):
    # item_reference carries no content (it names a server-side item);
    # image_generation_call.result and compaction/reasoning encrypted_content
    # are opaque payloads the provider validates or decodes: rewriting them
    # corrupts the item, and their text can only be the provider's own echo.
    app, upstream = gateway_app
    items = [
        {"type": "item_reference", "id": "msg_prev"},
        {
            "type": "image_generation_call",
            "id": "ig1",
            "status": "completed",
            "result": "AAmarie@acme.comAA",
        },
        {"type": "compaction", "encrypted_content": "gAAAA-Marie Dupont-opaque"},
        {
            "type": "reasoning",
            "id": "rs1",
            "encrypted_content": "gAAAA-opaque",
            "summary": [{"type": "summary_text", "text": "about Marie Dupont"}],
        },
    ]
    sent = await _roundtrip_input(app, upstream, items)
    assert sent == items


@pytest.mark.asyncio
async def test_responses_output_image_part_untouched_refusal_scrubbed(gateway_app):
    app, upstream = gateway_app
    image_part = {"type": "input_image", "image_url": "data:image/png;base64,marie@acme.com"}
    sent = await _roundtrip_input(
        app,
        upstream,
        [
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [{"type": "input_text", "text": "sent to marie@acme.com"}, image_part],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "refusal", "refusal": "I will not email Marie Dupont"}],
            },
        ],
    )
    assert sent[0]["output"][0]["text"] == "sent to <EMAIL_ADDRESS_1>"
    assert sent[0]["output"][1] == image_part
    # A restored refusal echoed back on the next turn carries real values,
    # exactly like assistant text: it must be scrubbed.
    assert sent[1]["content"][0]["refusal"] == "I will not email <PERSON_1>"


@pytest.mark.asyncio
async def test_responses_edge_shapes_scrubbed_without_corruption(gateway_app):
    # Odd but legal shapes: a non-dict item, a null content on a role item, an
    # unknown item type whose content mixes bare strings and parts, and
    # function_call arguments that are not valid JSON (scanned as raw text).
    app, upstream = gateway_app
    sent = await _roundtrip_input(
        app,
        upstream,
        [
            42,
            {"role": "user", "content": None},
            {
                "type": "future_item",
                "content": [
                    "bare Marie Dupont string",
                    17,
                    {"type": "input_text", "text": "note marie@acme.com"},
                ],
            },
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "send",
                "arguments": "not json Marie Dupont",
            },
        ],
    )
    assert sent[0] == 42
    assert sent[1] == {"role": "user", "content": None}
    assert sent[2]["content"] == [
        "bare <PERSON_1> string",
        17,
        {"type": "input_text", "text": "note <EMAIL_ADDRESS_1>"},
    ]
    assert sent[3]["arguments"] == "not json <PERSON_1>"


@pytest.mark.asyncio
async def test_responses_prompt_variables_scrubbed(gateway_app):
    app, upstream = gateway_app
    file_variable = {"type": "input_file", "file_data": "AAmarie@acme.comAA", "filename": "x.pdf"}
    body = {
        "model": "gpt-test",
        "prompt": {
            "id": "pmpt_1",
            "version": "2",
            "variables": {
                "customer": "Marie Dupont",
                "note": {"type": "input_text", "text": "email marie@acme.com"},
                "attachment": file_variable,
            },
        },
        "input": "hello",
    }
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json=body)
    assert resp.status_code == 200
    sent = upstream.sent_json()
    assert sent["prompt"]["id"] == "pmpt_1"
    variables = sent["prompt"]["variables"]
    assert variables["customer"] == "<PERSON_1>"
    assert variables["note"]["text"] == "email <EMAIL_ADDRESS_1>"
    # File payloads are binary: relayed whole, never rewritten.
    assert variables["attachment"] == file_variable


@pytest.mark.asyncio
async def test_client_token_relayed_verbatim_and_privaite_key_not_required():
    os.environ["PRIVAITE_API_KEYS"] = "privaite-secret-key"
    try:
        app, upstream = make_gateway_app(auth_enabled=True)
        async with _client(app) as client:
            resp = await client.post(
                "/v1/messages",
                json={"model": "m", "messages": [{"role": "user", "content": "hello"}]},
                headers={
                    "Authorization": "Bearer sk-ant-oat-client-token",
                    "anthropic-version": "2023-06-01",
                },
            )
            assert resp.status_code == 200
            assert upstream.request.headers["authorization"] == "Bearer sk-ant-oat-client-token"
            assert upstream.request.headers["anthropic-version"] == "2023-06-01"

            # Other routes still require PrivAiTe's own key.
            other = await client.get(
                "/v1/models", headers={"Authorization": "Bearer sk-ant-oat-client-token"}
            )
            assert other.status_code == 401
    finally:
        os.environ.pop("PRIVAITE_API_KEYS", None)


@pytest.mark.asyncio
async def test_gateway_disabled_routes_absent():
    app, upstream = make_gateway_app(gateway_enabled=False)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/messages", json={"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        )
    assert resp.status_code == 404
    assert upstream.request is None


@pytest.mark.asyncio
async def test_scrub_failure_fails_closed_nothing_forwarded():
    app, upstream = make_gateway_app(detector=BoomDetector())
    async with _client(app) as client:
        resp = await client.post(
            "/v1/messages",
            json={"model": "m", "messages": [{"role": "user", "content": "Marie Dupont data"}]},
        )
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "pii_error"
    assert upstream.request is None


@pytest.mark.asyncio
async def test_header_hygiene_and_no_token_in_logs(gateway_app, caplog):
    app, upstream = gateway_app
    token = "Bearer sk-ant-supersecret-123456"
    with caplog.at_level(logging.DEBUG, logger="privaite"):
        async with _client(app) as client:
            resp = await client.post(
                "/v1/messages",
                json={"model": "m", "messages": [{"role": "user", "content": "hi Marie Dupont"}]},
                headers={
                    "Authorization": token,
                    "anthropic-beta": "oauth-2025-04-20",
                    "x-forwarded-for": "10.0.0.1",
                },
            )

    assert resp.status_code == 200
    # host is the upstream's, not the client's, and content-length matches the
    # scrubbed body actually sent (httpx derives both from the connection).
    assert upstream.request.headers["host"] == "api.anthropic.com"
    assert int(upstream.request.headers["content-length"]) == len(upstream.request.content)
    # Transparent relay: the client's own headers reach the upstream verbatim,
    # including the token (the upstream authenticates the user) and any
    # provider-selecting header. Some providers pick their request schema from a
    # header, so forwarding them is a correctness requirement, not a leak.
    assert upstream.request.headers["authorization"] == token
    assert upstream.request.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert upstream.request.headers["x-forwarded-for"] == "10.0.0.1"
    # The relayed token must never reach a log record.
    assert "sk-ant-supersecret-123456" not in caplog.text

    for record in caplog.records:
        assert "sk-ant-supersecret-123456" not in record.getMessage()
