"""
title: PrivAiTe PII Anonymizer
author: crp4222
author_url: https://github.com/crp4222/PrivAiTe
version: 0.1.2
required_open_webui_version: 0.5.0
requirements: privaite>=0.2.4
description: Anonymize PII (text, tool calls, multimodal) before requests reach the provider.
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

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._engine = None
        self._engine_key = None

    def _languages(self) -> list[str]:
        return [lang.strip() for lang in self.valves.languages.split(",") if lang.strip()]

    async def _engine_for(self, languages: list[str]):
        from privaite.config.schema import (
            AnonymizationConfig,
            DeanonymizationConfig,
            DetectorsConfig,
            PIIConfig,
            PresidioDetectorConfig,
        )
        from privaite.pii.engine import PIIEngine

        preset = self.valves.preset if self.valves.preset in ("light", "onnx") else "light"
        key = (preset, tuple(languages), self.valves.deanonymize)
        if self._engine is not None and self._engine_key == key:
            return self._engine

        config = PIIConfig(
            enabled=True,
            preset=preset,
            detectors=DetectorsConfig(
                presidio=PresidioDetectorConfig(enabled=True, languages=languages or ["en"])
            ),
            anonymization=AnonymizationConfig(method="placeholder"),
            deanonymization=DeanonymizationConfig(enabled=self.valves.deanonymize),
        )
        engine = PIIEngine(config)
        try:
            await engine.initialize()
        except OSError:
            # spaCy models not present yet: download them once, then retry.
            from spacy.cli import download

            for lang in languages or ["en"]:
                model = _LANG_MODELS.get(lang)
                if model:
                    download(model)
            engine = PIIEngine(config)
            await engine.initialize()

        self._engine = engine
        self._engine_key = key
        return engine

    async def inlet(self, body: dict, __metadata__: dict | None = None) -> dict:
        messages = body.get("messages")
        if not messages:
            return body

        engine = await self._engine_for(self._languages())
        anonymized, mapping = await engine.process_request(messages)
        body["messages"] = anonymized

        if __metadata__ is not None and not mapping.is_empty:
            # Stash a plain fake->original dict; it survives to outlet on the same request.
            __metadata__["privaite_map"] = dict(mapping.get_all_fakes())
        return body

    async def outlet(self, body: dict, __metadata__: dict | None = None) -> dict:
        if not self.valves.deanonymize or __metadata__ is None:
            return body
        fakes = __metadata__.get("privaite_map")
        if not fakes:
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
        return body
