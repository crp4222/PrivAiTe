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


def test_default_preset_is_onnx():
    config = PIIConfig()
    assert config.preset == "onnx"
    assert config.detectors.onnx.enabled is True
    assert config.detectors.presidio.enabled is True


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


def test_on_error_defaults_to_block_and_accepts_allow():
    assert PIIConfig().on_error == "block"
    assert PIIConfig(on_error="allow").on_error == "allow"


def test_on_error_rejects_typos():
    # a free string let a typo silently fail open; the Literal rejects it now.
    with pytest.raises(ValueError):
        PIIConfig(on_error="blcok")
