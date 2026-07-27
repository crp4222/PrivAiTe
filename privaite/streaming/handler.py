from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

from privaite.config.schema import DeanonymizationConfig
from privaite.pii.engine import PIIProcessingError
from privaite.pii.mapping import PIIMapping
from privaite.streaming.buffer import StreamingDeAnonymizer, json_escaped_mapping
from privaite.streaming.sse import (
    create_chunk_dict,
    create_delta_chunk,
    format_sse_done,
    format_sse_event,
)

logger = logging.getLogger("privaite.streaming.handler")


# Plain-string delta fields restored with one buffer per (choice, field):
# reasoning traces and the refusal (a refusal can quote the request).
_TEXT_DELTA_FIELDS = ("reasoning_content", "reasoning", "refusal")


def _feed_audio_transcript(delta: dict, buf: Callable[[], StreamingDeAnonymizer]) -> None:
    """Route a delta's audio transcript fragment through its restore buffer."""
    audio = delta.get("audio")
    if isinstance(audio, dict) and isinstance(audio.get("transcript"), str):
        if audio["transcript"]:
            audio["transcript"] = buf().feed(audio["transcript"])


def _flush_audio_remainder(delta: dict, abuf: StreamingDeAnonymizer | None) -> None:
    """Append whatever the audio buffer still holds onto this delta's transcript,
    creating the audio dict when the finish chunk does not carry one."""
    if abuf is None:
        return
    remaining = abuf.flush()
    if not remaining:
        return
    audio = delta.get("audio")
    if not isinstance(audio, dict):
        audio = {}
        delta["audio"] = audio
    audio["transcript"] = (audio.get("transcript") or "") + remaining


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
            mapping and deanonymizer_config and deanonymizer_config.enabled and not mapping.is_empty
        )

        content_bufs: dict[int, StreamingDeAnonymizer] = {}
        text_field_bufs: dict[tuple[int, str], StreamingDeAnonymizer] = {}
        audio_bufs: dict[int, StreamingDeAnonymizer] = {}
        tool_bufs: dict[tuple[int, int], StreamingDeAnonymizer] = {}
        func_bufs: dict[int, StreamingDeAnonymizer] = {}
        finished: set[int] = set()
        model_name = ""
        json_mapping: PIIMapping | None = None

        def _buf(store: dict, key: Any, json_fragment: bool = False) -> StreamingDeAnonymizer:
            # Argument fragments are JSON source text: restore them with escaped
            # originals so a value carrying a quote, a backslash or a newline
            # stays a valid piece of the JSON string literal it lands in
            # (built once per stream, and only when such a fragment shows up).
            nonlocal json_mapping
            buf = store.get(key)
            if buf is None:
                if json_fragment and json_mapping is None:
                    json_mapping = json_escaped_mapping(mapping)  # type: ignore[arg-type]
                source = json_mapping if json_fragment else mapping
                buf = store[key] = StreamingDeAnonymizer(source)  # type: ignore[arg-type]
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
            for field in _TEXT_DELTA_FIELDS:
                rbuf = text_field_bufs.get((idx, field))
                if rbuf is not None:
                    remaining = rbuf.flush()
                    if remaining:
                        delta[field] = (delta.get(field) or "") + remaining
            _flush_audio_remainder(delta, audio_bufs.get(idx))
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
                    pre_events.append(
                        create_delta_chunk(
                            {
                                "tool_calls": [
                                    {"index": t_idx, "function": {"arguments": remaining}}
                                ]
                            },
                            model=model_name,
                            index=idx,
                        )
                    )
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
                        pre_events.append(
                            create_delta_chunk(
                                {"function_call": {"arguments": remaining}},
                                model=model_name,
                                index=idx,
                            )
                        )

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
                    for field in _TEXT_DELTA_FIELDS:
                        value = delta.get(field)
                        if isinstance(value, str) and value:
                            delta[field] = _buf(text_field_bufs, (idx, field)).feed(value)
                    _feed_audio_transcript(delta, lambda: _buf(audio_bufs, idx))
                    for call in delta.get("tool_calls") or []:
                        if not isinstance(call, dict):
                            continue
                        fn = call.get("function") or {}
                        if fn.get("arguments"):
                            fn["arguments"] = _buf(
                                tool_bufs, (idx, call.get("index", 0) or 0), json_fragment=True
                            ).feed(fn["arguments"])
                    function_call = delta.get("function_call")
                    if isinstance(function_call, dict) and function_call.get("arguments"):
                        function_call["arguments"] = _buf(func_bufs, idx, json_fragment=True).feed(
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
                text_field_bufs,
                finished,
                lambda key, r: create_delta_chunk({key[1]: r}, model=model_name, index=key[0]),
            ):
                yield sse
            for sse in _drain(
                audio_bufs,
                finished,
                lambda idx, r: create_delta_chunk(
                    {"audio": {"transcript": r}}, model=model_name, index=idx
                ),
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
            # A stream error can originate from a chunk being restored. Never
            # serialize its traceback: it could contain the caller's PII.
            logger.error("Streaming response processing failed")
            raise PIIProcessingError() from None

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
            mapping and deanonymizer_config and deanonymizer_config.enabled and not mapping.is_empty
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
            # See stream_response: error details may carry restored PII.
            logger.error("Text-completion stream processing failed")
            raise PIIProcessingError() from None

        yield format_sse_done()
