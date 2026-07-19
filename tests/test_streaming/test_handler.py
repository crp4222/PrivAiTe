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
        FakeDeltaChunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "save_contact", "arguments": ""},
                    }
                ]
            }
        ),
        FakeDeltaChunk(
            {"tool_calls": [{"index": 0, "function": {"arguments": '{"name": "Zorp Gl'}}]}
        ),
        FakeDeltaChunk({"tool_calls": [{"index": 0, "function": {"arguments": 'ax"}'}}]}),
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
            if line.startswith("data: ") and line[len("data: ") :].strip() != "[DONE]":
                out.append(json.loads(line[len("data: ") :]))
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
        RawChunk(
            {
                "id": "chatcmpl-real",
                "model": "m",
                "choices": [
                    {"index": 0, "delta": {"content": "hi <PERSON_1>"}, "finish_reason": None}
                ],
            }
        ),
        RawChunk(
            {
                "id": "chatcmpl-real",
                "model": "m",
                "usage": {"total_tokens": 7},
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        ),
    ]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    payloads = _payloads(events)

    finish = [p for p in payloads if any(c.get("finish_reason") for c in p.get("choices", []))]
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
        RawChunk(
            {
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            }
        ),
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
async def test_held_back_chunk_with_logprobs_or_refusal_still_emitted():
    # A chunk whose text is fully held back may still carry logprobs (choice
    # level) or a refusal (delta level); suppressing it dropped that payload.
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "<PERSON_1>", "PERSON")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)

    chunks = [
        RawChunk(
            {
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "<PERSON"},
                        "logprobs": {"content": [{"token": "x"}]},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        RawChunk(
            {
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "<PERS", "refusal": "no"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
        FakeChunk("ON_1> ok", finish_reason="stop"),
    ]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    payloads = _payloads(events)

    logprobs = [
        c.get("logprobs")
        for p in payloads
        for c in p.get("choices", [])
        if c.get("logprobs") is not None
    ]
    refusals = [
        c.get("delta", {}).get("refusal")
        for p in payloads
        for c in p.get("choices", [])
        if c.get("delta", {}).get("refusal")
    ]
    assert len(logprobs) == 1
    assert refusals == ["no"]


@pytest.mark.asyncio
async def test_held_back_chunk_with_usage_still_emitted():
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "<PERSON_1>", "PERSON")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)

    chunks = [
        RawChunk(
            {
                "model": "m",
                "usage": {"total_tokens": 5},
                "choices": [{"index": 0, "delta": {"content": "<PERSON"}, "finish_reason": None}],
            }
        ),
        FakeChunk("_1>", finish_reason="stop"),
    ]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    usages = [p.get("usage") for p in _payloads(events) if p.get("usage") is not None]
    assert usages == [{"total_tokens": 5}]


@pytest.mark.asyncio
async def test_finish_chunk_with_function_none_does_not_crash():
    # nonstandard finish chunk: tool_calls slot present with function: None while
    # a tail is still buffered for that index; must append, not crash.
    mapping = PIIMapping()
    mapping.add("marie@acme.com", "<EMAIL_ADDRESS_1>", "EMAIL_ADDRESS")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)

    chunks = [
        RawChunk(
            {
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"to": "<EMAIL_ADDRES'}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        ),
        RawChunk(
            {
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"index": 0, "function": None}]},
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        ),
    ]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    args = _collect_tool_args(events)
    # the held tail is flushed onto the finish chunk instead of being lost
    assert args[0] == '{"to": "<EMAIL_ADDRES'
    assert events[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_reasoning_content_is_restored():
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "<PERSON_1>", "PERSON")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)

    chunks = [
        RawChunk(
            {
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning_content": "the user is <PERSON_1>"},
                        "finish_reason": None,
                    }
                ],
            }
        ),
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


@pytest.mark.asyncio
async def test_refusal_delta_is_restored_even_split_across_chunks():
    # A refusal can quote the request; placeholders in it must be restored like
    # content, including one split across chunk boundaries.
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "<PERSON_1>", "PERSON")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)

    chunks = [
        FakeDeltaChunk({"refusal": "I cannot help <PER"}),
        FakeDeltaChunk({"refusal": "SON_1> with that"}),
        FakeChunk("", finish_reason="stop"),
    ]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    refusal = "".join(
        choice.get("delta", {}).get("refusal") or ""
        for payload in _payloads(events)
        for choice in payload.get("choices", [])
    )
    assert refusal == "I cannot help Marie Dupont with that"


@pytest.mark.asyncio
async def test_refusal_remainder_flushes_when_stream_ends_without_finish():
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "<PERSON_1>", "PERSON")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)

    chunks = [FakeDeltaChunk({"refusal": "no can do <PERSON_1"})]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    refusal = "".join(
        choice.get("delta", {}).get("refusal") or ""
        for payload in _payloads(events)
        for choice in payload.get("choices", [])
    )
    # The held-back partial placeholder is flushed verbatim after the stream.
    assert refusal == "no can do <PERSON_1"


@pytest.mark.asyncio
async def test_audio_transcript_delta_is_restored():
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "<PERSON_1>", "PERSON")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)

    chunks = [
        FakeDeltaChunk({"audio": {"id": "a1", "transcript": "hello <PER"}}),
        FakeDeltaChunk({"audio": {"id": "a1", "transcript": "SON_1>, hi"}}),
        FakeChunk("", finish_reason="stop"),
    ]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    transcript = "".join(
        (choice.get("delta", {}).get("audio") or {}).get("transcript") or ""
        for payload in _payloads(events)
        for choice in payload.get("choices", [])
    )
    assert transcript == "hello Marie Dupont, hi"
    # non-transcript audio fields survive untouched
    ids = {
        (choice.get("delta", {}).get("audio") or {}).get("id")
        for payload in _payloads(events)
        for choice in payload.get("choices", [])
        if choice.get("delta", {}).get("audio")
    }
    assert ids == {"a1"}


@pytest.mark.asyncio
async def test_audio_transcript_remainder_flushes_onto_finish_chunk():
    # The transcript ends on a partial placeholder; the held-back text must be
    # appended on the finish chunk (whose delta has no audio dict of its own).
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "<PERSON_1>", "PERSON")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)

    chunks = [
        FakeDeltaChunk({"audio": {"id": "a1", "transcript": "bye <PERSON_1"}}),
        FakeChunk("", finish_reason="stop"),
    ]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    transcript = "".join(
        (choice.get("delta", {}).get("audio") or {}).get("transcript") or ""
        for payload in _payloads(events)
        for choice in payload.get("choices", [])
    )
    assert transcript == "bye <PERSON_1"


@pytest.mark.asyncio
async def test_audio_transcript_remainder_drains_when_stream_ends_without_finish():
    mapping = PIIMapping()
    mapping.add("Marie Dupont", "<PERSON_1>", "PERSON")
    cfg = DeanonymizationConfig(enabled=True, fuzzy_matching=False)

    chunks = [FakeDeltaChunk({"audio": {"id": "a1", "transcript": "bye <PERSON_1"}})]

    events = [ev async for ev in StreamingHandler.stream_response(_stream(chunks), mapping, cfg)]
    transcript = "".join(
        (choice.get("delta", {}).get("audio") or {}).get("transcript") or ""
        for payload in _payloads(events)
        for choice in payload.get("choices", [])
    )
    assert transcript == "bye <PERSON_1"
    assert events[-1] == "data: [DONE]\n\n"
