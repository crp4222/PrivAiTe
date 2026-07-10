"""Startup and logging hardening: misconfigurations fail fast and loudly, and
log output stays machine-parseable."""

from __future__ import annotations

import json
import logging
import sys

import pytest

from privaite.config.schema import (
    DetectorsConfig,
    LiteLLMParams,
    PIIConfig,
    PresidioDetectorConfig,
    PrivAiTeConfig,
    ProviderConfig,
)
from privaite.pii.engine import PIIEngine, PIIProcessingError
from privaite.pii.mapping import PIIMapping
from privaite.providers.router import ProviderRouter
from privaite.streaming.handler import StreamingHandler
from privaite.utils.logging import _JsonFormatter, _PrivacySafeTextFormatter


def _provider(alias: str) -> ProviderConfig:
    return ProviderConfig(model_name=alias, litellm_params=LiteLLMParams(model=f"openai/{alias}"))


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

    detector = PresidioDetector(PresidioDetectorConfig(enabled=True, languages=["xx"]))
    with pytest.raises(ValueError, match="xx"):
        await detector.initialize()


def _format(msg: str) -> str:
    record = logging.LogRecord(
        name="privaite.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    return _JsonFormatter().format(record)


def test_json_log_lines_are_valid_json_for_hostile_messages():
    # quotes, newlines and backslashes in a message used to break the JSON line.
    for msg in ('with "quotes"', "multi\nline", "back\\slash", "unicode é"):
        parsed = json.loads(_format(msg))
        assert parsed["message"] == msg
        assert parsed["level"] == "INFO"


def _record_with_exception(message: str) -> logging.LogRecord:
    try:
        raise RuntimeError(message)
    except RuntimeError:
        return logging.LogRecord(
            name="privaite.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="safe message",
            args=(),
            exc_info=sys.exc_info(),
        )


def test_log_formatters_never_serialize_exception_text():
    # Dependencies can put the text they were processing into an exception. Both
    # configured log formats must omit its traceback entirely.
    secret = "alice.sensitive@example.test"
    record = _record_with_exception(secret)

    json_line = _JsonFormatter().format(record)
    text_line = _PrivacySafeTextFormatter("%(message)s").format(record)

    assert secret not in json_line
    assert secret not in text_line
    assert "exc_info" not in json.loads(json_line)


@pytest.mark.asyncio
async def test_detector_failure_is_safe_and_never_logs_input(caplog):
    class LeakyDetector:
        async def detect(self, text: str, language: str):
            raise RuntimeError(f"detector received: {text}")

    secret = "alice.sensitive@example.test"
    config = PIIConfig(
        preset=None,
        detectors=DetectorsConfig(presidio=PresidioDetectorConfig(enabled=False)),
    )
    engine = PIIEngine(config)
    engine.detectors = [LeakyDetector()]

    with caplog.at_level(logging.ERROR, logger="privaite"):
        with pytest.raises(PIIProcessingError) as caught:
            await engine.process_request([{"role": "user", "content": secret}])

    assert str(caught.value) == "PII processing failed"
    assert caught.value.__suppress_context__
    assert secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_response_restore_failure_is_safe_and_never_logs_original(caplog, monkeypatch):
    secret = "alice.sensitive@example.test"
    engine = PIIEngine(PIIConfig(preset=None))

    def explode(_content, _mapping):
        raise RuntimeError(f"restore failed for: {secret}")

    monkeypatch.setattr(engine.deanonymizer, "deanonymize", explode)

    with caplog.at_level(logging.ERROR, logger="privaite"):
        with pytest.raises(PIIProcessingError) as caught:
            await engine.process_response("<EMAIL_ADDRESS_1>", PIIMapping())

    assert str(caught.value) == "PII processing failed"
    assert caught.value.__suppress_context__
    assert secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_request_pipeline_never_logs_anonymization_exception_text(caplog):
    from privaite.api.pipeline import anonymize_or_error

    secret = "alice.sensitive@example.test"

    async def explode():
        raise RuntimeError(f"anonymization failed for: {secret}")

    with caplog.at_level(logging.ERROR, logger="privaite"):
        result, error = await anonymize_or_error(
            explode, PrivAiTeConfig(), logging.getLogger("privaite.api.test")
        )

    assert result is None
    assert error is not None
    assert error.status_code == 500
    assert secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stream_method",
    [StreamingHandler.stream_response, StreamingHandler.stream_text_response],
)
async def test_stream_failures_are_safe_and_never_log_their_exception(caplog, stream_method):
    secret = "alice.sensitive@example.test"

    class BrokenStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError(f"stream failed for: {secret}")

    with caplog.at_level(logging.ERROR, logger="privaite"):
        with pytest.raises(PIIProcessingError) as caught:
            _ = [event async for event in stream_method(BrokenStream(), None, None)]

    assert str(caught.value) == "PII processing failed"
    assert caught.value.__suppress_context__
    assert secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
