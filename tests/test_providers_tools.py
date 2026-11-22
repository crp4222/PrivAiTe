"""A chat request carrying `tools` reaches the provider.

litellm 1.106.0 to 1.106.12 and 1.108.0 to 1.108.3 import their MCP handler,
and through it the `mcp` package of their proxy extra, on every chat call with
`tools`. PrivAiTe did not depend on it, so in the 0.7.3 to 0.8.2 images every
tool request came back 502 and never left (#45). The endpoint tests use a fake
router; this one goes through the real router and litellm to a local server.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from privaite.config.schema import LiteLLMParams, ProviderConfig
from privaite.providers.router import ProviderRouter
from privaite.verify import Capture

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Weather of a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]


@pytest.fixture
def capture():
    server = Capture().start()
    yield server
    server.stop()


def _dependency(name: str) -> Requirement:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return next(
        Requirement(dep)
        for dep in pyproject["project"]["dependencies"]
        if Requirement(dep).name == name
    )


@pytest.mark.asyncio
async def test_a_tools_request_reaches_the_provider(capture) -> None:
    router = ProviderRouter(
        [
            ProviderConfig(
                model_name="m",
                litellm_params=LiteLLMParams(
                    model="openai/m", api_key="sk-test-not-real", api_base=capture.base_url
                ),
            )
        ]
    )

    await router.completion("m", [{"role": "user", "content": "hi"}], tools=TOOLS)

    assert [json.loads(body)["tools"] for body in capture.bodies] == [TOOLS]


def test_mcp_is_required_for_the_litellm_releases_that_import_it() -> None:
    assert _dependency("mcp").specifier.contains("2.3.0")
    assert not _dependency("mcp").specifier.contains("1.28.1")


def test_the_maintained_litellm_lines_stay_installable() -> None:
    """A LiteLLM proxy running the guardrail may be pinned to them: excluding
    them would make installing privaite replace the proxy's litellm."""
    litellm = _dependency("litellm")
    for version in ("1.106.0", "1.106.12", "1.108.0", "1.108.3", "1.110.2"):
        assert litellm.specifier.contains(version)
