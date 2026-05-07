import os
import tempfile

import pytest
import yaml

from privaite.config.loader import load_config
from privaite.config.schema import PrivAiTeConfig


def test_load_defaults_when_no_file():
    config = load_config("/nonexistent/path.yaml")
    assert isinstance(config, PrivAiTeConfig)
    assert config.server.port == 8400
    assert config.pii.enabled is True


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
