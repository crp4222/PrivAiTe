"""Startup and logging hardening: misconfigurations fail fast and loudly, and
log output stays machine-parseable."""

from __future__ import annotations

import json
import logging

import pytest

from privaite.config.schema import LiteLLMParams, PresidioDetectorConfig, ProviderConfig
from privaite.providers.router import ProviderRouter
from privaite.utils.logging import _JsonFormatter


def _provider(alias: str) -> ProviderConfig:
    return ProviderConfig(
        model_name=alias, litellm_params=LiteLLMParams(model=f"openai/{alias}")
    )


def test_duplicate_provider_alias_refused_at_startup():
    # last-wins overwrite silently routed traffic to the wrong provider.
    with pytest.raises(ValueError, match="Duplicate"):
        ProviderRouter([_provider("gpt"), _provider("gpt")])


def test_unique_aliases_register_fine():
    router = ProviderRouter([_provider("a"), _provider("b")])
    assert sorted(router.models) == ["a", "b"]


@pytest.mark.asyncio
async def test_pii_enabled_with_zero_detectors_refused_at_startup():
    # pii.enabled=true with every detector off would serve with detection
    # silently doing nothing; passthrough must be an explicit enabled=false.
    from privaite.config.schema import DetectorsConfig, PIIConfig
    from privaite.pii.engine import PIIEngine

    config = PIIConfig(
        enabled=True,
        preset=None,
        detectors=DetectorsConfig(presidio=PresidioDetectorConfig(enabled=False)),
    )
    with pytest.raises(ValueError, match="no detector"):
        await PIIEngine(config).initialize()


@pytest.mark.asyncio
async def test_unknown_presidio_language_fails_at_init_not_per_request():
    # An unmapped language used to be dropped silently at init and then crash
    # EVERY request when detect() looped over config.languages.
    from privaite.pii.detector_presidio import PresidioDetector

    detector = PresidioDetector(
        PresidioDetectorConfig(enabled=True, languages=["xx"])
    )
    with pytest.raises(ValueError, match="xx"):
        await detector.initialize()


def _format(msg: str) -> str:
    record = logging.LogRecord(
        name="privaite.test", level=logging.INFO, pathname=__file__,
        lineno=1, msg=msg, args=(), exc_info=None,
    )
    return _JsonFormatter().format(record)


def test_json_log_lines_are_valid_json_for_hostile_messages():
    # quotes, newlines and backslashes in a message used to break the JSON line.
    for msg in ('with "quotes"', "multi\nline", "back\\slash", "unicode é"):
        parsed = json.loads(_format(msg))
        assert parsed["message"] == msg
        assert parsed["level"] == "INFO"
