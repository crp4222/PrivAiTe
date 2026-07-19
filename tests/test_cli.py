"""Pin the CLI surface: options, echo strings, config plumbing and exit codes.

The CLI is what every quickstart runs first; a changed flag or startup line is
a behavior change and must show up here.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import privaite.cli as cli_module
from privaite.cli import main
from privaite.config.schema import (
    LiteLLMParams,
    PIIConfig,
    PrivAiTeConfig,
    ProviderConfig,
    ServerConfig,
)


@pytest.fixture
def fake_boot(monkeypatch):
    """Stub load_config and uvicorn.run, capturing what the CLI does with them."""
    calls: dict = {}

    config = PrivAiTeConfig(
        server=ServerConfig(host="0.0.0.0", port=8400, workers=1, log_level="info"),
        providers=[
            ProviderConfig(
                model_name="test-model",
                litellm_params=LiteLLMParams(model="openai/gpt-4o-mini"),
            )
        ],
        pii=PIIConfig(enabled=True),
    )

    def fake_load_config(path):
        calls["config_path"] = path
        return config

    def fake_run(app, **kwargs):
        calls["app"] = app
        calls["run_kwargs"] = kwargs

    monkeypatch.setattr(cli_module, "load_config", fake_load_config)
    monkeypatch.setattr(cli_module.uvicorn, "run", fake_run)
    monkeypatch.delenv("PRIVAITE_CONFIG_PATH", raising=False)
    calls["config"] = config
    return calls


def test_help_lists_every_option():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "--config" in result.output
    assert "Path to config YAML file" in result.output
    assert "--host" in result.output
    assert "Override server host" in result.output
    assert "--port" in result.output
    assert "Override server port" in result.output
    assert "--reload" in result.output
    assert "Auto-reload on file changes (dev mode)" in result.output


def test_startup_echo_and_uvicorn_wiring(fake_boot):
    result = CliRunner().invoke(main, [])

    assert result.exit_code == 0
    assert "Starting PrivAiTe on 0.0.0.0:8400" in result.output
    assert "PII processing: enabled" in result.output
    assert "Providers: 1 configured" in result.output
    assert "Auto-reload enabled" not in result.output

    assert fake_boot["app"] == "privaite.app:create_app"
    kwargs = fake_boot["run_kwargs"]
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 8400
    assert kwargs["workers"] == 1
    assert kwargs["log_level"] == "info"
    assert kwargs["factory"] is True
    assert kwargs["reload"] is False
    assert kwargs["reload_dirs"] is None


def test_pii_disabled_is_echoed(fake_boot):
    fake_boot["config"].pii.enabled = False
    result = CliRunner().invoke(main, [])
    assert result.exit_code == 0
    assert "PII processing: disabled" in result.output


def test_host_and_port_options_override_config(fake_boot):
    result = CliRunner().invoke(main, ["--host", "127.0.0.1", "--port", "8452"])

    assert result.exit_code == 0
    assert "Starting PrivAiTe on 127.0.0.1:8452" in result.output
    assert fake_boot["run_kwargs"]["host"] == "127.0.0.1"
    assert fake_boot["run_kwargs"]["port"] == 8452


def test_config_path_reaches_loader_and_worker_env(fake_boot, monkeypatch):
    # uvicorn re-imports create_app() in the worker process, so the path must
    # travel through PRIVAITE_CONFIG_PATH, not just this process's variable.
    result = CliRunner().invoke(main, ["--config", "some.yaml"])

    assert result.exit_code == 0
    assert fake_boot["config_path"] == "some.yaml"
    import os

    assert os.environ.get("PRIVAITE_CONFIG_PATH") == "some.yaml"
    monkeypatch.delenv("PRIVAITE_CONFIG_PATH", raising=False)


def test_no_config_flag_leaves_env_untouched(fake_boot):
    import os

    result = CliRunner().invoke(main, [])
    assert result.exit_code == 0
    assert fake_boot["config_path"] is None
    assert "PRIVAITE_CONFIG_PATH" not in os.environ


def test_reload_flag_echoes_and_wires_reload_dirs(fake_boot):
    result = CliRunner().invoke(main, ["--reload"])

    assert result.exit_code == 0
    assert "Auto-reload enabled (dev mode)" in result.output
    assert fake_boot["run_kwargs"]["reload"] is True
    assert fake_boot["run_kwargs"]["reload_dirs"] == ["privaite", "config"]


def test_non_integer_port_is_a_usage_error(fake_boot):
    result = CliRunner().invoke(main, ["--port", "not-a-port"])
    assert result.exit_code == 2
    assert "run_kwargs" not in fake_boot  # the server never started


def test_unknown_option_is_a_usage_error(fake_boot):
    result = CliRunner().invoke(main, ["--bogus"])
    assert result.exit_code == 2
    assert "run_kwargs" not in fake_boot
