from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

from privaite.config.schema import DeanonymizationConfig
from privaite.pii.mapping import PIIMapping
from privaite.streaming.buffer import StreamingDeAnonymizer
from privaite.streaming.sse import (
    create_chunk_dict,
    create_delta_chunk,
    format_sse_done,
    format_sse_event,
)

logger = logging.getLogger("privaite.streaming.handler")


_REASONING_FIELDS = ("reasoning_content", "reasoning")


def _drain(
    buffers: dict[Any, Any],
    finished: set[int],
    make_chunk: Callable[[Any, str], dict],
) -> Iterator[str]:
    """After the stream ends, emit whatever each buffer still holds so no restored
    text is silently dropped, skipping choices that already got a finish chunk. The
    buffer key is the choice index, or a tuple whose first element is it;
    ``make_chunk(key, remaining)`` builds the chunk dict to send."""
    for key, buf in buffers.items():
        idx = key[0] if isinstance(key, tuple) else key
        if idx in finished:
            continue
        remaining = buf.flush()
        if remaining:
            yield format_sse_event(json.dumps(make_chunk(key, remaining)))


class StreamingHandler:
    @staticmethod
    async def stream_response(
        litellm_stream: Any,
        mapping: PIIMapping | None,
        deanonymizer_config: DeanonymizationConfig | None,
    ) -> AsyncIterator[str]:
        """Stream a chat completion, restoring PII in place.

        Provider chunks are forwarded as-is (ids, usage, logprobs and the finish
        chunk itself all survive); only the streamed text is rewritten. Buffers
        are per choice index (n>1 streams stay independent), per (choice, tool
        index) for tool-call arguments, plus the legacy function_call and the
        reasoning fields. A placeholder split across chunks is held back by the
        trie buffer until it resolves; whatever a buffer still holds is appended
        onto the choice's finish chunk, or flushed after the stream if the
        provider never sent a finish_reason.
        """
        do_deanon = bool(
            mapping
            and deanonymizer_config
            and deanonymizer_config.enabled
            and not mapping.is_empty
        )

        content_bufs: dict[int, StreamingDeAnonymizer] = {}
        reasoning_bufs: dict[tuple[int, str], StreamingDeAnonymizer] = {}
        tool_bufs: dict[tuple[int, int], StreamingDeAnonymizer] = {}
        func_bufs: dict[int, StreamingDeAnonymizer] = {}
        finished: set[int] = set()
        model_name = ""

        def _buf(store: dict, key: Any) -> StreamingDeAnonymizer:
            buf = store.get(key)
            if buf is None:
                buf = store[key] = StreamingDeAnonymizer(mapping)  # type: ignore[arg-type]
            return buf

        def _flush_into_delta(idx: int, delta: dict, pre_events: list[dict]) -> None:
            # Append everything still held for this choice onto its finish chunk;
            # tool/function remainders whose slot is absent from this chunk go out
            # as small synthetic delta chunks just before it.
            buf = content_bufs.get(idx)
            if buf is not None:
                remaining = buf.flush()
                if remaining:
                    delta["content"] = (delta.get("content") or "") + remaining
            for field in _REASONING_FIELDS:
                rbuf = reasoning_bufs.get((idx, field))
                if rbuf is not None:
                    remaining = rbuf.flush()
                    if remaining:
                        delta[field] = (delta.get(field) or "") + remaining
            present = {
                call.get("index", 0) or 0: call
                for call in delta.get("tool_calls") or []
                if isinstance(call, dict)
            }
            for (c_idx, t_idx), tbuf in tool_bufs.items():
                if c_idx != idx:
                    continue
                remaining = tbuf.flush()
                if not remaining:
                    continue
                call = present.get(t_idx)
                if call is not None:
                    # function may be present-but-None on nonstandard chunks;
                    # setdefault would hand back the None and crash.
                    fn = call.get("function")
                    if not isinstance(fn, dict):
                        fn = {}
                        call["function"] = fn
                    fn["arguments"] = (fn.get("arguments") or "") + remaining
                else:
                    pre_events.append(create_delta_chunk(
                        {"tool_calls": [
                            {"index": t_idx, "function": {"arguments": remaining}}
                        ]},
                        model=model_name, index=idx,
                    ))
            fbuf = func_bufs.get(idx)
            if fbuf is not None:
                remaining = fbuf.flush()
                if remaining:
                    function_call = delta.get("function_call")
                    if isinstance(function_call, dict):
                        function_call["arguments"] = (
                            function_call.get("arguments") or ""
                        ) + remaining
                    else:
                        pre_events.append(create_delta_chunk(
                            {"function_call": {"arguments": remaining}},
                            model=model_name, index=idx,
                        ))

        try:
            async for chunk in litellm_stream:
                chunk_dict = chunk.model_dump() if hasattr(chunk, "model_dump") else dict(chunk)
                model_name = chunk_dict.get("model", model_name)

                choices = chunk_dict.get("choices") or []
                if not choices or not do_deanon:
                    yield format_sse_event(json.dumps(chunk_dict))
                    continue

                pre_events: list[dict] = []
                visible = False

                for choice in choices:
                    if not isinstance(choice, dict):
                        visible = True
                        continue
                    idx = choice.get("index", 0) or 0
                    delta = choice.get("delta") or {}
                    choice["delta"] = delta
                    content = delta.get("content")
                    finish_reason = choice.get("finish_reason")

                    if content:
                        delta["content"] = _buf(content_bufs, idx).feed(content)
                    for field in _REASONING_FIELDS:
                        value = delta.get(field)
                        if isinstance(value, str) and value:
                            delta[field] = _buf(reasoning_bufs, (idx, field)).feed(value)
                    for call in delta.get("tool_calls") or []:
                        if not isinstance(call, dict):
                            continue
                        fn = call.get("function") or {}
                        if fn.get("arguments"):
                            fn["arguments"] = _buf(
                                tool_bufs, (idx, call.get("index", 0) or 0)
                            ).feed(fn["arguments"])
                    function_call = delta.get("function_call")
                    if isinstance(function_call, dict) and function_call.get("arguments"):
                        function_call["arguments"] = _buf(func_bufs, idx).feed(
                            function_call["arguments"]
                        )

                    if finish_reason:
                        finished.add(idx)
                        _flush_into_delta(idx, delta, pre_events)

                    # Suppress only the pure hold-back case: this choice HAD text,
                    # all of it is buffered, and NOTHING else rides on the choice
                    # (no other delta field, no logprobs or any other choice-level
                    # payload). Anything else present means the chunk must reach
                    # the client even with its text held back.
                    delta_extra = any(
                        value not in (None, "", [])
                        for key, value in delta.items()
                        if key != "content"
                    )
                    choice_extra = any(
                        value is not None
                        for key, value in choice.items()
                        if key not in ("index", "delta", "finish_reason")
                    )
                    held_back = (
                        bool(content)
                        and not delta.get("content")
                        and not finish_reason
                        and not delta_extra
                        and not choice_extra
                    )
                    if not held_back:
                        visible = True

                # A held-back chunk that carries usage must still go out.
                if not visible and chunk_dict.get("usage") is not None:
                    visible = True

                for event in pre_events:
                    yield format_sse_event(json.dumps(event))
                if visible:
                    yield format_sse_event(json.dumps(chunk_dict))

            # Stream ended without a finish chunk for some choice: emit whatever the
            # buffers still hold so no restored text is silently dropped.
            for sse in _drain(
                content_bufs,
                finished,
                lambda idx, r: create_chunk_dict(content=r, model=model_name, index=idx),
            ):
                yield sse
            for sse in _drain(
                reasoning_bufs,
                finished,
                lambda key, r: create_delta_chunk({key[1]: r}, model=model_name, index=key[0]),
            ):
                yield sse
            for sse in _drain(
                tool_bufs,
                finished,
                lambda key, r: create_delta_chunk(
                    {"tool_calls": [{"index": key[1], "function": {"arguments": r}}]},
                    model=model_name,
                    index=key[0],
                ),
            ):
                yield sse
            for sse in _drain(
                func_bufs,
                finished,
                lambda idx, r: create_delta_chunk(
                    {"function_call": {"arguments": r}}, model=model_name, index=idx
                ),
            ):
                yield sse

        except Exception:
            logger.exception("Error during streaming")
            raise

        yield format_sse_done()

    @staticmethod
    async def stream_text_response(
        litellm_stream: Any,
        mapping: PIIMapping | None,
        deanonymizer_config: DeanonymizationConfig | None,
    ) -> AsyncIterator[str]:
        """Stream a /v1/completions (text_completion) response, restoring PII in
        each choice's `text`. Chunks are forwarded as-is (ids, usage and any other
        provider fields survive); only the text is rewritten. One trie buffer per
        choice index, flushed onto the choice's finish chunk, or after the stream
        if the provider never sent a finish_reason."""
        do_deanon = bool(
            mapping
            and deanonymizer_config
            and deanonymizer_config.enabled
            and not mapping.is_empty
        )
        buffers: dict[int, StreamingDeAnonymizer] = {}
        flushed: set[int] = set()
        model_name = ""

        try:
            async for chunk in litellm_stream:
                chunk_dict = chunk.model_dump() if hasattr(chunk, "model_dump") else dict(chunk)
                model_name = chunk_dict.get("model", model_name)

                if do_deanon and mapping:
                    for choice in chunk_dict.get("choices") or []:
                        if not isinstance(choice, dict):
                            continue
                        idx = choice.get("index", 0) or 0
                        buf = buffers.get(idx)
                        if buf is None:
                            buf = buffers[idx] = StreamingDeAnonymizer(mapping)
                        text = choice.get("text")
                        if text:
                            choice["text"] = buf.feed(text)
                        if choice.get("finish_reason"):
                            flushed.add(idx)
                            remaining = buf.flush()
                            if remaining:
                                # only rewrite when there is something to append,
                                # so a finish chunk's text: null stays null.
                                choice["text"] = (choice.get("text") or "") + remaining

                yield format_sse_event(json.dumps(chunk_dict))

            # Stream ended without a finish chunk for some choice: emit whatever the
            # buffers still hold so no restored text is silently dropped.
            for sse in _drain(
                buffers,
                flushed,
                lambda idx, r: {
                    "object": "text_completion",
                    "model": model_name,
                    "choices": [{"index": idx, "text": r, "finish_reason": None}],
                },
            ):
                yield sse

        except Exception:
            logger.exception("Error during text-completion streaming")
            raise

        yield format_sse_done()
