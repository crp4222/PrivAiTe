"""Startup warnings that quote benchmark figures.

The light preset warns the operator at boot with its own recall and points to
the onnx preset with its recall. Those figures are read by people choosing a
preset, so they follow the ones the README publishes instead of drifting behind
them (it said ~84% for a release after the published number had moved to 85.2%).
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


def _published_recall(row: str) -> int:
    match = re.search(rf"\| {re.escape(row)} \| (?:\*\*)?(\d+(?:\.\d+)?)%", README.read_text())
    assert match, f"README benchmark table no longer lists the recall of {row}"
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
        (
            None,
            "Presidio-only path (~{light}% recall on the benchmark). "
            "Use preset 'onnx' for ~{onnx}% if recall matters.",
        ),
        (
            ["PERSON"],
            "light recall (~{light}% on the benchmark), or use preset 'onnx' for ~{onnx}%.",
        ),
    ],
)
async def test_light_preset_warning_quotes_the_published_recalls(
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

    expected = sentence.format(
        light=_published_recall("`light`"), onnx=_published_recall("`onnx` (default)")
    )
    assert any(expected in message for message in records), records
