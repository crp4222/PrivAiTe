from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
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


class StreamingHandler:
    @staticmethod
    async def stream_response(
        litellm_stream: Any,
        mapping: PIIMapping | None,
        deanonymizer_config: DeanonymizationConfig | None,
    ) -> AsyncIterator[str]:
        do_deanon = bool(
            mapping
            and deanonymizer_config
            and deanonymizer_config.enabled
            and not mapping.is_empty
        )
        # One de-anonymizer for message content, one per tool-call index (each
        # tool call streams its arguments independently), and one for the legacy
        # function_call. A placeholder can be split across argument deltas, so the
        # trie buffer holds back partial matches until they resolve.
        content_deanon: StreamingDeAnonymizer | None = (
            StreamingDeAnonymizer(mapping) if do_deanon and mapping else None
        )
        tool_buffers: dict[int, StreamingDeAnonymizer] = {}
        func_buffer: StreamingDeAnonymizer | None = None

        model_name = ""

        try:
            async for chunk in litellm_stream:
                chunk_dict = chunk.model_dump() if hasattr(chunk, "model_dump") else dict(chunk)
                model_name = chunk_dict.get("model", model_name)

                choices = chunk_dict.get("choices", [])
                if not choices:
                    yield format_sse_event(json.dumps(chunk_dict))
                    continue

                choice = choices[0]
                delta = choice.get("delta", {}) or {}
                content = delta.get("content")
                tool_calls = delta.get("tool_calls")
                function_call = delta.get("function_call")
                finish_reason = choice.get("finish_reason")

                has_reasoning = (
                    delta.get("reasoning_content") is not None
                    or delta.get("reasoning") is not None
                )

                if do_deanon and mapping:
                    if content and content_deanon is not None:
                        delta["content"] = content_deanon.feed(content)
                    if tool_calls:
                        for call in tool_calls:
                            fn = call.get("function") or {}
                            if fn.get("arguments"):
                                idx = call.get("index", 0)
                                buf = tool_buffers.get(idx)
                                if buf is None:
                                    buf = StreamingDeAnonymizer(mapping)
                                    tool_buffers[idx] = buf
                                fn["arguments"] = buf.feed(fn["arguments"])
                    if function_call and function_call.get("arguments"):
                        if func_buffer is None:
                            func_buffer = StreamingDeAnonymizer(mapping)
                        function_call["arguments"] = func_buffer.feed(function_call["arguments"])

                if tool_calls or function_call:
                    yield format_sse_event(json.dumps(chunk_dict))
                elif content is not None:
                    if not do_deanon or delta.get("content") or has_reasoning:
                        yield format_sse_event(json.dumps(chunk_dict))
                elif not finish_reason:
                    yield format_sse_event(json.dumps(chunk_dict))

                if finish_reason:
                    if content_deanon is not None:
                        remaining = content_deanon.flush()
                        if remaining:
                            yield format_sse_event(json.dumps(
                                create_chunk_dict(content=remaining, model=model_name)
                            ))
                    for idx, buf in tool_buffers.items():
                        remaining = buf.flush()
                        if remaining:
                            yield format_sse_event(json.dumps(create_delta_chunk(
                                {"tool_calls": [
                                    {"index": idx, "function": {"arguments": remaining}}
                                ]},
                                model=model_name,
                            )))
                    if func_buffer is not None:
                        remaining = func_buffer.flush()
                        if remaining:
                            yield format_sse_event(json.dumps(create_delta_chunk(
                                {"function_call": {"arguments": remaining}},
                                model=model_name,
                            )))

                    final_chunk = create_chunk_dict(
                        content="", model=model_name, finish_reason=finish_reason
                    )
                    yield format_sse_event(json.dumps(final_chunk))

        except Exception:
            logger.exception("Error during streaming")
            raise

        yield format_sse_done()
