"""
PrivAiTe guardrail for the LiteLLM proxy.

Runs PrivAiTe's engine in-process inside LiteLLM. The pre-call hook anonymizes the
request and stashes the reversible map in the request metadata (consumed and popped
by the post-call hook); the post-call hook restores the real values in the response,
including inside tool-call arguments and the legacy function_call, which LiteLLM's
built-in Presidio guardrail does not touch. It also anonymizes the Responses API
request surface with the same coverage as the gateway (role items, tool calls and
their outputs, typed action carriers, `prompt.variables`; opaque/binary items
relayed whole) and restores Responses `output_text` / function_call output. The
auxiliary request fields LiteLLM forwards verbatim are scrubbed too, matching
the core proxy: chat `prediction.content`, `web_search_options.user_location`
and the completions `prompt`/`suffix` (request side only, nothing to restore;
tokenized integer-array prompts pass through unscanned as documented). Chat
streaming is restored too: text content, streamed tool-call arguments, and the
streamed function_call. (Responses API streaming restore is not yet implemented.)
On the failure path the map is dropped from metadata as well (best-effort), so it
is not left for a failure spend-log to persist.

If `block_entities` is configured, a request containing any of those PII types is
rejected with an HTTP 400 in the pre-call hook, before anything is forwarded to the
model. The error names the offending type(s) only, never the underlying value. The
gate also covers the agent's own prompt (Responses `instructions`, Anthropic
`system`): those fields are read for the gate only and still relayed verbatim.

It reuses PrivAiTe's engine, so there is no detection or masking logic here.

Usage: mount this file next to your LiteLLM config.yaml and reference it by
dot-notation. See integrations/litellm/README.md.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable, Iterator
from typing import Any, cast

from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.types.guardrails import GuardrailEventHooks

# Request fields carrying the AGENT's own prompt, not the user's text: relayed
# to the model verbatim (never rewritten), but still read by the block gate.
_GATED_VERBATIM_FIELDS = ("instructions", "system")

_LANG_MODELS = {
    "en": "en_core_web_lg",
    "fr": "fr_core_news_md",
    "de": "de_core_news_md",
    "es": "es_core_news_md",
    "it": "it_core_news_md",
    "pt": "pt_core_news_md",
    "nl": "nl_core_news_md",
}


def _obj_get(obj: Any, key: str) -> Any:
    """Read a field from a dict-or-object response item."""
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _obj_set(obj: Any, key: str, value: Any) -> None:
    """Write a field on a dict-or-object response item."""
    if isinstance(obj, dict):
        obj[key] = value
    else:
        setattr(obj, key, value)


def _append_nested(delta: Any, carrier: str, field: str, remaining: str) -> None:
    """Append text to delta.<carrier>.<field>, creating the carrier as a plain
    dict when the delta has none (a bare finish delta carries no function_call
    or audio object)."""
    holder = _obj_get(delta, carrier)
    if holder is None:
        _obj_set(delta, carrier, {field: remaining})
    else:
        _obj_set(holder, field, (_obj_get(holder, field) or "") + remaining)


def _append_tool_remainder(delta: Any, tc_index: int, remaining: str) -> None:
    """Append a held argument fragment under its tool_call index, creating the
    slot when the delta has none: the client reassembles arguments per index."""
    calls = _obj_get(delta, "tool_calls")
    if calls is None:
        calls = []
        _obj_set(delta, "tool_calls", calls)
    for call in calls:
        if (_obj_get(call, "index") or 0) == tc_index:
            _append_nested(call, "function", "arguments", remaining)
            return
    calls.append({"index": tc_index, "function": {"arguments": remaining}})


def _append_remainder(delta: Any, key: tuple, remaining: str) -> None:
    """Append a restore buffer's held tail onto a streamed delta. The key is
    (kind, choice index[, tool_call index]) as built by the streaming hook."""
    kind = key[0]
    if kind == "tool":
        _append_tool_remainder(delta, key[2], remaining)
    elif kind == "fc":
        _append_nested(delta, "function_call", "arguments", remaining)
    elif kind == "audio":
        _append_nested(delta, "audio", "transcript", remaining)
    else:
        # content and the text fields (reasoning_content, reasoning, refusal)
        _obj_set(delta, kind, (_obj_get(delta, kind) or "") + remaining)


_DELTA_CHANNELS = (
    "content",
    "reasoning_content",
    "reasoning",
    "refusal",
    "audio",
    "tool_calls",
    "function_call",
)


def _drain_chunk(template: Any, key: tuple, remaining: str) -> Any:
    """A trailing chunk carrying one held tail after the provider closed the
    stream without a finish_reason: a clone of the last chunk of that choice
    (same id, model and object as the rest of the stream) reduced to that one
    choice, its delta cleared, no finish_reason and no usage."""
    chunk = copy.deepcopy(template)
    index = key[1]
    choices = [c for c in _obj_get(chunk, "choices") or [] if (_obj_get(c, "index") or 0) == index]
    choice = choices[0]
    _obj_set(chunk, "choices", [choice])
    if _obj_get(chunk, "usage") is not None:
        _obj_set(chunk, "usage", None)
    _obj_set(choice, "finish_reason", None)
    delta = _obj_get(choice, "delta")
    for field in _DELTA_CHANNELS:
        if _obj_get(delta, field) is not None:
            _obj_set(delta, field, None)
    _append_remainder(delta, key, remaining)
    return chunk


class _StreamRestorer:
    """Restore state for one streamed response.

    One de-anonymizer buffer per streamed segment, keyed by (kind, choice
    index, ...): with n>1 the provider interleaves chunks for different
    choices, and tool-call arguments stream as fragments per tool_call index;
    each segment keeps its own boundary buffer so a placeholder split across
    chunks reassembles without mixing segments. The buffer holds back partial
    placeholders that span chunk boundaries; whatever remains is flushed onto
    the chunk that carries finish_reason (by the channel's own restore when
    the finish delta carries it, by the sweep in restore_chunk when it does
    not: the common finish shape is a bare `delta: {}`), or, when the provider
    closes the stream without a finish_reason, drained as trailing chunks
    cloned from the last chunk seen for that choice. A held tail is never
    dropped."""

    def __init__(
        self, mapping: object, json_mapping: object, make_buffer: Callable[..., object]
    ) -> None:
        self._mapping = mapping
        # For the argument channels: their fragments are JSON source text, so the
        # original has to arrive JSON-escaped or the client's parse of the
        # reassembled arguments fails on a quote, a backslash or a newline.
        # (Placeholders need no escaping, so the fakes are the same on both sides.)
        self._json_mapping = json_mapping
        self._make_buffer = make_buffer
        self._buffers: dict = {}
        self._last_chunk: dict = {}

    def restore(self, key: tuple, text: str, finished: bool, json_fragment: bool = False) -> str:
        if key not in self._buffers:
            self._buffers[key] = self._make_buffer(
                self._json_mapping if json_fragment else self._mapping
            )
        deanon = self._buffers[key]
        out = deanon.feed(text) if text else ""
        if finished:
            # The channel is closed: drop its buffer so a sweep never flushes
            # it a second time.
            out += self._buffers.pop(key).flush()
        return out

    def close_open(self, index: int | None) -> tuple[tuple[tuple, str], ...]:
        """Flush and drop every buffer still open for this choice (every choice
        when index is None); returns the non-empty tails."""
        tails = []
        for key in [k for k in self._buffers if index is None or k[1] == index]:
            remaining = self._buffers.pop(key).flush()
            if remaining:
                tails.append((key, remaining))
        return tuple(tails)

    def restore_chunk(self, chunk: object, restore_delta: Callable[..., None]) -> None:
        """Restore every choice of one chunk in place; on a finished choice,
        append the tails of the channels its finish delta does not carry."""
        for choice in getattr(chunk, "choices", None) or []:
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            index = getattr(choice, "index", 0) or 0
            self._last_chunk[index] = chunk
            finished = getattr(choice, "finish_reason", None) is not None
            restore_delta(delta, index, finished, self.restore)
            if finished:
                for key, remaining in self.close_open(index):
                    _append_remainder(delta, key, remaining)

    def drain(self) -> Iterator[object]:
        """Trailing chunks for the tails still held after the provider closed
        the stream without a finish_reason for their choice."""
        for key, remaining in self.close_open(None):
            template = self._last_chunk.get(key[1])
            if template is not None:
                yield _drain_chunk(template, key, remaining)


def _json_escape(value: str) -> str:
    """The JSON string-literal encoding of value, without the surrounding
    quotes: what a restored original must look like when spliced into streamed
    argument fragments. Mirrors privaite.streaming.buffer.json_escape."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


async def _restore_json_tree(engine: Any, value: Any, mapping: Any) -> Any:
    """Restore every string leaf of a parsed JSON document (keys are not
    rewritten, mirroring the core engine's walker)."""
    if isinstance(value, str):
        return await engine.process_response(value, mapping)
    if isinstance(value, dict):
        return {key: await _restore_json_tree(engine, item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [await _restore_json_tree(engine, item, mapping) for item in value]
    return value


async def _restore_arguments(engine: Any, arguments: str, mapping: Any) -> str:
    """Restore a tool/function call's `arguments`: a JSON document inside a
    string. Plain substitution would splice a raw quote, backslash or newline
    from the original into a JSON string literal and the client's json.loads of
    the arguments would fail; restore on the parsed tree and re-encode instead
    (core parity: PIIEngine._deanonymize_arguments). When nothing changes, or
    the string is not JSON, the previous behaviour is kept byte for byte."""
    plain = await engine.process_response(arguments, mapping)
    if plain == arguments:
        return arguments
    try:
        parsed = json.loads(arguments)
    except ValueError:
        # Not JSON: the plain string restore is all there is.
        return plain
    return json.dumps(await _restore_json_tree(engine, parsed, mapping), ensure_ascii=False)


class PrivaiteGuardrail(CustomGuardrail):
    def __init__(self, **kwargs: Any) -> None:
        self.preset = kwargs.pop("preset", None) or "onnx"
        if self.preset not in ("light", "onnx"):
            self.preset = "onnx"
        self.languages = kwargs.pop("languages", None) or "en,fr"
        deanon = kwargs.pop("deanonymize", True)
        self.deanonymize = (
            deanon
            if isinstance(deanon, bool)
            else str(deanon).strip().lower() not in ("false", "0", "no", "")
        )
        # PII TYPES to reject outright (empty = mask everything, the default).
        # Accepts a YAML list or a comma-separated string.
        self.block_entities = self._parse_block_entities(kwargs.pop("block_entities", None))
        kwargs.setdefault(
            "supported_event_hooks",
            [GuardrailEventHooks.pre_call, GuardrailEventHooks.post_call],
        )
        super().__init__(**kwargs)
        # Both hooks are mandatory: pre_call anonymizes the request and post_call
        # restores the response. A config such as `mode: post_call` would skip the
        # pre_call pass and silently forward raw PII to the model, so ensure both
        # hooks run for plain string/list modes. Tag-based Mode configs are left
        # untouched.
        hook = self.event_hook
        if hook is None or isinstance(hook, str):
            base = [hook] if isinstance(hook, str) and hook else []
            self.event_hook = cast(
                list[GuardrailEventHooks],
                list(dict.fromkeys(base + ["pre_call", "post_call"])),
            )
        elif isinstance(hook, list):
            normalized = [getattr(h, "value", h) for h in hook]
            self.event_hook = cast(
                list[GuardrailEventHooks],
                list(dict.fromkeys(normalized + ["pre_call", "post_call"])),
            )
        self._engine: Any = None
        self._engine_key: Any = None
        self._lock = asyncio.Lock()

    def _languages(self) -> list[str]:
        langs = [lang.strip() for lang in self.languages.split(",") if lang.strip()]
        return langs or ["en"]

    @staticmethod
    def _parse_block_entities(raw: Any) -> list[str]:
        if isinstance(raw, str):
            return [e.strip() for e in raw.split(",") if e.strip()]
        if isinstance(raw, (list, tuple)):
            return [str(e).strip() for e in raw if str(e).strip()]
        return []

    async def _engine_for(self, languages: list[str]) -> Any:
        from privaite.config.schema import (
            AnonymizationConfig,
            DeanonymizationConfig,
            DetectorsConfig,
            PIIConfig,
            PresidioDetectorConfig,
        )
        from privaite.pii.engine import PIIEngine

        key = (self.preset, tuple(languages), self.deanonymize, tuple(self.block_entities))
        if self._engine is not None and self._engine_key == key:
            return self._engine

        async with self._lock:
            if self._engine is not None and self._engine_key == key:
                return self._engine

            if self.block_entities and "block_entities" not in PIIConfig.model_fields:
                # PIIConfig uses extra="allow", so an older privaite would silently
                # swallow block_entities and forward the PII anyway. Fail closed
                # rather than give a false sense of a policy gate that is not there.
                raise RuntimeError(
                    "block_entities is set but the installed privaite does not "
                    "support it; upgrade privaite to a version that enforces "
                    "pii.block_entities"
                )

            config = PIIConfig(
                enabled=True,
                preset=self.preset,
                detectors=DetectorsConfig(
                    presidio=PresidioDetectorConfig(enabled=True, languages=languages)
                ),
                anonymization=AnonymizationConfig(method="placeholder"),
                deanonymization=DeanonymizationConfig(enabled=self.deanonymize),
                block_entities=self.block_entities,
            )
            engine = PIIEngine(config)
            try:
                await engine.initialize()
            except OSError:
                # spaCy models not present yet: download them once, then retry.
                # The download is synchronous pip machinery pulling hundreds of
                # MB; run it off the event loop so it does not stall every other
                # request in this proxy worker.
                from spacy.cli import download

                for lang in languages:
                    model = _LANG_MODELS.get(lang)
                    if model:
                        await asyncio.to_thread(download, model)
                engine = PIIEngine(config)
                await engine.initialize()

            self._engine = engine
            self._engine_key = key
            return engine

    def _overwrite_snapshot_field(self, data: dict, field: str, new_value: Any) -> None:
        # A plain `data[field] = ...` rebind leaks for top-level string fields
        # (`input`, `suffix`): the proxy snapshots the request body by
        # shallow-copying data before this hook, so the original string stays in
        # proxy_server_request.body[field]. Overwrite the snapshot copy too.
        # (Lists and dicts are mutated in place, so the aliased snapshot already
        # reflects the anonymized values.)
        psr = data.get("proxy_server_request")
        body = psr.get("body") if isinstance(psr, dict) else None
        if isinstance(body, dict) and field in body:
            body[field] = new_value

    @staticmethod
    def _has_aux_fields(data: dict) -> bool:
        """True when the request carries one of the auxiliary text fields
        scanned by _scrub_aux_fields; keeps the pre-call early return from
        skipping a request whose only user text sits in those fields."""
        prediction = data.get("prediction")
        if isinstance(prediction, dict) and "content" in prediction:
            return True
        web_search = data.get("web_search_options")
        if isinstance(web_search, dict) and "user_location" in web_search:
            return True
        suffix = data.get("suffix")
        if isinstance(suffix, str) and suffix:
            return True
        # /v1/completions user text: a string prompt or the batch list shape.
        # (A dict prompt is the Responses template, handled by _prompt_variables.)
        prompt = data.get("prompt")
        return isinstance(prompt, (str, list)) and bool(prompt)

    async def _scrub_aux_fields(self, data: dict, engine: Any, mapping: Any) -> bool:
        """Scrub the request-side text fields outside messages/input that
        LiteLLM forwards verbatim (core 0.3.3 parity): chat `prediction.content`
        (predicted outputs carry the client's current document),
        `web_search_options.user_location`, and the completions `prompt` (string
        or batch list) and `suffix`. Every value flows through the engine's
        process_request_value, the same single
        choke point, so the block gate and the fail-closed policy apply
        unchanged. Request inputs only, nothing to restore. The prediction and
        web_search_options dicts are aliased by the proxy's shallow body
        snapshot, so their keys are rebound on the SAME dict; `suffix` is a
        top-level string, so its snapshot copy is overwritten explicitly.
        Returns True when anything was scanned."""
        scanned = False
        prediction = data.get("prediction")
        if isinstance(prediction, dict) and "content" in prediction:
            scanned = True
            prediction["content"] = await engine.process_request_value(
                prediction["content"], mapping
            )
        web_search = data.get("web_search_options")
        if isinstance(web_search, dict) and "user_location" in web_search:
            scanned = True
            web_search["user_location"] = await engine.process_request_value(
                web_search["user_location"], mapping
            )
        suffix = data.get("suffix")
        if isinstance(suffix, str) and suffix:
            scanned = True
            new_suffix = await engine.process_request_value(suffix, mapping)
            data["suffix"] = new_suffix
            self._overwrite_snapshot_field(data, "suffix", new_suffix)
        # /v1/completions prompt: a string, or a list for the batch shape. The
        # walker scrubs string leaves only, so a tokenized (integer-array)
        # prompt passes through unchanged, keeping the documented "tokenized
        # inputs are not scanned" boundary. A dict prompt is the Responses
        # template, already handled via _prompt_variables.
        prompt = data.get("prompt")
        if isinstance(prompt, str) and prompt:
            scanned = True
            new_prompt = await engine.process_request_value(prompt, mapping)
            data["prompt"] = new_prompt
            self._overwrite_snapshot_field(data, "prompt", new_prompt)
        elif isinstance(prompt, list) and prompt:
            scanned = True
            # Slot-wise rewrite of the SAME list object: the body snapshot
            # aliases it, so the anonymized entries land in the snapshot too.
            prompt[:] = await engine.process_request_value(prompt, mapping)
        return scanned

    def _has_gated_fields(self, data: dict) -> bool:
        """True when the request carries an agent-prompt field the block gate
        must read; keeps the pre-call early return from skipping a request whose
        only text sits there. False unless block_entities is configured, since
        nothing reads those fields otherwise."""
        return bool(self.block_entities) and any(
            data.get(field) for field in _GATED_VERBATIM_FIELDS
        )

    async def _gate_verbatim_fields(self, data: dict, engine: Any) -> None:
        """Apply the block_entities gate to the request fields relayed verbatim:
        the agent's own prompt (Responses `instructions`, Anthropic `system`).

        Mirrors PIIEngine.gate_document on the core side: the field goes through
        the same choke point and the scrubbed copy is thrown away, so a blocked
        type rejects the whole request while the prompt still reaches the model
        unchanged. Without this, an operator who sets block_entities gets a 200
        whenever the blocked type sits in the prompt instead of a message.
        No-op unless block_entities is configured, so the default posture reads
        nothing. (scrub_document and not gate_document: it is the gate, and it
        works against every privaite release this guardrail already requires.)
        """
        if not self.block_entities:
            return
        for field in _GATED_VERBATIM_FIELDS:
            value = data.get(field)
            if value:
                await engine.scrub_document(value)

    @staticmethod
    def _prompt_variables(data: dict) -> dict | None:
        """Responses prompt-template variables carry user data (the template
        id/version do not). None when the request has no scannable variables;
        /v1/completions sends `prompt` as a string, which is not this surface."""
        prompt = data.get("prompt")
        if isinstance(prompt, dict) and isinstance(prompt.get("variables"), dict):
            return prompt["variables"]
        return None

    async def _anonymize_request(self, data: dict, engine: Any) -> Any:
        """Anonymize chat `messages` AND the Responses request surface (`input`
        plus `prompt.variables`) in place, sharing ONE mapping so no source is
        left untouched when a crafted request carries several. Responses items
        are scanned by the gateway's item scrubber, so the two integrations
        cover the exact same surface (role content, tool calls and outputs
        including list-of-parts, typed action carriers, mcp fields; opaque and
        binary payloads relayed whole). Every scanned string still flows through
        the engine's single choke point, so the block gate and the fail-closed
        policy apply unchanged. Returns the mapping, or None if there was
        nothing to anonymize."""
        # privaite.gateway.scrub is the reference implementation for WHAT a
        # Responses request exposes (AGENTS.md: integrations stay in sync with
        # the core). Reusing its scrubbers, private as they are, beats a local
        # mirror that would silently drift behind the next gateway hardening.
        from privaite.gateway.scrub import _scrub_data_value, _scrub_responses_item
        from privaite.pii.mapping import PIIMapping

        # Gate first: a blocked type in the agent's own prompt rejects the
        # request without having touched the caller's dict.
        await self._gate_verbatim_fields(data, engine)

        messages = data.get("messages")
        msg_list = messages if isinstance(messages, list) else []
        scanned = bool(msg_list)
        if msg_list:
            anonymized, mapping = await engine.process_request(msg_list)
            # msg_list is data["messages"] (same object) -> mutate it in place so
            # the proxy's shallow body snapshot holds the anonymized copy too.
            msg_list[:] = anonymized
        else:
            mapping = PIIMapping()

        input_value = data.get("input")
        if isinstance(input_value, str) and input_value:
            scanned = True
            new_text, _ = await engine.scrub_document(input_value, mapping)
            data["input"] = new_text
            self._overwrite_snapshot_field(data, "input", new_text)
        elif isinstance(input_value, list):
            scanned = True
            # Slot-by-slot rewrite of the SAME list object: the body snapshot
            # aliases it, so the anonymized items land in the snapshot too.
            for idx, item in enumerate(input_value):
                input_value[idx] = await _scrub_responses_item(engine, item, mapping)

        variables = self._prompt_variables(data)
        if variables is not None:
            scanned = True
            # Rebind the key on the SAME prompt dict (aliased by the snapshot).
            data["prompt"]["variables"] = {
                key: await _scrub_data_value(engine, value, mapping)
                for key, value in variables.items()
            }

        if await self._scrub_aux_fields(data, engine, mapping):
            scanned = True

        return mapping if scanned else None

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict, call_type: str
    ) -> dict:
        from privaite.pii.engine import PIIBlockedError

        # metadata is caller-controlled at the proxy boundary, so never trust an
        # incoming privaite_map: this hook is the only authority that may set it.
        # Clearing it here means the post-call hook can only ever restore from a
        # map produced by this guardrail's pre-call pass on the same request.
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            metadata.pop("privaite_map", None)

        if (
            not data.get("messages")
            and not data.get("input")
            and self._prompt_variables(data) is None
            and not self._has_aux_fields(data)
            and not self._has_gated_fields(data)
        ):
            return data

        engine = await self._engine_for(self._languages())
        try:
            mapping = await self._anonymize_request(data, engine)
        except PIIBlockedError as exc:
            # A blocked PII type was found: reject the request outright with a 400,
            # forwarding nothing. The message names TYPES only, never the values.
            from fastapi import HTTPException

            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

        if mapping is not None and self.deanonymize and not mapping.is_empty:
            # Carry a plain fake->original dict to the post-call hook on the same
            # request, the same channel LiteLLM's Presidio guardrail uses. The
            # post-call hook pops it again so it does not linger in metadata.
            data.setdefault("metadata", {})["privaite_map"] = dict(mapping.get_all_fakes())
        return data

    async def _restore_audio_transcript(self, message: Any, engine: Any, mapping: Any) -> None:
        """Restore the audio transcript on a response message (restore parity
        with the core: audio replies carry their text in audio.transcript, and
        the model echoes placeholders there like anywhere else)."""
        audio = getattr(message, "audio", None)
        if audio is None:
            return
        transcript = _obj_get(audio, "transcript")
        if isinstance(transcript, str) and transcript:
            _obj_set(audio, "transcript", await engine.process_response(transcript, mapping))

    async def _restore_message(self, message: Any, engine: Any, mapping: Any) -> None:
        """Restore originals in one response message: content, reasoning trace,
        the refusal, the audio transcript, tool-call args and the legacy
        function_call args (restore parity with the core proxy)."""
        content = getattr(message, "content", None)
        if isinstance(content, str) and content:
            message.content = await engine.process_response(content, mapping)
        # A refusal can quote the request, so it carries placeholders too.
        for field in ("reasoning_content", "reasoning", "refusal"):
            value = getattr(message, field, None)
            if isinstance(value, str) and value:
                setattr(message, field, await engine.process_response(value, mapping))
        await self._restore_audio_transcript(message, engine, mapping)
        for tool_call in getattr(message, "tool_calls", None) or []:
            fn = getattr(tool_call, "function", None)
            if fn is None:
                continue
            args = getattr(fn, "arguments", None)
            if args:
                fn.arguments = await _restore_arguments(engine, args, mapping)
        function_call = getattr(message, "function_call", None)
        if function_call is not None:
            fc_args = getattr(function_call, "arguments", None)
            if fc_args:
                function_call.arguments = await _restore_arguments(engine, fc_args, mapping)

    async def async_post_call_success_hook(
        self, data: dict, user_api_key_dict: Any, response: Any
    ) -> Any:
        if not self.deanonymize:
            return response
        # pop (not get): consume the reversible map so the originals do not linger
        # in request metadata, which the proxy may persist to spend logs.
        fakes = (data.get("metadata") or {}).pop("privaite_map", None)
        if not fakes:
            return response

        from privaite.pii.mapping import PIIMapping

        mapping = PIIMapping()
        for fake, original in fakes.items():
            mapping.add(original, fake, "PII")

        engine = await self._engine_for(self._languages())
        for choice in getattr(response, "choices", None) or []:
            message = getattr(choice, "message", None)
            if message is not None:
                await self._restore_message(message, engine, mapping)
        # Responses API results carry text under `output`, not `choices`.
        await self._restore_responses_output(response, engine, mapping)
        return response

    async def _restore_responses_output(self, response: Any, engine: Any, mapping: Any) -> None:
        """Restore originals in a Responses API result: output_text content
        blocks and any function_call output-item arguments (dict or object)."""
        for item in getattr(response, "output", None) or []:
            for block in _obj_get(item, "content") or []:
                text = _obj_get(block, "text")
                if isinstance(text, str) and text:
                    _obj_set(block, "text", await engine.process_response(text, mapping))
            args = _obj_get(item, "arguments")
            if isinstance(args, str) and args:
                _obj_set(item, "arguments", await _restore_arguments(engine, args, mapping))

    def _restore_delta_audio(self, delta: Any, index: int, finished: bool, restore) -> None:
        """Feed streamed audio transcript fragments through their own restore
        buffer and flush the held tail on the finish chunk, creating the audio
        carrier when the finish delta has none so the tail is never dropped."""
        audio = getattr(delta, "audio", None)
        fragment = _obj_get(audio, "transcript") if audio is not None else None
        if not isinstance(fragment, str):
            fragment = ""
        if not fragment and not finished:
            return
        restored = restore(("audio", index), fragment, finished)
        if audio is not None:
            _obj_set(audio, "transcript", restored)
        elif restored:
            _obj_set(delta, "audio", {"transcript": restored})

    def _restore_delta(self, delta: Any, index: int, finished: bool, restore) -> None:
        """Restore one streamed delta in place: text content, the reasoning
        trace, the refusal, the audio transcript, streamed tool-call argument
        fragments (per tool_call index) and the legacy function_call."""
        content = getattr(delta, "content", None) or ""
        restored = restore(("content", index), content, finished)
        if content or finished:
            delta.content = restored
        for field in ("reasoning_content", "reasoning", "refusal"):
            value = getattr(delta, field, None)
            if isinstance(value, str) and (value or finished):
                setattr(delta, field, restore((field, index), value, finished))
        self._restore_delta_audio(delta, index, finished, restore)
        for tool_call in getattr(delta, "tool_calls", None) or []:
            fn = getattr(tool_call, "function", None)
            if fn is None:
                continue
            args = getattr(fn, "arguments", None)
            if args:
                tc_index = getattr(tool_call, "index", 0) or 0
                # Pass `finished` so a fragment that arrives on the same chunk as
                # finish_reason flushes its held-back tail instead of dropping it.
                # json_fragment: the fragment is JSON source text, so the restored
                # original must be spliced in JSON-escaped.
                fn.arguments = restore(("tool", index, tc_index), args, finished, True)
        function_call = getattr(delta, "function_call", None)
        if function_call is not None:
            fc_args = getattr(function_call, "arguments", None)
            if fc_args:
                function_call.arguments = restore(("fc", index), fc_args, finished, True)

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict: Any, response: Any, request_data: dict
    ) -> Any:
        # pop (not get): consume the map so the originals do not linger in metadata.
        fakes = (request_data.get("metadata") or {}).pop("privaite_map", None)
        if not self.deanonymize or not fakes:
            async for chunk in response:
                yield chunk
            return

        from privaite.pii.mapping import PIIMapping
        from privaite.streaming.buffer import StreamingDeAnonymizer

        mapping = PIIMapping()
        # Second mapping for the argument channels: their fragments are JSON
        # source text, so the original has to arrive JSON-escaped or the
        # client's parse of the reassembled arguments fails on a quote, a
        # backslash or a newline. (Placeholders need no escaping, so the fakes
        # are the same on both sides.)
        json_mapping = PIIMapping()
        for fake, original in fakes.items():
            mapping.add(original, fake, "PII")
            json_mapping.add(_json_escape(original), fake, "PII")

        restorer = _StreamRestorer(mapping, json_mapping, StreamingDeAnonymizer)
        async for chunk in response:
            restorer.restore_chunk(chunk, self._restore_delta)
            yield chunk
        for drained in restorer.drain():
            yield drained

    async def async_post_call_failure_hook(
        self,
        request_data: Any,
        original_exception: Any,
        user_api_key_dict: Any,
        traceback_str: Any = None,
    ) -> Any:
        # The success/streaming hooks pop the reversible map after restoring, but
        # they never run when the call fails. Drop it here too so the originals
        # are not left in metadata for a failure spend-log to persist. (Recent
        # LiteLLM also strips "privaite_map" from the spend-log body itself; this
        # is the best-effort defense for a stock install that predates that.)
        metadata = request_data.get("metadata") if isinstance(request_data, dict) else None
        if isinstance(metadata, dict):
            metadata.pop("privaite_map", None)
        return None
