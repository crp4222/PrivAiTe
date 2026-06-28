"""
PrivAiTe guardrail for the LiteLLM proxy.

Runs PrivAiTe's engine in-process inside LiteLLM. The pre-call hook anonymizes the
request and stashes the reversible map in the request metadata (consumed and popped
by the post-call hook); the post-call hook restores the real values in the response,
including inside tool-call arguments and the legacy function_call, which LiteLLM's
built-in Presidio guardrail does not touch. Streaming is restored too: text
content, streamed tool-call arguments, and the streamed function_call.

It reuses PrivAiTe's engine, so there is no detection or masking logic here.

Usage: mount this file next to your LiteLLM config.yaml and reference it by
dot-notation. See integrations/litellm/README.md.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.types.guardrails import GuardrailEventHooks

_LANG_MODELS = {
    "en": "en_core_web_lg",
    "fr": "fr_core_news_md",
    "de": "de_core_news_md",
    "es": "es_core_news_md",
    "it": "it_core_news_md",
    "pt": "pt_core_news_md",
    "nl": "nl_core_news_md",
}


class PrivaiteGuardrail(CustomGuardrail):
    def __init__(self, **kwargs: Any) -> None:
        self.preset = kwargs.pop("preset", None) or "onnx"
        if self.preset not in ("light", "onnx"):
            self.preset = "onnx"
        self.languages = kwargs.pop("languages", None) or "en,fr"
        deanon = kwargs.pop("deanonymize", True)
        self.deanonymize = (
            deanon if isinstance(deanon, bool)
            else str(deanon).strip().lower() not in ("false", "0", "no", "")
        )
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

    async def _engine_for(self, languages: list[str]) -> Any:
        from privaite.config.schema import (
            AnonymizationConfig,
            DeanonymizationConfig,
            DetectorsConfig,
            PIIConfig,
            PresidioDetectorConfig,
        )
        from privaite.pii.engine import PIIEngine

        key = (self.preset, tuple(languages), self.deanonymize)
        if self._engine is not None and self._engine_key == key:
            return self._engine

        async with self._lock:
            if self._engine is not None and self._engine_key == key:
                return self._engine

            config = PIIConfig(
                enabled=True,
                preset=self.preset,
                detectors=DetectorsConfig(
                    presidio=PresidioDetectorConfig(enabled=True, languages=languages)
                ),
                anonymization=AnonymizationConfig(method="placeholder"),
                deanonymization=DeanonymizationConfig(enabled=self.deanonymize),
            )
            engine = PIIEngine(config)
            try:
                await engine.initialize()
            except OSError:
                # spaCy models not present yet: download them once, then retry.
                from spacy.cli import download

                for lang in languages:
                    model = _LANG_MODELS.get(lang)
                    if model:
                        download(model)
                engine = PIIEngine(config)
                await engine.initialize()

            self._engine = engine
            self._engine_key = key
            return engine

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict, call_type: str
    ) -> dict:
        # metadata is caller-controlled at the proxy boundary, so never trust an
        # incoming privaite_map: this hook is the only authority that may set it.
        # Clearing it here means the post-call hook can only ever restore from a
        # map produced by this guardrail's pre-call pass on the same request.
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            metadata.pop("privaite_map", None)

        messages = data.get("messages")
        if not messages:
            return data

        engine = await self._engine_for(self._languages())
        anonymized, mapping = await engine.process_request(messages)
        # Mutate the list in place rather than rebinding data["messages"]: the
        # proxy snapshots the request body by shallow-copying data BEFORE this
        # hook runs, so a rebind would leave the original raw-PII messages in
        # that snapshot (and thus in spend logs). An in-place update keeps the
        # snapshot pointing at the anonymized messages.
        if isinstance(messages, list):
            messages[:] = anonymized
        else:
            data["messages"] = anonymized

        if self.deanonymize and not mapping.is_empty:
            # Carry a plain fake->original dict to the post-call hook on the same
            # request, the same channel LiteLLM's Presidio guardrail uses. The
            # post-call hook pops it again so it does not linger in metadata.
            data.setdefault("metadata", {})["privaite_map"] = dict(mapping.get_all_fakes())
        return data

    async def _restore_message(self, message: Any, engine: Any, mapping: Any) -> None:
        """Restore originals in one response message: content, tool-call args and
        the legacy function_call args."""
        content = getattr(message, "content", None)
        if isinstance(content, str) and content:
            message.content = await engine.process_response(content, mapping)
        for tool_call in (getattr(message, "tool_calls", None) or []):
            fn = getattr(tool_call, "function", None)
            if fn is None:
                continue
            args = getattr(fn, "arguments", None)
            if args:
                fn.arguments = await engine.process_response(args, mapping)
        function_call = getattr(message, "function_call", None)
        if function_call is not None:
            fc_args = getattr(function_call, "arguments", None)
            if fc_args:
                function_call.arguments = await engine.process_response(fc_args, mapping)

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
        for choice in (getattr(response, "choices", None) or []):
            message = getattr(choice, "message", None)
            if message is not None:
                await self._restore_message(message, engine, mapping)
        return response

    def _restore_delta(self, delta: Any, index: int, finished: bool, restore) -> None:
        """Restore one streamed delta in place: text content, streamed tool-call
        argument fragments (per tool_call index) and the legacy function_call."""
        content = getattr(delta, "content", None) or ""
        restored = restore(("content", index), content, finished)
        if content or finished:
            delta.content = restored
        for tool_call in (getattr(delta, "tool_calls", None) or []):
            fn = getattr(tool_call, "function", None)
            if fn is None:
                continue
            args = getattr(fn, "arguments", None)
            if args:
                tc_index = getattr(tool_call, "index", 0) or 0
                fn.arguments = restore(("tool", index, tc_index), args, False)
        function_call = getattr(delta, "function_call", None)
        if function_call is not None:
            fc_args = getattr(function_call, "arguments", None)
            if fc_args:
                function_call.arguments = restore(("fc", index), fc_args, False)

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
        for fake, original in fakes.items():
            mapping.add(original, fake, "PII")

        # One de-anonymizer buffer per streamed segment, keyed by (kind, choice
        # index, ...). With n>1 the provider interleaves chunks for different
        # choices, and tool-call arguments stream as fragments per tool_call
        # index; each segment keeps its own boundary buffer so a placeholder split
        # across chunks reassembles without mixing segments.
        buffers: dict[tuple, StreamingDeAnonymizer] = {}

        def _restore(key: tuple, text: str, finished: bool) -> str:
            deanon = buffers.get(key)
            if deanon is None:
                deanon = buffers[key] = StreamingDeAnonymizer(mapping)
            out = deanon.feed(text) if text else ""
            if finished:
                out += deanon.flush()
            return out

        # The buffer holds back partial placeholders that span chunk boundaries.
        # Whatever remains is flushed onto the chunk that carries finish_reason.
        async for chunk in response:
            for choice in (getattr(chunk, "choices", None) or []):
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                index = getattr(choice, "index", 0) or 0
                finished = getattr(choice, "finish_reason", None) is not None
                self._restore_delta(delta, index, finished, _restore)
            yield chunk
