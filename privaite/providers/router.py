from __future__ import annotations

import logging
from typing import Any

import litellm

from privaite.config.schema import ProviderConfig

logger = logging.getLogger("privaite.providers.router")


class ProviderRouter:
    def __init__(self, providers: list[ProviderConfig]) -> None:
        self._model_map: dict[str, dict[str, Any]] = {}
        for p in providers:
            params = p.litellm_params.model_dump(exclude_none=True)
            self._model_map[p.model_name] = params
            logger.info("Registered model alias: %s -> %s", p.model_name, params.get("model"))

    @property
    def models(self) -> list[str]:
        return list(self._model_map.keys())

    def has_model(self, alias: str) -> bool:
        return alias in self._model_map

    def resolve_model(self, alias: str) -> dict[str, Any]:
        if alias not in self._model_map:
            raise KeyError(f"Model alias '{alias}' not configured")
        return dict(self._model_map[alias])

    async def completion(
        self, model_alias: str, messages: list[dict], **kwargs: Any
    ) -> Any:
        params = self.resolve_model(model_alias)
        model = params.pop("model")
        return await litellm.acompletion(
            model=model, messages=messages, **params, **kwargs
        )

    async def streaming_completion(
        self, model_alias: str, messages: list[dict], **kwargs: Any
    ) -> Any:
        params = self.resolve_model(model_alias)
        model = params.pop("model")
        return await litellm.acompletion(
            model=model, messages=messages, stream=True, **params, **kwargs
        )

    async def embedding(
        self, model_alias: str, input_text: str | list[str], **kwargs: Any
    ) -> Any:
        params = self.resolve_model(model_alias)
        model = params.pop("model")
        return await litellm.aembedding(
            model=model, input=input_text, **params, **kwargs
        )
