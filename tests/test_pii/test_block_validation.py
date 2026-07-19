"""Startup validation of pii.block_entities.

A blocked type that no enabled detector can emit would make the block gate
silently unenforceable (fail-open by configuration), so initialize() must
refuse it. The validator is deliberately conservative: whenever a detector's
producible set is unknown, validation is skipped rather than risk rejecting a
working config.
"""

from __future__ import annotations

import pytest

from privaite.config.schema import (
    CustomPatternConfig,
    DetectorsConfig,
    GlinerDetectorConfig,
    OnnxDetectorConfig,
    PIIConfig,
    PresidioDetectorConfig,
)
from privaite.pii.engine import PIIEngine


def _presidio_config(
    entities: list[str] | None,
    block_entities: list[str],
    custom_patterns: list[CustomPatternConfig] | None = None,
) -> PIIConfig:
    return PIIConfig(
        enabled=True,
        preset=None,
        detectors=DetectorsConfig(
            presidio=PresidioDetectorConfig(enabled=True, languages=["en"], entities=entities)
        ),
        block_entities=block_entities,
        custom_patterns=custom_patterns or [],
    )


def _onnx_config(
    block_entities: list[str], label_mapping: dict[str, str] | None = None
) -> PIIConfig:
    kwargs = {} if label_mapping is None else {"label_mapping": label_mapping}
    return PIIConfig(
        enabled=True,
        preset=None,
        detectors=DetectorsConfig(
            presidio=PresidioDetectorConfig(enabled=False),
            onnx=OnnxDetectorConfig(enabled=True, **kwargs),
        ),
        block_entities=block_entities,
    )


@pytest.mark.asyncio
async def test_unproducible_blocked_type_fails_at_init():
    # Presidio restricted to PERSON can never emit US_PASSPORT: blocking it
    # would silently never fire. initialize() must raise before loading models.
    engine = PIIEngine(_presidio_config(entities=["PERSON"], block_entities=["US_PASSPORT"]))
    with pytest.raises(ValueError, match="US_PASSPORT"):
        await engine.initialize()


@pytest.mark.asyncio
async def test_unproducible_blocked_type_fails_for_ml_label_mapping():
    # The default onnx label_mapping cannot emit US_PASSPORT either. The
    # validator runs before the model is downloaded/loaded, so this is cheap.
    engine = PIIEngine(_onnx_config(block_entities=["US_PASSPORT"]))
    with pytest.raises(ValueError, match="US_PASSPORT"):
        await engine.initialize()


def test_producible_blocked_type_passes_validation():
    engine = PIIEngine(_presidio_config(entities=["PERSON"], block_entities=["PERSON"]))
    engine._validate_block_entities()  # must not raise


def test_ml_label_mapping_values_are_producible():
    # SECRET is a value of the default privacy-filter label_mapping.
    engine = PIIEngine(_onnx_config(block_entities=["SECRET"]))
    engine._validate_block_entities()  # must not raise


def test_custom_pattern_type_counts_as_producible():
    engine = PIIEngine(
        _presidio_config(
            entities=["PERSON"],
            block_entities=["CUSTOMER_ID"],
            custom_patterns=[CustomPatternConfig(pattern=r"KD-\d{6}", entity_type="CUSTOMER_ID")],
        )
    )
    engine._validate_block_entities()  # must not raise


def test_union_across_detectors_is_producible():
    # A type only one of several enabled detectors can emit is still valid.
    config = PIIConfig(
        enabled=True,
        preset=None,
        detectors=DetectorsConfig(
            presidio=PresidioDetectorConfig(enabled=True, languages=["en"], entities=["PERSON"]),
            onnx=OnnxDetectorConfig(enabled=True),
        ),
        block_entities=["SECRET"],
    )
    PIIEngine(config)._validate_block_entities()  # must not raise


def test_presidio_without_allowlist_skips_validation():
    # No entity allowlist means the full Presidio registry: producible set is
    # unknown, so even an odd type must not be rejected (under-warn, never
    # break a legitimate startup).
    engine = PIIEngine(_presidio_config(entities=None, block_entities=["ANY_ODD_TYPE"]))
    engine._validate_block_entities()  # must not raise


def test_empty_label_mapping_skips_validation():
    engine = PIIEngine(_onnx_config(block_entities=["ANY_ODD_TYPE"], label_mapping={}))
    engine._validate_block_entities()  # must not raise


def test_gliner_label_mapping_counts():
    config = PIIConfig(
        enabled=True,
        preset=None,
        detectors=DetectorsConfig(
            presidio=PresidioDetectorConfig(enabled=False),
            gliner=GlinerDetectorConfig(enabled=True),
        ),
        block_entities=["US_PASSPORT"],
    )
    # US_PASSPORT is producible via gliner's default label_mapping.
    PIIEngine(config)._validate_block_entities()  # must not raise


def test_default_empty_block_entities_is_unaffected():
    engine = PIIEngine(_presidio_config(entities=["PERSON"], block_entities=[]))
    engine._validate_block_entities()  # no-op


def test_zero_detectors_left_to_dedicated_check():
    # With no detector enabled at all, the zero-detector startup error must
    # keep reporting the real problem; block validation stays silent.
    config = PIIConfig(
        enabled=True,
        preset=None,
        detectors=DetectorsConfig(presidio=PresidioDetectorConfig(enabled=False)),
        block_entities=["US_PASSPORT"],
    )
    engine = PIIEngine(config)
    engine._validate_block_entities()  # must not raise here


@pytest.mark.asyncio
async def test_zero_detectors_still_raises_the_zero_detector_error():
    config = PIIConfig(
        enabled=True,
        preset=None,
        detectors=DetectorsConfig(presidio=PresidioDetectorConfig(enabled=False)),
        block_entities=["US_PASSPORT"],
    )
    with pytest.raises(ValueError, match="no detector"):
        await PIIEngine(config).initialize()
