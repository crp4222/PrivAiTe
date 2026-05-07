from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from privaite.config.schema import DeanonymizationConfig
from privaite.pii.mapping import PIIMapping
from privaite.streaming.buffer import StreamingDeAnonymizer
from privaite.streaming.sse import create_chunk_dict, format_sse_done, format_sse_event

logger = logging.getLogger("privaite.streaming.handler")


class StreamingHandler:
    @staticmethod
    async def stream_response(
        litellm_stream: Any,
        mapping: PIIMapping | None,
        deanonymizer_config: DeanonymizationConfig | None,
    ) -> AsyncIterator[str]:
        deanon: StreamingDeAnonymizer | None = None
        if mapping and deanonymizer_config and deanonymizer_config.enabled and not mapping.is_empty:
            deanon = StreamingDeAnonymizer(mapping)

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
                delta = choice.get("delta", {})
                content = delta.get("content")
                finish_reason = choice.get("finish_reason")

                if content and deanon:
                    deanonymized = deanon.feed(content)
                    if deanonymized:
                        delta["content"] = deanonymized
                        yield format_sse_event(json.dumps(chunk_dict))
                elif content:
                    yield format_sse_event(json.dumps(chunk_dict))
                elif not finish_reason:
                    yield format_sse_event(json.dumps(chunk_dict))

                if finish_reason:
                    if deanon:
                        remaining = deanon.flush()
                        if remaining:
                            flush_chunk = create_chunk_dict(
                                content=remaining, model=model_name
                            )
                            yield format_sse_event(json.dumps(flush_chunk))

                    final_chunk = create_chunk_dict(
                        content="", model=model_name, finish_reason=finish_reason
                    )
                    yield format_sse_event(json.dumps(final_chunk))

        except Exception:
            logger.exception("Error during streaming")
            raise

        yield format_sse_done()
