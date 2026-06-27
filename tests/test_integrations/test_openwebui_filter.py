from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

FILTER_PATH = (
    Path(__file__).resolve().parents[2]
    / "integrations" / "openwebui" / "privaite_filter.py"
)


def _load_filter():
    spec = importlib.util.spec_from_file_location("privaite_filter", FILTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_filter_uses_engine_api_that_exists():
    """The filter calls these engine and mapping methods. Lock the contract so a
    refactor of the engine cannot silently break the integration."""
    from privaite.pii.engine import PIIEngine
    from privaite.pii.mapping import PIIMapping

    for name in (
        "process_request",
        "process_response",
        "process_response_tool_calls",
        "process_response_function_call",
    ):
        assert hasattr(PIIEngine, name), f"PIIEngine.{name} missing"
    assert hasattr(PIIMapping, "get_all_fakes")
    assert hasattr(PIIMapping, "is_empty")


@pytest.mark.asyncio
async def test_inlet_outlet_roundtrip():
    """inlet anonymizes the request and stashes the mapping; outlet restores the
    real values in the assistant reply, exactly as Open WebUI drives it."""
    module = _load_filter()
    flt = module.Filter()
    flt.valves.preset = "light"
    flt.valves.languages = "en,fr"

    meta: dict = {}
    body = {"messages": [
        {"role": "user", "content": "I am Marie Dupont, email marie.dupont@acme.com"}
    ]}
    body = await flt.inlet(body, meta)
    anonymized = body["messages"][0]["content"]

    assert "Marie Dupont" not in anonymized
    assert "marie.dupont@acme.com" not in anonymized
    assert meta.get("privaite_map")

    reply = {"messages": [{"role": "assistant", "content": f"Hello {anonymized}"}]}
    reply = await flt.outlet(reply, meta)
    restored = reply["messages"][0]["content"]

    assert "Marie Dupont" in restored
    assert "marie.dupont@acme.com" in restored
