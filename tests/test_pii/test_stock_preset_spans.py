"""The stock preset really does detect values that break naive JSON splicing.

The restore paths JSON-escape a restored original before splicing it into
streamed tool-call arguments. That is only worth doing if the shipped
configuration can actually produce originals carrying JSON metacharacters, so
this pins the measured fact instead of a hypothesis: with the default preset,
a multi-line address yields a span that spans the line break, and a UNC path or
a domain-qualified login yields a span carrying backslashes.

The claim is an existence proof over a small corpus of realistic inputs, not a
per-input contract: which of them the model spans across a newline is a model
detail that may shift, that ANY of them does is the point.

Skipped unless the stock ONNX model is already in the local Hugging Face cache:
CI does not download it (~800MB), and a test must never pull it in.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from privaite.config.schema import (
    DetectorsConfig,
    OnnxDetectorConfig,
    PIIConfig,
    PresidioDetectorConfig,
)
from privaite.pii.engine import PIIEngine

os.environ["TOKENIZERS_PARALLELISM"] = "false"

# Synthetic, invented values: multi-line shipping addresses, a UNC share and a
# domain-qualified login.
_MULTILINE_ADDRESSES = (
    "Send the parcel to:\nMarie Dupont\n12 Rue de la Paix\n75002 Paris\nFrance\nThanks.",
    "Ship to:\nJohn Carter\n1600 Amphitheatre Parkway\nMountain View, CA 94043\nUSA",
    "Return to sender:\nMarie Dupont\n12 Rue de la Paix\n75002 Paris\nFrance\nRegards, Marie",
)
_BACKSLASH_PATHS = (
    r"net use Z: \\corp.acme.local\users\marie.dupont /persistent:yes",
    r"The account is CORP\marie.dupont and the mailbox is marie.dupont@acme.com.",
    r"Server=SQLSRV01\SQLEXPRESS;User Id=CORP\svc_payroll;Password=Hunter2;",
)


def _stock_model_cached() -> bool:
    """True when the default ONNX weights are already in the local HF cache."""
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:  # pragma: no cover - huggingface_hub is a hard dependency
        return False
    config = OnnxDetectorConfig()
    hit = try_to_load_from_cache(
        config.model_name,
        f"onnx/model_{config.onnx_variant}.onnx",
        revision=config.revision,
    )
    return isinstance(hit, str)


requires_stock_model = pytest.mark.skipif(
    not _stock_model_cached(),
    reason="stock ONNX model not in the local Hugging Face cache",
)


@pytest.fixture(scope="module")
def stock_engine():
    # Stock posture: PIIConfig defaults (preset "onnx"), with one language pinned
    # so the fixture does not depend on which spaCy models are installed.
    config = PIIConfig(detectors=DetectorsConfig(presidio=PresidioDetectorConfig(languages=["en"])))
    assert config.preset == "onnx"
    engine = PIIEngine(config)
    loop = asyncio.new_event_loop()
    loop.run_until_complete(engine.initialize())
    yield engine
    loop.run_until_complete(engine.shutdown())
    loop.close()


async def _span_texts(engine, texts: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for text in texts:
        found.extend(entity.text for entity in await engine.inspect_text(text))
    return found


@requires_stock_model
@pytest.mark.asyncio
async def test_stock_preset_emits_a_span_containing_a_newline(stock_engine):
    spans = await _span_texts(stock_engine, _MULTILINE_ADDRESSES)

    assert any("\n" in span for span in spans), "no multi-line span in the stock detections"


@requires_stock_model
@pytest.mark.asyncio
async def test_stock_preset_emits_a_span_containing_a_backslash(stock_engine):
    spans = await _span_texts(stock_engine, _BACKSLASH_PATHS)

    assert any("\\" in span for span in spans), "no backslash-carrying span in the stock detections"
