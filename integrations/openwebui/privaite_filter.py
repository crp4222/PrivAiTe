"""
title: PrivAiTe PII Anonymizer
author: crp4222
author_url: https://github.com/crp4222/PrivAiTe
version: 0.1.6
required_open_webui_version: 0.5.0
requirements: privaite>=0.2.12
description: Anonymize or block PII (text, tool calls, multimodal) before it reaches the provider.
"""

# Runs PrivAiTe's engine in-process inside Open WebUI. inlet() anonymizes the
# outgoing request and stashes the mapping in __metadata__; outlet() reads that
# mapping back and restores the real values in the assistant reply.
#
# Note: this pulls Presidio and spaCy into Open WebUI's environment and downloads
# the spaCy models for the chosen languages on first use (en_core_web_lg alone is
# ~560MB). The default "onnx" preset also downloads the Privacy Filter model on
# first use, so the first request after enabling it can be slow. Set the preset
# valve to "light" to skip the ONNX model, or run PrivAiTe as a standalone proxy
# and point your connection at it instead.

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_LANG_MODELS = {
    "en": "en_core_web_lg",
    "fr": "fr_core_news_md",
    "de": "de_core_news_md",
    "es": "es_core_news_md",
    "it": "it_core_news_md",
    "pt": "pt_core_news_md",
    "nl": "nl_core_news_md",
}


class Filter:
    class Valves(BaseModel):
        preset: str = Field(default="onnx", description="'onnx' (full, secrets) or 'light' (fast)")
        languages: str = Field(default="en,fr", description="Comma-separated spaCy languages.")
        deanonymize: bool = Field(default=True, description="Restore PII in the response.")
        block_entities: str = Field(
            default="",
            description="Comma-separated PII TYPES to reject outright, e.g. "
            "US_SSN,CREDIT_CARD. Empty (default) masks everything instead.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._engine: Any = None
        self._engine_key: Any = None
        self._lock: Any = None

    def _languages(self) -> list[str]:
        return [lang.strip() for lang in self.valves.languages.split(",") if lang.strip()]

    def _block_entities(self) -> list[str]:
        return [e.strip() for e in self.valves.block_entities.split(",") if e.strip()]

    async def _engine_for(self, languages: list[str]):
        import asyncio

        from privaite.config.schema import (
            AnonymizationConfig,
            DeanonymizationConfig,
            DetectorsConfig,
            PIIConfig,
            PresidioDetectorConfig,
        )
        from privaite.pii.engine import PIIEngine

        preset = self.valves.preset if self.valves.preset in ("light", "onnx") else "light"
        block_entities = self._block_entities()
        key = (preset, tuple(languages), self.valves.deanonymize, tuple(block_entities))
        if self._engine is not None and self._engine_key == key:
            return self._engine

        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            # Two concurrent first requests must not both build engines (and
            # both trigger the model download below).
            if self._engine is not None and self._engine_key == key:
                return self._engine

            if block_entities and "block_entities" not in PIIConfig.model_fields:
                # PIIConfig uses extra="allow", so an older privaite would silently
                # swallow block_entities and forward the PII anyway. Fail closed
                # rather than pretend a policy gate is in force when it is not.
                raise RuntimeError(
                    "block_entities is set but the installed privaite does not "
                    "support it; upgrade privaite to a version that enforces "
                    "pii.block_entities"
                )

            config = PIIConfig(
                enabled=True,
                preset=preset,
                detectors=DetectorsConfig(
                    presidio=PresidioDetectorConfig(enabled=True, languages=languages or ["en"])
                ),
                anonymization=AnonymizationConfig(method="placeholder"),
                deanonymization=DeanonymizationConfig(enabled=self.valves.deanonymize),
                block_entities=block_entities,
            )
            engine = PIIEngine(config)
            try:
                await engine.initialize()
            except OSError:
                # spaCy models not present yet: download them once, then retry.
                # The download is synchronous pip machinery pulling hundreds of
                # MB; run it off the event loop or every request in the process
                # stalls behind it.
                from spacy.cli import download

                for lang in languages or ["en"]:
                    model = _LANG_MODELS.get(lang)
                    if model:
                        await asyncio.to_thread(download, model)
                engine = PIIEngine(config)
                await engine.initialize()

            self._engine = engine
            self._engine_key = key
            return engine

    async def inlet(self, body: dict, __metadata__: dict | None = None) -> dict:
        # Metadata may round-trip through Open WebUI's storage and back from the
        # client, so never trust an incoming privaite_map (it could rewrite the
        # reply to attacker-chosen text) and never assume it is ephemeral.
        if __metadata__ is not None:
            __metadata__.pop("privaite_map", None)

        messages = body.get("messages")
        if not messages:
            return body

        from privaite.pii.engine import PIIBlockedError

        engine = await self._engine_for(self._languages())
        try:
            anonymized, mapping = await engine.process_request(messages)
        except PIIBlockedError as exc:
            # A blocked PII type was found: refuse the request. Open WebUI surfaces
            # the message to the user; it names TYPES only, never the values.
            raise Exception(str(exc)) from exc
        body["messages"] = anonymized

        # Only stash when outlet will consume (and pop) it: with restore off the
        # originals would sit in metadata forever for nothing.
        if __metadata__ is not None and self.valves.deanonymize and not mapping.is_empty:
            __metadata__["privaite_map"] = dict(mapping.get_all_fakes())
        return body

    async def outlet(self, body: dict, __metadata__: dict | None = None) -> dict:
        if __metadata__ is None:
            return body
        # pop (not get): Open WebUI may persist message metadata, and the map
        # holds the ORIGINAL values. Consume it so it cannot reach storage.
        fakes = __metadata__.pop("privaite_map", None)
        if not self.valves.deanonymize or not fakes:
            return body

        from privaite.pii.mapping import PIIMapping

        mapping = PIIMapping()
        for fake, original in fakes.items():
            mapping.add(original, fake, "PII")

        engine = await self._engine_for(self._languages())
        for message in body.get("messages", []):
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = await engine.process_response(content, mapping)
            elif isinstance(content, list):
                # Multimodal replies: restore each text part.
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        part["text"] = await engine.process_response(part["text"], mapping)
            for field in ("reasoning_content", "reasoning"):
                value = message.get(field)
                if isinstance(value, str) and value:
                    message[field] = await engine.process_response(value, mapping)
            tool_calls = message.get("tool_calls")
            if tool_calls:
                message["tool_calls"] = await engine.process_response_tool_calls(
                    tool_calls, mapping
                )
            function_call = message.get("function_call")
            if function_call:
                message["function_call"] = await engine.process_response_function_call(
                    function_call, mapping
                )
            # Open WebUI >= 0.10 stores the reply as structured `output` items
            # ({"type": "message"|"reasoning", "content": [{"type": "output_text",
            # "text": ...}]}) and leaves message["content"] empty, so the branches
            # above are a no-op and the user would see placeholders. Restore the
            # text inside those items too. Older Open WebUI has no "output" key, so
            # this simply does not fire and the "content" path above handles it.
            output = message.get("output")
            if isinstance(output, list):
                restored_output = await self._restore_output_items(output, engine, mapping)
                if restored_output is not None:
                    message["output"] = restored_output
        return body

    async def _restore_output_items(
        self, output: list[Any], engine: Any, mapping: Any
    ) -> list[Any] | None:
        # Return a NEW output list when a value changed and None otherwise: Open
        # WebUI decides whether to persist/emit the restored reply by comparing
        # the output object against the stored one, so an in-place edit would be
        # seen as unchanged and dropped. Text parts and function-call arguments
        # can carry placeholders; every other node is carried over by reference.
        changed = False
        new_output: list[Any] = []
        for item in output:
            if not isinstance(item, dict):
                new_output.append(item)
                continue
            new_parts: list[Any] = []
            item_changed = False
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if (
                        isinstance(part, dict)
                        and isinstance(part.get("text"), str)
                        and part["text"]
                    ):
                        restored = await engine.process_response(part["text"], mapping)
                        if restored != part["text"]:
                            part = {**part, "text": restored}
                            item_changed = True
                    new_parts.append(part)
            if item_changed:
                item = {**item, "content": new_parts}
            arguments = item.get("arguments")
            if isinstance(arguments, str) and arguments:
                restored = await engine.process_response(arguments, mapping)
                if restored != arguments:
                    item = {**item, "arguments": restored}
                    item_changed = True
            if item_changed:
                changed = True
            new_output.append(item)
        return new_output if changed else None
