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
