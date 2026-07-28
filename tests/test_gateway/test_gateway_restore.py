from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_gateway.conftest import FakeDetector, make_gateway_app


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# Every request below mentions Marie Dupont so the scrub pass builds the
# reversible mapping ("Marie Dupont" -> "<PERSON_1>") that the response echoes.
_ANTHROPIC_REQUEST = {
    "model": "claude-test",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "Contact Marie Dupont"}],
}

_RESPONSES_REQUEST = {"model": "gpt-test", "input": "Contact Marie Dupont"}


@pytest.mark.asyncio
async def test_anthropic_non_streaming_restore_skips_thinking(gateway_app):
    app, upstream = gateway_app
    upstream.set_json(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "about <PERSON_1>", "signature": "sig"},
                {"type": "text", "text": "I contacted <PERSON_1>."},
                {"type": "tool_use", "id": "t1", "name": "send", "input": {"name": "<PERSON_1>"}},
            ],
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
    )
    async with _client(app) as client:
        resp = await client.post("/v1/messages", json=_ANTHROPIC_REQUEST)

    assert resp.status_code == 200
    body = resp.json()
    # Thinking blocks pass through unmodified; everything else is restored.
    assert body["content"][0]["thinking"] == "about <PERSON_1>"
    assert body["content"][1]["text"] == "I contacted Marie Dupont."
    assert body["content"][2]["input"]["name"] == "Marie Dupont"


@pytest.mark.asyncio
async def test_anthropic_non_streaming_restores_server_and_mcp_tool_blocks(gateway_app):
    # The premise of the round-trip scrub coverage: these blocks ARE restored
    # on the way back, so the client history holds real values inside them.
    app, upstream = gateway_app
    upstream.set_json(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "server_tool_use",
                    "id": "srvtoolu_1",
                    "name": "web_search",
                    "input": {"query": "who is <PERSON_1>"},
                },
                {
                    "type": "mcp_tool_use",
                    "id": "mcptoolu_1",
                    "name": "lookup",
                    "server_name": "crm",
                    "input": {"who": "<PERSON_1>"},
                },
            ],
        }
    )
    async with _client(app) as client:
        resp = await client.post("/v1/messages", json=_ANTHROPIC_REQUEST)

    assert resp.status_code == 200
    body = resp.json()
    assert body["content"][0]["input"]["query"] == "who is Marie Dupont"
    assert body["content"][1]["input"]["who"] == "Marie Dupont"


@pytest.mark.asyncio
async def test_responses_non_streaming_restore(gateway_app):
    app, upstream = gateway_app
    upstream.set_json(
        {
            "id": "resp_1",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Emailed <PERSON_1>."}],
                },
                {"type": "function_call", "arguments": json.dumps({"who": "<PERSON_1>"})},
            ],
        }
    )
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json=_RESPONSES_REQUEST)

    assert resp.status_code == 200
    body = resp.json()
    assert body["output"][0]["content"][0]["text"] == "Emailed Marie Dupont."
    assert "Marie Dupont" in body["output"][1]["arguments"]


@pytest.mark.asyncio
async def test_anthropic_streaming_restores_placeholder_split_across_deltas(gateway_app):
    app, upstream = gateway_app
    upstream.set_sse(
        [
            ("message_start", {"type": "message_start", "message": {"id": "msg_1"}}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hello <PERS"},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "ON_1>, bye"},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "thinking_delta", "thinking": "about <PERSON_1>"},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_stop", {"type": "message_stop"}),
        ]
    )
    async with _client(app) as client:
        resp = await client.post("/v1/messages", json=_ANTHROPIC_REQUEST)

    assert resp.status_code == 200
    stream = resp.text

    # The split placeholder was reassembled and restored in the visible text.
    texts = _collect_anthropic_text(stream)
    assert "Hello Marie Dupont, bye" in texts
    # SSE framing survives, the finish events are intact, no duplicate stop.
    assert "event: content_block_delta" in stream
    assert stream.count('"type": "message_stop"') == 1
    # The thinking delta was left verbatim (placeholder NOT restored there).
    assert "about <PERSON_1>" in stream
    # No placeholder leaked into a restored text delta.
    assert "<PERS" not in texts


@pytest.mark.asyncio
async def test_anthropic_streaming_flushes_holdback_on_block_stop(gateway_app):
    app, upstream = gateway_app
    # The stream ends mid-placeholder: the held-back partial must be flushed
    # (verbatim, since it never completed a fake) before the stop events.
    upstream.set_sse(
        [
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "tail <PERS"},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_stop", {"type": "message_stop"}),
        ]
    )
    async with _client(app) as client:
        resp = await client.post("/v1/messages", json=_ANTHROPIC_REQUEST)

    texts = _collect_anthropic_text(resp.text)
    assert texts == "tail <PERS"
    # The flush event lands BEFORE the block stop.
    assert resp.text.find("<PERS") < resp.text.find("content_block_stop")


@pytest.mark.asyncio
async def test_responses_streaming_restores_placeholder_split_across_deltas(gateway_app):
    app, upstream = gateway_app
    upstream.set_sse(
        [
            ("response.created", {"type": "response.created", "response": {"id": "r1"}}),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "Hi <PERS",
                },
            ),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "ON_1>!",
                },
            ),
            (
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "text": "Hi <PERSON_1>!",
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "Hi <PERSON_1>!"}],
                            }
                        ]
                    },
                },
            ),
        ]
    )
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json=_RESPONSES_REQUEST)

    assert resp.status_code == 200
    stream = resp.text

    deltas = _collect_responses_deltas(stream)
    assert "Hi Marie Dupont!" in deltas
    assert "<PERS" not in deltas
    # The complete-value events are restored whole.
    assert '"text": "Hi Marie Dupont!"' in stream
    assert "<PERSON_1>" not in stream


@pytest.mark.asyncio
async def test_responses_streaming_custom_tool_call_input_restored(gateway_app):
    # Codex's shell tool is a custom tool: its streamed command rides
    # response.custom_tool_call_input.delta, not function_call_arguments.
    # A placeholder split across two deltas must reassemble through the
    # holdback buffer, and the .done event carries the complete value.
    app, upstream = gateway_app
    upstream.set_sse(
        [
            (
                "response.custom_tool_call_input.delta",
                {
                    "type": "response.custom_tool_call_input.delta",
                    "item_id": "ct1",
                    "delta": "grep <PERS",
                },
            ),
            (
                "response.custom_tool_call_input.delta",
                {
                    "type": "response.custom_tool_call_input.delta",
                    "item_id": "ct1",
                    "delta": "ON_1> notes",
                },
            ),
            (
                "response.custom_tool_call_input.done",
                {
                    "type": "response.custom_tool_call_input.done",
                    "item_id": "ct1",
                    "input": "grep <PERSON_1> notes",
                },
            ),
            ("response.completed", {"type": "response.completed"}),
        ]
    )
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json={**_RESPONSES_REQUEST, "stream": True})

    assert resp.status_code == 200
    events = _parse_stream(resp.text)
    deltas = "".join(
        e["delta"] for e in events if e.get("type") == "response.custom_tool_call_input.delta"
    )
    assert deltas == "grep Marie Dupont notes"
    done = [e for e in events if e.get("type") == "response.custom_tool_call_input.done"]
    assert done[0]["input"] == "grep Marie Dupont notes"
    assert "<PERS" not in deltas


@pytest.mark.asyncio
async def test_responses_streaming_flushes_heldback_tool_input_on_completed(gateway_app):
    # The stream ends with a partial placeholder held back in a
    # custom_tool_call_input channel: it must be flushed as a synthetic delta
    # (verbatim, it never completed a fake) before response.completed.
    app, upstream = gateway_app
    upstream.set_sse(
        [
            (
                "response.custom_tool_call_input.delta",
                {
                    "type": "response.custom_tool_call_input.delta",
                    "item_id": "ct1",
                    "delta": "tail <PERS",
                },
            ),
            ("response.completed", {"type": "response.completed"}),
        ]
    )
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json={**_RESPONSES_REQUEST, "stream": True})

    events = _parse_stream(resp.text)
    deltas = [e for e in events if e["type"] == "response.custom_tool_call_input.delta"]
    assert "".join(e["delta"] for e in deltas) == "tail <PERS"
    # The synthetic flush delta keeps the channel's item_id and lands before
    # the completed event.
    assert deltas[-1]["item_id"] == "ct1"
    assert resp.text.find("<PERS") < resp.text.find("response.completed")


@pytest.mark.asyncio
async def test_responses_streaming_reasoning_never_restored(gateway_app):
    # Reasoning is verbatim end to end: summary deltas AND dones, and the
    # encrypted_content inside output_item/completed payloads. Rewriting the
    # encrypted blob would corrupt a provider-validated payload.
    app, upstream = gateway_app
    reasoning_item = {
        "type": "reasoning",
        "id": "rs1",
        "encrypted_content": "enc-<PERSON_1>-blob",
        "summary": [{"type": "summary_text", "text": "about <PERSON_1>"}],
    }
    upstream.set_sse(
        [
            (
                "response.reasoning_summary_text.delta",
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": "rs1",
                    "delta": "about <PERSON_1>",
                },
            ),
            (
                "response.reasoning_summary_text.done",
                {
                    "type": "response.reasoning_summary_text.done",
                    "item_id": "rs1",
                    "text": "about <PERSON_1>",
                },
            ),
            (
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": 0, "item": reasoning_item},
            ),
            (
                "response.output_text.delta",
                {"type": "response.output_text.delta", "item_id": "m1", "delta": "Hi <PERSON_1>!"},
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "output": [
                            reasoning_item,
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "Hi <PERSON_1>!"}],
                            },
                        ]
                    },
                },
            ),
        ]
    )
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json={**_RESPONSES_REQUEST, "stream": True})

    events = _parse_stream(resp.text)
    by_type = {e["type"]: e for e in events}
    # Reasoning summary events verbatim (placeholder NOT restored).
    assert by_type["response.reasoning_summary_text.delta"]["delta"] == "about <PERSON_1>"
    assert by_type["response.reasoning_summary_text.done"]["text"] == "about <PERSON_1>"
    # The reasoning item is skipped inside whole-payload restores.
    assert by_type["response.output_item.done"]["item"] == reasoning_item
    completed_output = by_type["response.completed"]["response"]["output"]
    assert completed_output[0] == reasoning_item
    # Everything else IS restored.
    assert by_type["response.output_text.delta"]["delta"] == "Hi Marie Dupont!"
    assert completed_output[1]["content"][0]["text"] == "Hi Marie Dupont!"


@pytest.mark.asyncio
async def test_responses_streaming_refusal_restored_binary_events_verbatim(gateway_app):
    app, upstream = gateway_app
    upstream.set_sse(
        [
            (
                "response.refusal.delta",
                {"type": "response.refusal.delta", "item_id": "m1", "delta": "no <PERS"},
            ),
            (
                "response.refusal.delta",
                {"type": "response.refusal.delta", "item_id": "m1", "delta": "ON_1> today"},
            ),
            (
                "response.audio.delta",
                {"type": "response.audio.delta", "delta": "AAAA<PERSON_1>AAAA"},
            ),
            (
                "response.image_generation_call.partial_image",
                {
                    "type": "response.image_generation_call.partial_image",
                    "item_id": "ig1",
                    "partial_image_b64": "BBBB<PERSON_1>BBBB",
                },
            ),
            ("response.completed", {"type": "response.completed"}),
        ]
    )
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json={**_RESPONSES_REQUEST, "stream": True})

    events = _parse_stream(resp.text)
    refusal = "".join(e["delta"] for e in events if e["type"] == "response.refusal.delta")
    assert refusal == "no Marie Dupont today"
    # Base64 carriers are never rewritten, even when a placeholder-shaped byte
    # run appears inside them.
    audio = [e for e in events if e["type"] == "response.audio.delta"]
    assert audio[0]["delta"] == "AAAA<PERSON_1>AAAA"
    partial = [e for e in events if e["type"] == "response.image_generation_call.partial_image"]
    assert partial[0]["partial_image_b64"] == "BBBB<PERSON_1>BBBB"


@pytest.mark.asyncio
async def test_responses_non_streaming_restore_skips_reasoning(gateway_app):
    app, upstream = gateway_app
    reasoning_item = {
        "type": "reasoning",
        "id": "rs1",
        "encrypted_content": "enc-<PERSON_1>-blob",
        "summary": [{"type": "summary_text", "text": "about <PERSON_1>"}],
    }
    upstream.set_json(
        {
            "id": "resp_1",
            "output": [
                reasoning_item,
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "Hi <PERSON_1>."},
                        {"type": "refusal", "refusal": "not for <PERSON_1>"},
                    ],
                },
            ],
        }
    )
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json=_RESPONSES_REQUEST)

    assert resp.status_code == 200
    body = resp.json()
    assert body["output"][0] == reasoning_item
    assert body["output"][1]["content"][0]["text"] == "Hi Marie Dupont."
    assert body["output"][1]["content"][1]["refusal"] == "not for Marie Dupont"


@pytest.mark.asyncio
async def test_pii_disabled_is_pure_passthrough():
    app, upstream = make_gateway_app(pii_enabled=False)
    upstream.set_json({"id": "msg_1", "content": [{"type": "text", "text": "raw"}]})
    raw_body = {"model": "m", "messages": [{"role": "user", "content": "Marie Dupont raw"}]}
    async with _client(app) as client:
        resp = await client.post("/v1/messages", json=raw_body)

    assert resp.status_code == 200
    # The body was relayed byte-for-byte, no scrub, no restore.
    assert json.loads(upstream.request.content) == raw_body
    assert resp.json() == {"id": "msg_1", "content": [{"type": "text", "text": "raw"}]}


@pytest.mark.asyncio
async def test_streaming_detected_from_request_flag_without_content_type(gateway_app):
    # The ChatGPT Codex backend streams SSE with no text/event-stream
    # content-type. Routing that by content-type alone would send an SSE body
    # into the JSON path and relay it unrestored. The client's own stream: true
    # must drive the SSE restore path instead.
    app, upstream = gateway_app
    upstream.set_sse(
        [
            (
                "response.output_text.delta",
                {"type": "response.output_text.delta", "item_id": "m", "delta": "Hi <PERS"},
            ),
            (
                "response.output_text.delta",
                {"type": "response.output_text.delta", "item_id": "m", "delta": "ON_1>!"},
            ),
            ("response.completed", {"type": "response.completed"}),
        ]
    )
    upstream.headers = {}  # no content-type, exactly like the Codex backend

    async with _client(app) as client:
        resp = await client.post("/v1/responses", json={**_RESPONSES_REQUEST, "stream": True})

    assert resp.status_code == 200
    deltas = _collect_responses_deltas(resp.text)
    assert "Hi Marie Dupont!" in deltas
    assert "<PERS" not in deltas


# An original with a quote, a backslash and a newline (a multi-line address is
# the realistic case): spliced raw into streamed JSON fragments, any one of
# them makes the client's accumulated tool arguments unparseable.
_SPECIALS_ADDRESS = '12 "B" Street\\Unit 7\nParis'


def _specials_app():
    return make_gateway_app(detector=FakeDetector({_SPECIALS_ADDRESS: "LOCATION"}))


_SPECIALS_ANTHROPIC_REQUEST = {
    "model": "claude-test",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": f"Ship to {_SPECIALS_ADDRESS} please"}],
}


@pytest.mark.asyncio
async def test_anthropic_streamed_tool_json_stays_valid_with_special_chars():
    app, upstream = _specials_app()
    # Non-streaming reference: restore happens on the parsed input tree.
    upstream.set_json(
        {
            "id": "msg_1",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "ship",
                    "input": {"address": "<LOCATION_1>", "urgent": True},
                }
            ],
        }
    )
    async with _client(app) as client:
        ref = await client.post("/v1/messages", json=_SPECIALS_ANTHROPIC_REQUEST)
    expected = ref.json()["content"][0]["input"]
    assert expected == {"address": _SPECIALS_ADDRESS, "urgent": True}

    upstream.set_sse(
        [
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "t1", "name": "ship", "input": {}},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"address": "<LOCA'},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": 'TION_1>", "urgent": true}',
                    },
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_stop", {"type": "message_stop"}),
        ]
    )
    async with _client(app) as client:
        resp = await client.post(
            "/v1/messages", json={**_SPECIALS_ANTHROPIC_REQUEST, "stream": True}
        )

    events = _parse_stream(resp.text)
    fragments = "".join(
        e["delta"]["partial_json"]
        for e in events
        if e.get("type") == "content_block_delta"
        and e.get("delta", {}).get("type") == "input_json_delta"
    )
    # The reassembled streamed arguments are valid JSON and parse to the SAME
    # object as the non-streaming path, raw quote/backslash/newline included.
    assert json.loads(fragments) == expected


@pytest.mark.asyncio
async def test_responses_streamed_arguments_stay_valid_with_special_chars():
    app, upstream = _specials_app()
    request = {"model": "gpt-test", "input": f"Ship to {_SPECIALS_ADDRESS} please"}
    placeholder_args = '{"address": "<LOCATION_1>", "urgent": true}'

    upstream.set_json(
        {
            "id": "resp_1",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "ship",
                    "arguments": placeholder_args,
                }
            ],
        }
    )
    async with _client(app) as client:
        ref = await client.post("/v1/responses", json=request)
    expected = json.loads(ref.json()["output"][0]["arguments"])
    assert expected == {"address": _SPECIALS_ADDRESS, "urgent": True}

    upstream.set_sse(
        [
            (
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc1",
                    "delta": '{"address": "<LOCA',
                },
            ),
            (
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc1",
                    "delta": 'TION_1>", "urgent": true}',
                },
            ),
            (
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": "fc1",
                    "arguments": placeholder_args,
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "c1",
                                "name": "ship",
                                "arguments": placeholder_args,
                            }
                        ]
                    },
                },
            ),
        ]
    )
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json={**request, "stream": True})

    events = _parse_stream(resp.text)
    fragments = "".join(
        e["delta"] for e in events if e["type"] == "response.function_call_arguments.delta"
    )
    assert json.loads(fragments) == expected
    done = [e for e in events if e["type"] == "response.function_call_arguments.done"]
    assert json.loads(done[0]["arguments"]) == expected
    completed = [e for e in events if e["type"] == "response.completed"]
    assert json.loads(completed[0]["response"]["output"][0]["arguments"]) == expected


@pytest.mark.asyncio
async def test_responses_plain_text_channel_restores_raw_not_json_escaped():
    # custom_tool_call_input accumulates to a plain string, not JSON source
    # text: the restored original must arrive raw (real newline, unescaped
    # quote and backslash), with only the event's own JSON framing escaping it.
    app, upstream = _specials_app()
    request = {"model": "gpt-test", "input": f"Ship to {_SPECIALS_ADDRESS} please"}
    upstream.set_sse(
        [
            (
                "response.custom_tool_call_input.delta",
                {
                    "type": "response.custom_tool_call_input.delta",
                    "item_id": "ct1",
                    "delta": "mkdir <LOCA",
                },
            ),
            (
                "response.custom_tool_call_input.delta",
                {
                    "type": "response.custom_tool_call_input.delta",
                    "item_id": "ct1",
                    "delta": "TION_1> now",
                },
            ),
            ("response.completed", {"type": "response.completed"}),
        ]
    )
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json={**request, "stream": True})

    events = _parse_stream(resp.text)
    deltas = "".join(
        e["delta"] for e in events if e["type"] == "response.custom_tool_call_input.delta"
    )
    assert deltas == f"mkdir {_SPECIALS_ADDRESS} now"


@pytest.mark.asyncio
async def test_responses_arguments_without_placeholder_kept_byte_identical(gateway_app):
    # The JSON-aware arguments restore must not gratuitously re-encode: an
    # arguments string containing no placeholder keeps its exact bytes
    # (whitespace, key order, escapes).
    app, upstream = gateway_app
    compact = '{"b":1,"a":"x\\u00e9"}'
    upstream.set_json({"id": "r", "output": [{"type": "function_call", "arguments": compact}]})
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json=_RESPONSES_REQUEST)
    assert resp.json()["output"][0]["arguments"] == compact


@pytest.mark.asyncio
async def test_responses_non_json_arguments_still_restored_as_text(gateway_app):
    # Invalid-JSON arguments keep the previous behavior: plain text restore.
    app, upstream = gateway_app
    upstream.set_json(
        {"id": "r", "output": [{"type": "function_call", "arguments": "not json <PERSON_1>"}]}
    )
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json=_RESPONSES_REQUEST)
    assert resp.json()["output"][0]["arguments"] == "not json Marie Dupont"


def _collect_anthropic_text(stream: str) -> str:
    out = []
    for line in stream.splitlines():
        if not line.startswith("data:"):
            continue
        data = json.loads(line[5:])
        delta = data.get("delta") or {}
        if data.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
            out.append(delta.get("text", ""))
    return "".join(out)


def _parse_stream(stream: str) -> list[dict]:
    out = []
    for line in stream.splitlines():
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            out.append(json.loads(line[5:]))
    return out


def _collect_responses_deltas(stream: str) -> str:
    out = []
    for line in stream.splitlines():
        if not line.startswith("data:"):
            continue
        data = json.loads(line[5:])
        if data.get("type") == "response.output_text.delta":
            out.append(data.get("delta", ""))
    return "".join(out)


@pytest.mark.asyncio
async def test_dict_valued_type_key_does_not_abort_the_restore(gateway_app):
    # A tool schema can carry a property literally named "type" whose value is an
    # object. `type` is only a block discriminator when it is a string, so the
    # skip check must not test an unhashable value for membership: doing so
    # raised TypeError and aborted the whole restore pass, which reached Codex
    # through its own stock tool schema.
    app, upstream = gateway_app
    upstream.set_json(
        {
            "id": "r",
            "tools": [{"input_schema": {"properties": {"type": {"type": "object"}}}}],
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "<PERSON_1>"}]}
            ],
        }
    )
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json=_RESPONSES_REQUEST)
    assert resp.status_code == 200
    body = resp.json()
    assert body["output"][0]["content"][0]["text"] == "Marie Dupont"
    # the schema itself is walked and relayed unchanged
    assert body["tools"][0]["input_schema"]["properties"]["type"] == {"type": "object"}


@pytest.mark.asyncio
async def test_unexpected_restore_failure_withholds_the_body(gateway_app, monkeypatch):
    # An unexpected restore error must keep the documented pii_error shape and
    # withhold the body, not surface as a bare 500 with a traceback: the
    # non-streaming path used to catch only PIIProcessingError.
    app, upstream = gateway_app
    upstream.set_json({"id": "r", "output": [{"type": "message", "content": []}]})

    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("privaite.gateway.routes.restore_tree", boom)
    async with _client(app) as client:
        resp = await client.post("/v1/responses", json=_RESPONSES_REQUEST)
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "pii_error"
