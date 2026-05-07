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


def test_preset_none_uses_defaults():
    config = PIIConfig(preset=None)
    assert config.detectors.presidio.enabled is True
    assert config.detectors.mlmodel.enabled is False


def test_preset_invalid_raises():
    with pytest.raises(ValueError, match="Unknown PII preset"):
        PIIConfig(preset="turbo")
