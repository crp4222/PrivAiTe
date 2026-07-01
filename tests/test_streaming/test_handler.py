from __future__ import annotations

import json

import pytest

from privaite.config.schema import DeanonymizationConfig
from privaite.pii.mapping import PIIMapping
from privaite.streaming.handler import StreamingHandler


class FakeChunk:
    def __init__(self, content=None, finish_reason=None, model="m") -> None:
        delta = {"content": content} if content is not None else {}
        self._data = {
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    def model_dump(self) -> dict:
        return self._data


class FakeDeltaChunk:
    def __init__(self, delta, finish_reason=None, model="m") -> None:
        self._data = {
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    def model_dump(self) -> dict:
        return self._data


def _collect_tool_args(events: list[str]) -> dict[int, str]:
    args: dict[int, str] = {}
    for event in events:
        for line in event.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: ") :]
            if payload.strip() == "[DONE]":
                continue
            for choice in json.loads(payload).get("choices", []):
                for call in choice.get("delta", {}).get("tool_calls") or []:
                    idx = call.get("index", 0)
                    frag = (call.get("function") or {}).get("arguments") or ""
                    args[idx] = args.get(idx, "") + frag
    return args


def _collect_function_call_args(events: list[str]) -> str:
    out: list[str] = []
    for event in events:
        for line in event.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: ") :]
            if payload.strip() == "[DONE]":
                continue
            for choice in json.loads(payload).get("choices", []):
                fc = choice.get("delta", {}).get("function_call")
                if fc and fc.get("arguments"):
                    out.append(fc["arguments"])
    return "".join(out)


async def _stream(chunks):
    for chunk in chunks:
        yield chunk


def _collect_content(events: list[str]) -> str:
    out: list[str] = []
    for event in events:
        for line in event.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: ") :]
            if payload.strip() == "[DONE]":
                continue
            for choice in json.loads(payload).get("choices", []):
                content = choice.get("delta", {}).get("content")
                if content:
                    out.append(content)
    return "".join(out)


@pytest.mark.asyncio
async def test_deanonymizes_placeholder_split_across_chunks():
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "Zorp Glax", "PERSON")  # original -> fake
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)
    # the model echoes the fake, split across chunk boundaries
    chunks = [
        FakeChunk("Hello Zorp"),
        FakeChunk(" Gl"),
        FakeChunk("ax, bye"),
        FakeChunk("", finish_reason="stop"),
    ]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    full = _collect_content(events)

    assert "Marie Dupont" in full
    assert "Zorp" not in full
    assert events[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_passes_through_without_mapping():
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)
    chunks = [FakeChunk("plain text"), FakeChunk("", finish_reason="stop")]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), None, cfg)]

    assert "plain text" in _collect_content(events)


@pytest.mark.asyncio
async def test_deanonymizes_tool_call_arguments_split_across_chunks():
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "Zorp Glax", "PERSON")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)
    # the fake value is split across two argument deltas
    chunks = [
        FakeDeltaChunk({"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "save_contact", "arguments": ""}}
        ]}),
        FakeDeltaChunk({"tool_calls": [
            {"index": 0, "function": {"arguments": '{"name": "Zorp Gl'}}
        ]}),
        FakeDeltaChunk({"tool_calls": [
            {"index": 0, "function": {"arguments": 'ax"}'}}
        ]}),
        FakeChunk("", finish_reason="tool_calls"),
    ]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    args = _collect_tool_args(events)

    assert args[0] == '{"name": "Marie Dupont"}'
    assert "Zorp" not in args[0]
    assert events[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_deanonymizes_legacy_function_call_arguments():
    mapping = PIIMapping()
    mapping.add("marie@acme.com", "fake@x.io", "EMAIL_ADDRESS")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)
    chunks = [
        FakeDeltaChunk({"function_call": {"name": "send", "arguments": '{"to": "fa'}}),
        FakeDeltaChunk({"function_call": {"arguments": 'ke@x.io"}'}}),
        FakeChunk("", finish_reason="function_call"),
    ]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    restored = _collect_function_call_args(events)

    assert restored == '{"to": "marie@acme.com"}'
    assert "fake@x.io" not in restored


@pytest.mark.asyncio
async def test_no_deanonymization_when_disabled():
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "Zorp Glax", "PERSON")
    cfg = DeanonymizationConfig(enabled=False)
    chunks = [FakeChunk("Hello Zorp Glax"), FakeChunk("", finish_reason="stop")]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]

    assert "Zorp Glax" in _collect_content(events)


def _payloads(events: list[str]) -> list[dict]:
    out = []
    for event in events:
        for line in event.splitlines():
            if line.startswith("data: ") and line[len("data: "):].strip() != "[DONE]":
                out.append(json.loads(line[len("data: "):]))
    return out


class RawChunk:
    def __init__(self, data: dict) -> None:
        self._data = data

    def model_dump(self) -> dict:
        return self._data


@pytest.mark.asyncio
async def test_multi_choice_streams_use_independent_buffers():
    # n=2: each choice echoes a DIFFERENT placeholder split across chunks; the
    # buffers must not mix and both must restore.
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "<PERSON_1>", "PERSON")
    mapping.add("Paul Martin", "<PERSON_2>", "PERSON")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)

    def _choice(idx, content, finish=None):
        return {"index": idx, "delta": {"content": content}, "finish_reason": finish}

    chunks = [
        RawChunk({"model": "m", "choices": [_choice(0, "A: <PERS")]}),
        RawChunk({"model": "m", "choices": [_choice(1, "B: <PERS")]}),
        RawChunk({"model": "m", "choices": [_choice(0, "ON_1> ok", "stop")]}),
        RawChunk({"model": "m", "choices": [_choice(1, "ON_2> ok", "stop")]}),
    ]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    by_choice: dict[int, str] = {}
    for payload in _payloads(events):
        for choice in payload.get("choices", []):
            content = choice.get("delta", {}).get("content")
            if content:
                idx = choice.get("index", 0)
                by_choice[idx] = by_choice.get(idx, "") + content

    assert by_choice[0] == "A: Marie Dupont ok"
    assert by_choice[1] == "B: Paul Martin ok"


@pytest.mark.asyncio
async def test_provider_finish_chunk_survives_with_usage_and_id():
    # The finish chunk must be forwarded, not replaced by a synthetic one: its
    # id, usage and any other provider fields have to reach the client.
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "<PERSON_1>", "PERSON")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)

    chunks = [
        RawChunk({"id": "chatcmpl-real", "model": "m", "choices": [
            {"index": 0, "delta": {"content": "hi <PERSON_1>"}, "finish_reason": None}
        ]}),
        RawChunk({"id": "chatcmpl-real", "model": "m",
                  "usage": {"total_tokens": 7},
                  "choices": [
                      {"index": 0, "delta": {}, "finish_reason": "stop"}
                  ]}),
    ]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    payloads = _payloads(events)

    finish = [p for p in payloads
              if any(c.get("finish_reason") for c in p.get("choices", []))]
    assert len(finish) == 1  # exactly one finish chunk, never a duplicated pair
    assert finish[0]["id"] == "chatcmpl-real"
    assert finish[0]["usage"] == {"total_tokens": 7}
    assert "Marie Dupont" in _collect_content(events)


@pytest.mark.asyncio
async def test_initial_role_chunk_is_not_swallowed():
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "<PERSON_1>", "PERSON")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)

    chunks = [
        RawChunk({"model": "m", "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": ""},
             "finish_reason": None}
        ]}),
        FakeChunk("hello", finish_reason="stop"),
    ]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    roles = [
        choice.get("delta", {}).get("role")
        for payload in _payloads(events)
        for choice in payload.get("choices", [])
    ]
    assert "assistant" in roles


@pytest.mark.asyncio
async def test_buffered_tail_flushed_when_stream_ends_without_finish():
    # The provider dies before sending finish_reason: the held-back placeholder
    # prefix must still be emitted (restored) before [DONE].
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "<PERSON_1>", "PERSON")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)

    chunks = [FakeChunk("bye <PERSON_1")]  # no finish chunk ever arrives

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    full = _collect_content(events)

    assert full == "bye <PERSON_1"  # tail emitted verbatim, nothing dropped
    assert events[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_reasoning_content_is_restored():
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "<PERSON_1>", "PERSON")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)

    chunks = [
        RawChunk({"model": "m", "choices": [
            {"index": 0,
             "delta": {"reasoning_content": "the user is <PERSON_1>"},
             "finish_reason": None}
        ]}),
        FakeChunk("", finish_reason="stop"),
    ]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    reasoning = "".join(
        choice.get("delta", {}).get("reasoning_content") or ""
        for payload in _payloads(events)
        for choice in payload.get("choices", [])
    )
    assert "Marie Dupont" in reasoning
    assert "<PERSON_1>" not in reasoning
