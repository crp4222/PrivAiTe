from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from privaite.config.schema import PrivAiTeConfig

logger = logging.getLogger("privaite.config.loader")

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

    explicit = path is not None or "PRIVAITE_CONFIG_PATH" in os.environ
    if path is None:
        path = os.environ.get("PRIVAITE_CONFIG_PATH", "config/privaite.yaml")

    path = Path(path)

    if not path.exists():
        if explicit:
            # A path the operator asked for (a --config typo, a bad mount) must
            # not silently degrade to an empty default config with 0 providers.
            raise FileNotFoundError(f"Config file not found: {path}")
        logger.warning(
            "No config file at default path %s; starting with built-in defaults (0 providers)",
            path,
        )
        return PrivAiTeConfig()

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    interpolated = _interpolate_env_vars(raw)
    try:
        return PrivAiTeConfig.model_validate(interpolated)
    except ValidationError as exc:
        # Never echo field VALUES: interpolation already ran, so pydantic's
        # default repr would print interpolated API keys into the startup log.
        locations = "; ".join(
            ".".join(str(part) for part in err["loc"]) + f" ({err['msg']})"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise ValueError(f"Invalid config {path}: {locations}") from None
