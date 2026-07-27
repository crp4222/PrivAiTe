"""Request scrubbing for the gateway protocols.

Only content-bearing subtrees are scanned, matching the documented boundary:
tool/function definitions, the agent's own `system`/`instructions` prompt, and
JSON object keys are never touched. Every scanned string still flows through the
engine's single choke point (PIIEngine._anonymize_text) via scrub_document, so
the block_entities gate and the fail-closed policy apply unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from privaite.pii.engine import PIIEngine
from privaite.pii.mapping import PIIMapping

# Anthropic rejects modified thinking blocks echoed back on a later turn, so
# they are never scrubbed on the way out (nor restored on the way back).
_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})

# Scrub coverage must match restore coverage. The response restore rewrites
# every string leaf except thinking blocks, so the client-side history holds
# REAL values inside server-side and MCP tool blocks too; when the client
# echoes them back on the next turn they must be re-scrubbed exactly like
# tool_use/tool_result. Missing server_tool_use here was a confirmed
# round-trip leak (Claude Code's WebSearch/WebFetch input restored on turn N,
# forwarded raw on turn N+1).
_TOOL_USE_TYPES = frozenset({"tool_use", "server_tool_use", "mcp_tool_use"})
_TOOL_RESULT_TYPES = frozenset({"tool_result", "mcp_tool_result"})

# Anthropic blocks whose whole payload is binary or a pointer: the blob lives
# under `source.data` (or a file id), there is no plaintext sibling to scan.
# Relayed byte-for-byte, parity with _RESPONSES_OPAQUE_TYPES.
_ANTHROPIC_OPAQUE_TYPES = frozenset({"image", "container_upload"})

# Field names that carry plaintext on the Anthropic block schema, used by the
# fallback for block types this build does not know. It is an ALLOWLIST on
# purpose: it cannot corrupt a payload it never reads, so a base64 blob under
# `source.data`, a thinking `signature`, a web-search `encrypted_content` and
# every id stay byte-for-byte, while an unknown block that carries text is
# still scanned instead of relayed raw. `stdout`/`stderr` are the
# code-execution results; `source` and `url` are plaintext strings here (a
# fetch-me pointer is a dict `source`, which goes through the source rule).
_GENERIC_TEXT_FIELDS = ("text", "title", "context", "source", "url", "reason", "stdout", "stderr")

# Fields holding an arbitrary JSON payload on an unknown block (a future
# tool-use variant's `input`, a tool result's `output`): walked leaf by leaf.
_GENERIC_DATA_FIELDS = ("input", "output")

# Responses input items relayed byte-for-byte. Either their payload is opaque
# or binary (encrypted reasoning/compaction, a screenshot, a generated image:
# rewriting base64 corrupts it and the provider validates encrypted content),
# or it is a tool/pointer definition with no user text (parity with the
# documented "tools definitions are not scanned" surface). item_reference only
# names a server-side item by id: no content transits here.
_RESPONSES_OPAQUE_TYPES = frozenset(
    {
        "reasoning",
        "compaction",
        "compaction_trigger",
        "computer_call_output",
        "image_generation_call",
        "item_reference",
        "mcp_list_tools",
        "tool_search_call",
        "tool_search_output",
        "additional_tools",
    }
)

# Item types whose user data sits in a known structured field (per the
# Responses API Item schema in openai/openai-openapi): a typed command/action,
# a patch diff, search queries and retrieved chunks, interpreter code and logs.
_RESPONSES_DATA_FIELDS: dict[str, tuple[str, ...]] = {
    "computer_call": ("action", "actions"),
    "local_shell_call": ("action",),
    "shell_call": ("action",),
    "web_search_call": ("action",),
    "apply_patch_call": ("operation",),
    "file_search_call": ("queries", "results"),
    "code_interpreter_call": ("code", "outputs"),
    "program": ("code",),
    "program_output": ("result",),
}

# Content/output parts whose payload is binary (base64 or a file id), not
# text. Scrubbing them would corrupt the payload without removing anything a
# text detector could actually find.
_BINARY_PART_TYPES = frozenset(
    {
        "input_image",
        "input_file",
        "input_audio",
        "image",
        "output_image",
        "computer_screenshot",
    }
)


async def scrub_anthropic_request(
    engine: PIIEngine, body: dict[str, Any]
) -> tuple[dict[str, Any], PIIMapping]:
    """Scrub an Anthropic Messages (or count_tokens) request body.

    Scanned: messages[] string content, text blocks, tool_use /
    server_tool_use / mcp_tool_use input, tool_result / mcp_tool_result
    content, text-typed document sources and search_result blocks, plus the
    plaintext fields of any block type this build does not know. Untouched:
    `system`, `tools`/`tool_choice`, thinking blocks and binary media blocks.
    """
    mapping = PIIMapping()
    new_body = dict(body)
    messages = body.get("messages")
    if isinstance(messages, list):
        new_body["messages"] = [
            await _scrub_anthropic_message(engine, message, mapping) for message in messages
        ]
    return new_body, mapping


async def _scrub_anthropic_message(engine: PIIEngine, message: Any, mapping: PIIMapping) -> Any:
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if isinstance(content, str):
        new_message = dict(message)
        new_message["content"], _ = await engine.scrub_document(content, mapping)
        return new_message
    if isinstance(content, list):
        new_message = dict(message)
        new_message["content"] = [
            await _scrub_anthropic_block(engine, block, mapping) for block in content
        ]
        return new_message
    return message


async def _scrub_anthropic_block(engine: PIIEngine, block: Any, mapping: PIIMapping) -> Any:
    if not isinstance(block, dict):
        return block
    btype = block.get("type")
    if btype in _THINKING_TYPES:
        return block
    if btype == "text":
        return await _scrub_str_fields(engine, block, ("text",), mapping)
    if btype in _TOOL_USE_TYPES:
        return await _scrub_tool_use_block(engine, block, mapping)
    if btype in _TOOL_RESULT_TYPES:
        return await _scrub_tool_result_block(engine, block, mapping)
    if btype == "document":
        return await _scrub_document_block(engine, block, mapping)
    if btype == "search_result":
        return await _scrub_search_result_block(engine, block, mapping)
    if btype in _ANTHROPIC_OPAQUE_TYPES:
        return block
    return await _scrub_unknown_block(engine, block, mapping)


async def _scrub_unknown_block(
    engine: PIIEngine, block: dict[str, Any], mapping: PIIMapping
) -> dict[str, Any]:
    """Fallback for a block type this build does not know: a beta block, or one
    the API gains after this release.

    Relaying an unknown shape unscanned is exactly how the server_tool_use
    round-trip leak happened, so text is scanned by default here; the same hole
    silently hid the code-execution `stdout`/`stderr` results. Only the
    allowlisted plaintext fields are read (see _GENERIC_TEXT_FIELDS), so binary
    and opaque payloads are relayed byte-for-byte, and thinking blocks never
    reach this function at all.
    """
    new_block = await _scrub_str_fields(engine, block, _GENERIC_TEXT_FIELDS, mapping)
    for field in _GENERIC_DATA_FIELDS:
        if block.get(field) is not None:
            new_block[field], _ = await engine.scrub_document(block[field], mapping)
    if "content" in block:
        new_block["content"] = await _scrub_block_content(engine, block["content"], mapping)
    source = block.get("source")
    if isinstance(source, dict):
        new_block["source"] = await _scrub_block_source(engine, source, mapping)
    return new_block


async def _scrub_str_fields(
    engine: PIIEngine, block: dict[str, Any], fields: tuple[str, ...], mapping: PIIMapping
) -> dict[str, Any]:
    new_block = dict(block)
    for field in fields:
        if isinstance(block.get(field), str):
            new_block[field], _ = await engine.scrub_document(block[field], mapping)
    return new_block


async def _scrub_tool_use_block(
    engine: PIIEngine, block: dict[str, Any], mapping: PIIMapping
) -> dict[str, Any]:
    # The tool-call-argument leak: arguments carry user data verbatim. Applies
    # equally to client tool_use and to the server_tool_use/mcp_tool_use blocks
    # the client echoes back with their input already restored to real values.
    if "input" not in block:
        return block
    new_block = dict(block)
    new_block["input"], _ = await engine.scrub_document(block["input"], mapping)
    return new_block


async def _scrub_tool_result_block(
    engine: PIIEngine, block: dict[str, Any], mapping: PIIMapping
) -> dict[str, Any]:
    if "content" not in block:
        return block
    new_block = dict(block)
    new_block["content"] = await _scrub_block_content(engine, block["content"], mapping)
    return new_block


async def _scrub_block_content(engine: PIIEngine, inner: Any, mapping: PIIMapping) -> Any:
    """A block's `content` payload: a bare string, a list of nested blocks, or a
    single nested block (a web_fetch_result wraps one document there). A bare
    string INSIDE the list is user text too and restore would restore it, so it
    is scanned (same rule as the engine's content walker)."""
    if isinstance(inner, str):
        scrubbed, _ = await engine.scrub_document(inner, mapping)
        return scrubbed
    if isinstance(inner, list):
        return [
            await _scrub_block_content(engine, part, mapping)
            if isinstance(part, str)
            else await _scrub_anthropic_block(engine, part, mapping)
            for part in inner
        ]
    if isinstance(inner, dict):
        return await _scrub_anthropic_block(engine, inner, mapping)
    return inner


async def _scrub_document_block(
    engine: PIIEngine, block: dict[str, Any], mapping: PIIMapping
) -> dict[str, Any]:
    """A document block whose source.type is "text" or "content" carries
    plaintext and is scrubbed (title/context are plaintext on every document).
    base64/url/file sources are binary payloads or pointers: rewriting them
    would corrupt the document, so they are relayed whole."""
    new_block = await _scrub_str_fields(engine, block, ("title", "context"), mapping)
    source = block.get("source")
    if isinstance(source, dict):
        new_block["source"] = await _scrub_block_source(engine, source, mapping)
    return new_block


async def _scrub_block_source(
    engine: PIIEngine, source: dict[str, Any], mapping: PIIMapping
) -> dict[str, Any]:
    """A block `source` object: only a "text" or "content" source is plaintext.
    base64/url/file sources are binary payloads or pointers, returned as they
    came in (the blob lives under `data`: rewriting it corrupts the payload)."""
    stype = source.get("type")
    if stype == "text" and isinstance(source.get("data"), str):
        new_source = dict(source)
        new_source["data"], _ = await engine.scrub_document(source["data"], mapping)
        return new_source
    if stype == "content":
        new_source = dict(source)
        new_source["content"] = await _scrub_block_content(engine, source.get("content"), mapping)
        return new_source
    return source


async def _scrub_search_result_block(
    engine: PIIEngine, block: dict[str, Any], mapping: PIIMapping
) -> dict[str, Any]:
    """search_result blocks carry retrieved plaintext: `content` (text blocks)
    plus the `title` and `source` strings."""
    new_block = await _scrub_str_fields(engine, block, ("title", "source"), mapping)
    if "content" in block:
        new_block["content"] = await _scrub_block_content(engine, block["content"], mapping)
    return new_block


async def scrub_responses_request(
    engine: PIIEngine, body: dict[str, Any]
) -> tuple[dict[str, Any], PIIMapping]:
    """Scrub an OpenAI Responses request: `input` item by item (role message,
    tool calls and their outputs, typed actions, bare string) plus
    `prompt.variables`. Top-level `instructions` stays untouched (the agent's
    own prompt, parity with the Anthropic `system` field); opaque and binary
    items are relayed byte-for-byte (see _RESPONSES_OPAQUE_TYPES)."""
    mapping = PIIMapping()
    new_body = dict(body)
    input_value = body.get("input")
    if isinstance(input_value, str):
        if input_value:
            new_body["input"], _ = await engine.scrub_document(input_value, mapping)
    elif isinstance(input_value, list):
        new_body["input"] = [
            await _scrub_responses_item(engine, item, mapping) for item in input_value
        ]
    # Prompt-template variables carry user data (the template id/version do
    # not); a variable can be a bare string or a typed content part.
    prompt = body.get("prompt")
    if isinstance(prompt, dict) and isinstance(prompt.get("variables"), dict):
        new_prompt = dict(prompt)
        new_prompt["variables"] = {
            key: await _scrub_data_value(engine, value, mapping)
            for key, value in prompt["variables"].items()
        }
        new_body["prompt"] = new_prompt
    return new_body, mapping


async def _scrub_responses_item(engine: PIIEngine, item: Any, mapping: PIIMapping) -> Any:
    if isinstance(item, str):
        scrubbed, _ = await engine.scrub_document(item, mapping)
        return scrubbed
    if not isinstance(item, dict):
        return item
    # Only rewrite content that is actually present. A role item can legitimately
    # carry none (e.g. Codex sends a `developer` item whose payload is `tools`,
    # not `content`); injecting a `content` key there makes the provider reject
    # the whole request as an unknown parameter.
    if "role" in item and "content" in item:
        new_item = dict(item)
        new_item["content"] = await _scrub_responses_content(engine, item["content"], mapping)
        return new_item
    itype = item.get("type") or ""
    if itype in _RESPONSES_OPAQUE_TYPES:
        return item
    typed = await _scrub_typed_item(engine, item, itype, mapping)
    if typed is not None:
        return typed
    return await _scrub_generic_item(engine, item, mapping)


async def _scrub_typed_item(
    engine: PIIEngine, item: dict[str, Any], itype: str, mapping: PIIMapping
) -> Any:
    """Scrub an item whose user data sits in a type-specific field; None hands
    the item to the generic text-field scan."""
    if itype == "function_call" and isinstance(item.get("arguments"), str):
        new_item = dict(item)
        new_item["arguments"] = await _scrub_json_arguments(engine, item["arguments"], mapping)
        return new_item
    # A custom tool call carries its command in `input` (a string), not
    # `arguments`. Codex's shell tool is a custom tool, so this is where a
    # command that embeds user data would sit.
    if itype == "custom_tool_call" and isinstance(item.get("input"), str):
        new_item = dict(item)
        new_item["input"], _ = await engine.scrub_document(item["input"], mapping)
        return new_item
    fields = _RESPONSES_DATA_FIELDS.get(itype)
    if fields is not None:
        return await _scrub_item_fields(engine, item, fields, mapping)
    # Tool OUTPUT is where a file the agent read comes back. Codex reads files
    # through its shell tool, so file contents arrive as custom_tool_call_output
    # items whose `output` is a list of {type, text} parts (a plain string for
    # other tools). The walk scrubs every text leaf but leaves binary parts
    # whole; missing this let every byte of a read file reach the provider
    # unscrubbed. Covers function_call_output, custom_tool_call_output,
    # local_shell_call_output, shell_call_output and apply_patch_call_output
    # alike (computer_call_output is opaque and never reaches here).
    if itype.endswith("_output") and "output" in item:
        new_item = dict(item)
        new_item["output"] = await _scrub_data_value(engine, item["output"], mapping)
        return new_item
    return None


async def _scrub_item_fields(
    engine: PIIEngine, item: dict[str, Any], fields: tuple[str, ...], mapping: PIIMapping
) -> dict[str, Any]:
    new_item = dict(item)
    for field in fields:
        if item.get(field) is not None:
            new_item[field] = await _scrub_data_value(engine, item[field], mapping)
    return new_item


async def _scrub_data_value(engine: PIIEngine, value: Any, mapping: PIIMapping) -> Any:
    """Walk a tool payload (str, list of parts, or arbitrary JSON) scrubbing
    every text leaf, but relay binary parts whole: rewriting base64 corrupts it
    and carries nothing a text detector can find."""
    if isinstance(value, list):
        return [await _scrub_data_value(engine, part, mapping) for part in value]
    if isinstance(value, dict) and value.get("type") in _BINARY_PART_TYPES:
        return value
    scrubbed, _ = await engine.scrub_document(value, mapping)
    return scrubbed


async def _scrub_generic_item(
    engine: PIIEngine, item: dict[str, Any], mapping: PIIMapping
) -> dict[str, Any]:
    """Fallback scan for unknown item shapes: scrub EVERY text-bearing field
    found, not just the first (an mcp_call carries both `arguments` and
    `output`; stopping at the first match would leak the other one)."""
    new_item = dict(item)
    changed = False
    for field in ("output", "arguments", "input", "text", "reason"):
        value = item.get(field)
        if not (isinstance(value, str) and value):
            continue
        changed = True
        if field == "arguments":
            new_item[field] = await _scrub_json_arguments(engine, value, mapping)
        else:
            new_item[field], _ = await engine.scrub_document(value, mapping)
    if isinstance(item.get("content"), (str, list)):
        changed = True
        new_item["content"] = await _scrub_responses_content(engine, item["content"], mapping)
    return new_item if changed else item


async def _scrub_responses_content(engine: PIIEngine, content: Any, mapping: PIIMapping) -> Any:
    if isinstance(content, str):
        scrubbed, _ = await engine.scrub_document(content, mapping)
        return scrubbed
    if not isinstance(content, list):
        return content
    parts: list[Any] = []
    for part in content:
        parts.append(await _scrub_content_part(engine, part, mapping))
    return parts


async def _scrub_content_part(engine: PIIEngine, part: Any, mapping: PIIMapping) -> Any:
    if isinstance(part, str):
        scrubbed, _ = await engine.scrub_document(part, mapping)
        return scrubbed
    if not isinstance(part, dict):
        return part
    # `text` covers input_text/output_text/summary_text; `refusal` is the one
    # message part that carries its text elsewhere. Once restored client-side,
    # an echoed refusal carries real values back, same as any assistant text.
    new_part = None
    for field in ("text", "refusal"):
        if isinstance(part.get(field), str):
            new_part = dict(part) if new_part is None else new_part
            new_part[field], _ = await engine.scrub_document(part[field], mapping)
    return new_part if new_part is not None else part


async def _scrub_json_arguments(engine: PIIEngine, arguments: str, mapping: PIIMapping) -> str:
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, ValueError):
        # Not valid JSON: scan the raw string instead of dropping it.
        scrubbed, _ = await engine.scrub_document(arguments, mapping)
        return str(scrubbed)
    walked, _ = await engine.scrub_document(parsed, mapping)
    return json.dumps(walked, ensure_ascii=False)
