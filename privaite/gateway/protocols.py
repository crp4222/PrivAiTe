"""Declarative descriptions of the gateway protocols.

Each ProtocolSpec is data (route paths, restore-skip block types) plus pure
functions: classify one parsed SSE payload into what the stream restorer must do
with it, and build the synthetic event that carries a channel's flushed holdback
text. No transport and no engine code lives here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# A holdback buffer key: one per independent text stream inside one response.
Channel = tuple[Any, ...]


@dataclass(frozen=True)
class EventPlan:
    """What the stream restorer must do with one parsed SSE data payload.

    kind "verbatim" re-emits the raw event untouched, "delta" feeds the fragment
    at ``path`` through the channel's holdback buffer, "restore" rewrites the
    whole payload as a complete value (no holdback needed). ``flush`` names the
    channels whose holdback must be emitted BEFORE this event; ``flush_all``
    flushes every open channel. ``json_fragment`` marks a delta whose fragments
    are pieces of JSON source text (streamed tool arguments): a restored
    original spliced into them must be JSON-string-escaped, or a quote,
    backslash or newline in the original breaks the client's accumulated JSON.
    """

    kind: str
    channel: Channel | None = None
    path: tuple[str, ...] = ()
    flush: tuple[Channel, ...] = ()
    flush_all: bool = False
    json_fragment: bool = False


_VERBATIM = EventPlan("verbatim")
_RESTORE = EventPlan("restore")


@dataclass(frozen=True)
class ProtocolSpec:
    name: str
    # (gateway route path, suffix appended to the upstream base_url).
    routes: tuple[tuple[str, str], ...]
    # Block types whose subtree must reach the client byte-for-byte on restore.
    skip_restore_types: frozenset[str]
    classify_event: Callable[[dict[str, Any]], EventPlan]
    # (channel, remaining holdback) -> (SSE event name, data payload).
    flush_event: Callable[[Channel, str], tuple[str, dict[str, Any]]]


def _anthropic_classify(data: dict[str, Any]) -> EventPlan:
    etype = data.get("type")
    if etype == "content_block_delta":
        delta = data.get("delta")
        dtype = delta.get("type") if isinstance(delta, dict) else None
        index = data.get("index", 0)
        if dtype == "text_delta":
            return EventPlan("delta", channel=("text", index), path=("delta", "text"))
        if dtype == "input_json_delta":
            return EventPlan(
                "delta",
                channel=("json", index),
                path=("delta", "partial_json"),
                json_fragment=True,
            )
        if dtype in ("thinking_delta", "signature_delta"):
            # Anthropic rejects thinking blocks that were modified before being
            # echoed back on the next turn: never restore (or scrub) them.
            return _VERBATIM
        return _RESTORE
    if etype == "content_block_stop":
        index = data.get("index", 0)
        return EventPlan("restore", flush=(("text", index), ("json", index)))
    if etype == "message_stop":
        return EventPlan("restore", flush_all=True)
    return _RESTORE


def _anthropic_flush_event(channel: Channel, remaining: str) -> tuple[str, dict[str, Any]]:
    kind, index = channel
    if kind == "json":
        delta: dict[str, Any] = {"type": "input_json_delta", "partial_json": remaining}
    else:
        delta = {"type": "text_delta", "text": remaining}
    return "content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}


# Every text-fragment stream in the Responses SSE union gets a holdback
# buffer: Codex's shell tool is a custom tool, so its streamed command rides
# response.custom_tool_call_input.delta, not function_call_arguments.
_RESPONSES_DELTA_EVENTS = frozenset(
    {
        "response.output_text.delta",
        "response.function_call_arguments.delta",
        "response.custom_tool_call_input.delta",
        "response.mcp_call_arguments.delta",
        "response.refusal.delta",
        "response.audio.transcript.delta",
        "response.code_interpreter_call_code.delta",
    }
)
# Every delta stream has a `.done` twin carrying the complete value, restored
# whole after flushing the delta channel. Deriving one set from the other pins
# the naming symmetry the flush path relies on.
_RESPONSES_DONE_EVENTS = frozenset(
    etype[: -len("delta")] + "done" for etype in _RESPONSES_DELTA_EVENTS
)
# Delta streams whose accumulated fragments form a JSON document (the tool
# arguments string), unlike output_text/custom_tool_call_input/refusal deltas
# whose accumulation is plain text: a restored original spliced into these must
# stay a valid piece of a JSON string literal.
_RESPONSES_JSON_FRAGMENT_EVENTS = frozenset(
    {
        "response.function_call_arguments.delta",
        "response.mcp_call_arguments.delta",
    }
)
# Reasoning passes through untouched, parity with Anthropic thinking blocks
# (the prefix covers reasoning_text, reasoning_summary_text and
# reasoning_summary_part, deltas and dones alike). Audio and partial-image
# payloads are base64: restoring must never rewrite them.
_RESPONSES_VERBATIM_PREFIX = "response.reasoning"
_RESPONSES_VERBATIM_EVENTS = frozenset(
    {
        "response.audio.delta",
        "response.audio.done",
        "response.image_generation_call.partial_image",
    }
)


def _responses_channel_id(data: dict[str, Any]) -> Any:
    item_id = data.get("item_id")
    return item_id if item_id is not None else data.get("output_index", 0)


def _responses_classify(data: dict[str, Any]) -> EventPlan:
    etype = data.get("type") or ""
    if etype in _RESPONSES_DELTA_EVENTS:
        return EventPlan(
            "delta",
            channel=(etype, _responses_channel_id(data)),
            path=("delta",),
            json_fragment=etype in _RESPONSES_JSON_FRAGMENT_EVENTS,
        )
    if etype.startswith(_RESPONSES_VERBATIM_PREFIX) or etype in _RESPONSES_VERBATIM_EVENTS:
        return _VERBATIM
    if etype in _RESPONSES_DONE_EVENTS:
        delta_type = etype[: -len("done")] + "delta"
        return EventPlan("restore", flush=((delta_type, _responses_channel_id(data)),))
    if etype == "response.completed":
        return EventPlan("restore", flush_all=True)
    return _RESTORE


def _responses_flush_event(channel: Channel, remaining: str) -> tuple[str, dict[str, Any]]:
    delta_type, channel_id = channel
    data: dict[str, Any] = {"type": delta_type, "delta": remaining}
    if isinstance(channel_id, str):
        data["item_id"] = channel_id
    else:
        data["output_index"] = channel_id
    return delta_type, data


ANTHROPIC_MESSAGES = ProtocolSpec(
    name="anthropic_messages",
    routes=(
        ("/v1/messages", "/messages"),
        ("/v1/messages/count_tokens", "/messages/count_tokens"),
    ),
    skip_restore_types=frozenset({"thinking", "redacted_thinking"}),
    classify_event=_anthropic_classify,
    flush_event=_anthropic_flush_event,
)

OPENAI_RESPONSES = ProtocolSpec(
    name="openai_responses",
    routes=(("/v1/responses", "/responses"),),
    # encrypted_content is opaque and provider-validated: restoring a
    # placeholder-shaped byte run inside it would corrupt the item. The
    # matching scrub side never touches these types either.
    skip_restore_types=frozenset({"reasoning", "compaction"}),
    classify_event=_responses_classify,
    flush_event=_responses_flush_event,
)

GATEWAY_SPECS = (ANTHROPIC_MESSAGES, OPENAI_RESPONSES)

# Resolved from the specs so the auth middleware never hardcodes a route.
GATEWAY_ROUTE_PATHS = frozenset(path for spec in GATEWAY_SPECS for path, _ in spec.routes)
