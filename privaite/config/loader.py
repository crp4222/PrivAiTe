from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

from privaite.config.schema import PrivAiTeConfig

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)}")


def _interpolate_env_vars(obj: object) -> object:
    if isinstance(obj, str):
        def _replace(match: re.Match) -> str:
            var_name = match.group(1)
            value = os.environ.get(var_name)
            if value is None:
                raise ValueError(f"Environment variable '{var_name}' is not set")
            return value

        return _ENV_VAR_PATTERN.sub(_replace, obj)

    if isinstance(obj, dict):
        return {k: _interpolate_env_vars(v) for k, v in obj.items()}

    if isinstance(obj, list):
        return [_interpolate_env_vars(item) for item in obj]

    return obj


def load_config(path: str | Path | None = None) -> PrivAiTeConfig:
    # override=False so the explicit environment (e.g. PRIVAITE_CONFIG_PATH set by
    # the CLI from --config) wins over .env; .env only fills in variables that are
    # not already set, such as OPENAI_API_KEY.
    load_dotenv(override=False)

    if path is None:
        path = os.environ.get("PRIVAITE_CONFIG_PATH", "config/privaite.yaml")

    path = Path(path)

    if not path.exists():
        return PrivAiTeConfig()

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    interpolated = _interpolate_env_vars(raw)
    return PrivAiTeConfig.model_validate(interpolated)
