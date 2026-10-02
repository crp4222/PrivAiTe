"""Startup warnings that quote benchmark figures.

The light preset warns the operator at boot and points to the onnx preset with
its recall. That figure is read by people choosing a preset, so it follows the
one the README publishes instead of drifting behind it (it said ~84% for a
release after the published number had moved to 85.2%).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from privaite.app import create_app, lifespan
from privaite.config.schema import (
    DetectorsConfig,
    PIIConfig,
    PresidioDetectorConfig,
    PrivAiTeConfig,
)

README = Path(__file__).resolve().parents[1] / "README.md"


def _published_onnx_recall() -> int:
    match = re.search(r"\| `onnx` \(default\) \| \*\*(\d+(?:\.\d+)?)%\*\*", README.read_text())
    assert match, "README benchmark table no longer lists the onnx recall"
    return round(float(match.group(1)))


class _FakeEngine:
    """Startup path only: no model is loaded."""

    def __init__(self, config) -> None:
        pass

    async def initialize(self) -> None:
        pass

    async def warmup(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


@pytest.mark.parametrize(
    "entities, sentence",
    [
        (None, "Use preset 'onnx' for ~{recall}% if recall matters."),
        (["PERSON"], "or use preset 'onnx' for ~{recall}%."),
    ],
)
async def test_light_preset_warning_quotes_the_published_onnx_recall(
    monkeypatch, entities, sentence
) -> None:
    from privaite.pii import engine as engine_module

    monkeypatch.setattr(engine_module, "PIIEngine", _FakeEngine)
    config = PrivAiTeConfig(
        pii=PIIConfig(
            preset="light",
            detectors=DetectorsConfig(presidio=PresidioDetectorConfig(entities=entities)),
        )
    )
    app = create_app(config)

    # setup_logging stops propagation at the "privaite" logger, so listen there.
    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = _Collect(level=logging.WARNING)
    logging.getLogger("privaite").addHandler(handler)
    try:
        async with lifespan(app):
            pass
    finally:
        logging.getLogger("privaite").removeHandler(handler)

    expected = sentence.format(recall=_published_onnx_recall())
    assert any(expected in message for message in records), records
