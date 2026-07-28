import os
import tempfile
from pathlib import Path

import pytest
import yaml

from privaite.config.loader import load_config
from privaite.config.schema import PrivAiTeConfig

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _write_yaml(data: dict) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        return f.name


def test_explicitly_requested_missing_file_raises():
    # A --config typo or bad mount must not silently start an empty proxy with
    # 0 providers; only the DEFAULT path may fall back to built-in defaults.
    with pytest.raises(FileNotFoundError, match="nonexistent"):
        load_config("/nonexistent/path.yaml")


def test_load_defaults_when_default_path_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no config/privaite.yaml here
    monkeypatch.delenv("PRIVAITE_CONFIG_PATH", raising=False)
    config = load_config()
    assert isinstance(config, PrivAiTeConfig)
    assert config.server.port == 8400
    assert config.pii.enabled is True


def test_validation_error_never_echoes_interpolated_secrets(monkeypatch):
    # Interpolation runs before validation; a schema error must report field
    # locations only, never the interpolated api_key value.
    monkeypatch.setenv("TEST_PRIVAITE_SECRET", "sk-REAL-SECRET-VALUE-123")
    data = {
        "providers": [
            {  # model_name missing -> ValidationError
                "litellm_params": {
                    "model": "openai/gpt-4o",
                    "api_key": "${TEST_PRIVAITE_SECRET}",
                },
            }
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        path = f.name

    try:
        with pytest.raises(ValueError) as ei:
            load_config(path)
        message = str(ei.value)
        assert "model_name" in message  # the location is reported
        assert "sk-REAL-SECRET-VALUE-123" not in message  # the value never is
    finally:
        os.unlink(path)


def test_load_from_yaml():
    data = {
        "server": {"port": 9000},
        "pii": {"enabled": False},
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        path = f.name

    try:
        config = load_config(path)
        assert config.server.port == 9000
        assert config.pii.enabled is False
    finally:
        os.unlink(path)


def test_env_var_interpolation():
    os.environ["TEST_PRIVAITE_KEY"] = "sk-test-123"

    data = {
        "providers": [
            {
                "model_name": "test",
                "litellm_params": {
                    "model": "openai/gpt-4o",
                    "api_key": "${TEST_PRIVAITE_KEY}",
                },
            }
        ]
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        path = f.name

    try:
        config = load_config(path)
        assert config.providers[0].litellm_params.api_key == "sk-test-123"
    finally:
        os.unlink(path)
        del os.environ["TEST_PRIVAITE_KEY"]


def test_misspelled_block_entities_key_fails_at_boot():
    # The typo used to be ignored: the proxy started with an EMPTY policy gate
    # while the operator believed requests carrying those types were rejected.
    path = _write_yaml({"pii": {"enabled": True, "block_entites": ["US_SSN"]}})
    try:
        with pytest.raises(ValueError) as ei:
            load_config(path)
        message = str(ei.value)
        assert "pii.block_entites" in message
        assert "unknown config key" in message
    finally:
        os.unlink(path)


def test_misspelled_gateway_key_fails_at_boot():
    # `enable` instead of `enabled` used to yield a silently disabled gateway.
    path = _write_yaml({"gateway": {"enable": True}})
    try:
        with pytest.raises(ValueError) as ei:
            load_config(path)
        assert "gateway.enable" in str(ei.value)
    finally:
        os.unlink(path)


def test_unknown_top_level_key_fails_at_boot():
    path = _write_yaml({"pii_settings": {"enabled": True}})
    try:
        with pytest.raises(ValueError, match="unknown config key"):
            load_config(path)
    finally:
        os.unlink(path)


def test_provider_litellm_params_stay_permissive():
    # litellm's per-provider parameters change with every release, so this one
    # section must keep accepting keys the schema does not know about.
    path = _write_yaml(
        {
            "providers": [
                {
                    "model_name": "azure-model",
                    "litellm_params": {
                        "model": "azure/gpt-4o",
                        "api_version": "2024-02-01",
                        "aws_region_name": "eu-west-3",
                    },
                }
            ]
        }
    )
    try:
        config = load_config(path)
        assert config.providers[0].litellm_params.model == "azure/gpt-4o"
    finally:
        os.unlink(path)


@pytest.mark.parametrize(
    "name",
    # The tracked ones only: config/privaite.yaml is a local operator file, so
    # asserting on it passes on a developer machine and fails in CI.
    ["privaite.openai.yaml", "privaite.example.yaml"],
)
def test_shipped_configs_still_load(name, monkeypatch):
    # The Docker image boots on one of these three; strict key checking must not
    # reject anything we ship.
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder-not-a-key")
    config = load_config(_CONFIG_DIR / name)
    assert config.providers
    assert config.pii.enabled is True


def test_missing_env_var_raises():
    data = {
        "providers": [
            {
                "model_name": "test",
                "litellm_params": {
                    "model": "openai/gpt-4o",
                    "api_key": "${NONEXISTENT_VAR_XYZ}",
                },
            }
        ]
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        path = f.name

    try:
        with pytest.raises(ValueError, match="NONEXISTENT_VAR_XYZ"):
            load_config(path)
    finally:
        os.unlink(path)
