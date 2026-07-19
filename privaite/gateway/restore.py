"""Response restore for the gateway.

Whole-JSON restore for complete payloads (non-streaming bodies, non-delta SSE
events) and a holdback SSE restorer for incremental deltas: one
StreamingDeAnonymizer per text channel so a placeholder split across two events
is still restored, driven by the protocol's declarative event classification.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from privaite.gateway.protocols import Channel, EventPlan, ProtocolSpec
from privaite.pii.engine import PIIEngine, PIIProcessingError
from privaite.pii.mapping import PIIMapping
from privaite.streaming.buffer import StreamingDeAnonymizer

logger = logging.getLogger("privaite.gateway.restore")


def restore_tree(
    engine: PIIEngine, value: Any, mapping: PIIMapping, skip_types: frozenset[str]
) -> Any:
    """Restore every string leaf except inside skipped block types (thinking
    blocks must reach the client byte-for-byte). Restoration is pure replacement
    of known fakes, so walking extra fields is harmless. The one non-leaf case:
    an `arguments` string is JSON source text, restored on its parsed tree."""
    if isinstance(value, dict):
        if skip_types and value.get("type") in skip_types:
            return value
        return {
            k: _restore_json_arguments(engine, v, mapping)
            if k == "arguments" and isinstance(v, str)
            else restore_tree(engine, v, mapping, skip_types)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [restore_tree(engine, v, mapping, skip_types) for v in value]
    if isinstance(value, str):
        return engine.restore_document(value, mapping)
    return value


def _restore_json_arguments(engine: PIIEngine, arguments: str, mapping: PIIMapping) -> Any:
    """Restore a function/MCP call's `arguments`: a JSON document inside a
    string. Plain substitution would splice a raw quote, backslash or newline
    from the original into a JSON string literal and the client's json.loads
    of the arguments would fail; restore on the parsed tree and re-encode
    instead. When nothing changes (or the string is not JSON) the current
    byte-identical passthrough is kept."""
    plain = engine.restore_document(arguments, mapping)
    if plain == arguments:
        return arguments
    try:
        parsed = json.loads(arguments)
    except ValueError:
        # Not JSON: the plain string restore is all there is.
        return plain
    restored = restore_tree(engine, parsed, mapping, frozenset())
    return json.dumps(restored, ensure_ascii=False)


def _json_escape(value: str) -> str:
    """The JSON string-literal encoding of value, without the surrounding
    quotes: what a restored original must look like when spliced into streamed
    JSON fragments."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


def _json_escaped_mapping(mapping: PIIMapping) -> PIIMapping:
    """A derived mapping whose originals are JSON-string-escaped, for holdback
    buffers restoring inside JSON source text. The fakes are left as-is:
    placeholders contain no JSON-escaping characters, so they appear literally
    in the fragments (a fake that does escape would never match either way,
    with or without this derivation)."""
    escaped = PIIMapping()
    for fake, original in mapping.get_all_fakes().items():
        escaped.add(_json_escape(original), fake, mapping.get_entity_type(original) or "")
    return escaped


def _sse_block(lines: list[str]) -> str:
    return "\n".join(lines) + "\n\n"


class _SSERestorer:
    """Restores one SSE stream: rewrites data payloads per the protocol's event
    plans and preserves the upstream framing (event: lines, [DONE], comments)."""

    def __init__(self, engine: PIIEngine, mapping: PIIMapping, spec: ProtocolSpec) -> None:
        self._engine = engine
        self._mapping = mapping
        self._spec = spec
        self._buffers: dict[Channel, StreamingDeAnonymizer] = {}

    def _feed(self, channel: Channel, fragment: str, json_fragment: bool) -> str:
        buffer = self._buffers.get(channel)
        if buffer is None:
            # A JSON-fragment channel (streamed tool arguments) restores with
            # escaped originals, so the spliced value stays a valid piece of
            # the JSON string literal it lands in.
            mapping = _json_escaped_mapping(self._mapping) if json_fragment else self._mapping
            buffer = self._buffers[channel] = StreamingDeAnonymizer(mapping)
        return buffer.feed(fragment)

    def flush(self, channels: tuple[Channel, ...] = (), flush_all: bool = False) -> list[str]:
        """Emit synthetic delta events carrying any held-back text, so a partial
        placeholder at a block boundary is never silently dropped."""
        keys = list(self._buffers) if flush_all else [c for c in channels if c in self._buffers]
        out: list[str] = []
        for key in keys:
            remaining = self._buffers[key].flush()
            if remaining:
                name, data = self._spec.flush_event(key, remaining)
                out.append(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")
        return out

    def process_event(self, lines: list[str]) -> list[str]:
        """One full SSE event (its raw lines, no trailing blank) -> the SSE text
        blocks to emit, flush events first."""
        data_indices = [i for i, line in enumerate(lines) if line.startswith("data:")]
        if not data_indices:
            # Comment/keepalive or event-name-only block: pass through verbatim.
            return [_sse_block(lines)]

        payload = "\n".join(lines[i][5:].removeprefix(" ") for i in data_indices)
        if payload.strip() == "[DONE]":
            return self.flush(flush_all=True) + [_sse_block(lines)]
        try:
            data = json.loads(payload)
        except ValueError:
            return [_sse_block(lines)]
        if not isinstance(data, dict):
            return [_sse_block(lines)]

        plan = self._spec.classify_event(data)
        pre = self.flush(plan.flush, plan.flush_all) if (plan.flush or plan.flush_all) else []
        if plan.kind == "verbatim":
            return pre + [_sse_block(lines)]
        new_payload = self._rewrite_payload(data, plan)
        return pre + [_sse_block(self._replace_data(lines, data_indices, new_payload))]

    def _rewrite_payload(self, data: dict[str, Any], plan: EventPlan) -> str:
        """Return the event's new `data:` payload: an incremental delta fed
        through the holdback buffer, or a whole complete value restored."""
        if plan.kind == "delta":
            self._feed_delta(data, plan)
            return json.dumps(data, ensure_ascii=False)
        restored = restore_tree(self._engine, data, self._mapping, self._spec.skip_restore_types)
        return json.dumps(restored, ensure_ascii=False)

    def _feed_delta(self, data: dict[str, Any], plan: EventPlan) -> None:
        parent: Any = data
        for key in plan.path[:-1]:
            parent = parent.get(key) if isinstance(parent, dict) else None
        leaf = plan.path[-1]
        if isinstance(parent, dict) and isinstance(parent.get(leaf), str):
            assert plan.channel is not None
            parent[leaf] = self._feed(plan.channel, parent[leaf], plan.json_fragment)

    @staticmethod
    def _replace_data(lines: list[str], data_indices: list[int], new_payload: str) -> list[str]:
        """Rebuild the event lines with the rewritten payload on a single data
        line, preserving every non-data line (event name, id, comments)."""
        data_set = set(data_indices)
        rebuilt: list[str] = []
        emitted_data = False
        for i, line in enumerate(lines):
            if i in data_set:
                if not emitted_data:
                    rebuilt.append("data: " + new_payload)
                    emitted_data = True
            else:
                rebuilt.append(line)
        return rebuilt


async def restore_sse_stream(
    upstream: httpx.Response,
    engine: PIIEngine,
    mapping: PIIMapping,
    spec: ProtocolSpec,
) -> AsyncIterator[str]:
    """Restore an upstream SSE stream event by event, flushing every channel at
    stream end so no held-back text is dropped."""
    restorer = _SSERestorer(engine, mapping, spec)
    try:
        pending: list[str] = []
        async for line in upstream.aiter_lines():
            if line == "":
                if pending:
                    for block in restorer.process_event(pending):
                        yield block
                    pending = []
                continue
            pending.append(line)
        if pending:
            for block in restorer.process_event(pending):
                yield block
        for block in restorer.flush(flush_all=True):
            yield block
    except PIIProcessingError:
        raise
    except Exception:
        # A stream error can originate from a payload being restored; never
        # serialize its traceback, it could carry the caller's PII.
        logger.error("Gateway stream restore failed")
        raise PIIProcessingError() from None
    finally:
        await upstream.aclose()
