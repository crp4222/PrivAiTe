import pytest

from privaite.config.schema import PIIConfig


def test_preset_light():
    config = PIIConfig(preset="light")
    assert config.detectors.presidio.enabled is True
    assert config.detectors.bert_ner.enabled is False
    assert config.detectors.mlmodel.enabled is False


def test_preset_standard():
    config = PIIConfig(preset="standard")
    assert config.detectors.presidio.enabled is True
    assert config.detectors.bert_ner.enabled is True
    assert config.detectors.mlmodel.enabled is False


def test_preset_full():
    config = PIIConfig(preset="full")
    assert config.detectors.presidio.enabled is True
    assert config.detectors.bert_ner.enabled is True
    assert config.detectors.mlmodel.enabled is True


def test_preset_max_enables_gliner_on_top_of_onnx():
    config = PIIConfig(preset="max")
    assert config.detectors.presidio.enabled is True
    assert config.detectors.onnx.enabled is True
    assert config.detectors.gliner.enabled is True
    assert config.detectors.bert_ner.enabled is False
    assert config.detectors.mlmodel.enabled is False
    # mirrors the onnx preset's Presidio allowlist
    assert config.detectors.presidio.entities == [
        "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD",
        "IBAN_CODE", "IP_ADDRESS", "DATE_TIME", "US_SSN", "UK_NHS",
    ]


def test_default_preset_is_onnx():
    config = PIIConfig()
    assert config.preset == "onnx"
    assert config.detectors.onnx.enabled is True
    assert config.detectors.presidio.enabled is True
    # gliner stays off unless the max preset is chosen
    assert config.detectors.gliner.enabled is False


def test_preset_none_uses_defaults():
    config = PIIConfig(preset=None)
    assert config.detectors.presidio.enabled is True
    assert config.detectors.mlmodel.enabled is False
    assert config.detectors.onnx.enabled is False


def test_preset_invalid_raises():
    with pytest.raises(ValueError, match="Unknown PII preset"):
        PIIConfig(preset="turbo")


def test_preset_light_does_not_pin_entities():
    # the light path must run full Presidio (~62% recall), not a crippled
    # allowlist (~35%); a pinned entities list is the documented footgun.
    config = PIIConfig(preset="light")
    assert config.detectors.presidio.entities is None


def test_inspect_endpoint_disabled_by_default():
    # The dry-run inspect endpoint returns detections for caller text; exposing
    # it must be an explicit operator decision, never the default posture.
    assert PIIConfig().inspect.enabled is False


def test_on_error_defaults_to_block_and_accepts_allow():
    assert PIIConfig().on_error == "block"
    assert PIIConfig(on_error="allow").on_error == "allow"


def test_on_error_rejects_typos():
    # a free string let a typo silently fail open; the Literal rejects it now.
    with pytest.raises(ValueError):
        PIIConfig(on_error="blcok")


def test_block_entities_default_empty_and_accepts_list():
    # default is empty -> behavior unchanged (everything masked, nothing blocked).
    assert PIIConfig().block_entities == []
    assert PIIConfig(block_entities=["US_SSN", "CREDIT_CARD"]).block_entities == [
        "US_SSN",
        "CREDIT_CARD",
    ]


def test_model_loading_is_supply_chain_safe_by_default():
    # A PII proxy must not execute model-repo code, and should be pinnable to a
    # revision so a rewritten repo cannot silently swap the weights.
    from privaite.config.schema import (
        BertNERDetectorConfig,
        MLModelDetectorConfig,
        OnnxDetectorConfig,
    )

    assert MLModelDetectorConfig().trust_remote_code is False
    assert OnnxDetectorConfig().trust_remote_code is False
    assert MLModelDetectorConfig().revision is None
    assert OnnxDetectorConfig().revision is None
    assert BertNERDetectorConfig().revision is None
