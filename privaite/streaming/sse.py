from __future__ import annotations

import time
import uuid


def format_sse_event(data: str) -> str:
    return f"data: {data}\n\n"


def format_sse_done() -> str:
    return "data: [DONE]\n\n"


def create_chunk_dict(
    content: str,
    model: str = "",
    finish_reason: str | None = None,
) -> dict:
    return create_delta_chunk(
        {"content": content} if content else {},
        model=model,
        finish_reason=finish_reason,
    )


def create_delta_chunk(
    delta: dict,
    model: str = "",
    finish_reason: str | None = None,
) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
