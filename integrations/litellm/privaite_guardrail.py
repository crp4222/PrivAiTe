"""
PrivAiTe guardrail for the LiteLLM proxy.

Runs PrivAiTe's engine in-process inside LiteLLM. The pre-call hook anonymizes the
request and stashes the reversible map in the request metadata; the post-call hook
restores the real values in the response, including inside tool-call arguments and
the legacy function_call, which LiteLLM's built-in Presidio guardrail does not
touch. Streaming responses are restored too.

It reuses PrivAiTe's engine, so there is no detection or masking logic here.

Usage: mount this file next to your LiteLLM config.yaml and reference it by
dot-notation. See integrations/litellm/README.md.
"""

from __future__ import annotations

import asyncio
from typing import Any

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
        messages = data.get("messages")
        if not messages:
            return data

        engine = await self._engine_for(self._languages())
        anonymized, mapping = await engine.process_request(messages)
        data["messages"] = anonymized

        if self.deanonymize and not mapping.is_empty:
            # Carry a plain fake->original dict to the post-call hook on the same
            # request, the same channel LiteLLM's Presidio guardrail uses.
            data.setdefault("metadata", {})["privaite_map"] = dict(mapping.get_all_fakes())
        return data

    async def async_post_call_success_hook(
        self, data: dict, user_api_key_dict: Any, response: Any
    ) -> Any:
        if not self.deanonymize:
            return response
        fakes = (data.get("metadata") or {}).get("privaite_map")
        if not fakes:
            return response

        from privaite.pii.mapping import PIIMapping

        mapping = PIIMapping()
        for fake, original in fakes.items():
            mapping.add(original, fake, "PII")

        engine = await self._engine_for(self._languages())
        for choice in (getattr(response, "choices", None) or []):
            message = getattr(choice, "message", None)
            if message is None:
                continue
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
        return response

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict: Any, response: Any, request_data: dict
    ) -> Any:
        fakes = (request_data.get("metadata") or {}).get("privaite_map")
        if not self.deanonymize or not fakes:
            async for chunk in response:
                yield chunk
            return

        from privaite.pii.mapping import PIIMapping
        from privaite.streaming.buffer import StreamingDeAnonymizer

        mapping = PIIMapping()
        for fake, original in fakes.items():
            mapping.add(original, fake, "PII")
        deanon = StreamingDeAnonymizer(mapping)

        # The buffer holds back partial placeholders that span chunk boundaries.
        # Whatever remains is flushed onto the chunk that carries finish_reason.
        async for chunk in response:
            for choice in (getattr(chunk, "choices", None) or []):
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                finished = getattr(choice, "finish_reason", None) is not None
                content = getattr(delta, "content", None) or ""
                restored = deanon.feed(content) if content else ""
                if finished:
                    restored += deanon.flush()
                if content or finished:
                    delta.content = restored
            yield chunk
