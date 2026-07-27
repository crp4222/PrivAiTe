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
        "PERSON",
        "EMAIL_ADDRESS",
        "PHONE_NUMBER",
        "CREDIT_CARD",
        "IBAN_CODE",
        "IP_ADDRESS",
        "DATE_TIME",
        "US_SSN",
        "UK_NHS",
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


def test_unknown_key_is_rejected_by_every_config_section():
    from privaite.config.schema import (
        DeanonymizationConfig,
        GatewayConfig,
        PrivAiTeConfig,
        ServerConfig,
    )

    with pytest.raises(ValueError):
        PIIConfig(block_entites=["US_SSN"])
    with pytest.raises(ValueError):
        GatewayConfig(enable=True)
    with pytest.raises(ValueError):
        ServerConfig(prt=9000)
    with pytest.raises(ValueError):
        DeanonymizationConfig(fuzzy_treshold=0.9)
    with pytest.raises(ValueError):
        PrivAiTeConfig(piii={})


def test_litellm_params_keep_accepting_unknown_provider_keys():
    from privaite.config.schema import LiteLLMParams

    params = LiteLLMParams(model="azure/gpt-4o", api_version="2024-02-01")
    assert params.model_dump()["api_version"] == "2024-02-01"


def test_model_loading_is_supply_chain_safe_by_default():
    # A PII proxy must not execute model-repo code or follow mutable model refs
    # by default, so a rewritten repo cannot silently swap the weights.
    from privaite.config.schema import (
        BertNERDetectorConfig,
        GlinerDetectorConfig,
        MLModelDetectorConfig,
        OnnxDetectorConfig,
    )

    assert MLModelDetectorConfig().trust_remote_code is False
    assert OnnxDetectorConfig().trust_remote_code is False
    assert MLModelDetectorConfig().revision == "7ffa9a043d54d1be65afb281eddf0ffbe629385b"
    assert OnnxDetectorConfig().revision == "7ffa9a043d54d1be65afb281eddf0ffbe629385b"
    assert BertNERDetectorConfig().revision == "d1a3e8f13f8c3566299d95fcfc9a8d2382a9affc"
    assert GlinerDetectorConfig().revision == "1fcf13e85f4eef5394e1fcd406cf2ca9ea82351d"
