import os
import tempfile

import pytest
import yaml

from privaite.config.loader import load_config
from privaite.config.schema import PrivAiTeConfig


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
            {   # model_name missing -> ValidationError
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
