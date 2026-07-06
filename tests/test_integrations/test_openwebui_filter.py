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


@pytest.mark.asyncio
async def test_outlet_pops_map_so_originals_cannot_persist():
    # Open WebUI may persist message metadata; the fake->original map must be
    # consumed by outlet, never left behind.
    module = _load_filter()
    flt = module.Filter()
    flt.valves.preset = "light"
    flt.valves.languages = "en"

    meta: dict = {}
    await flt.inlet(
        {"messages": [{"role": "user", "content": "I am Marie Dupont"}]}, meta
    )
    assert "privaite_map" in meta

    reply = {"messages": [{"role": "assistant", "content": "ok"}]}
    await flt.outlet(reply, meta)
    assert "privaite_map" not in meta


@pytest.mark.asyncio
async def test_inlet_never_stashes_when_restore_disabled():
    # with deanonymize off, outlet never consumes the map: stashing it would
    # park the ORIGINAL values in metadata for nothing.
    module = _load_filter()
    flt = module.Filter()
    flt.valves.preset = "light"
    flt.valves.languages = "en"
    flt.valves.deanonymize = False

    meta: dict = {}
    await flt.inlet(
        {"messages": [{"role": "user", "content": "I am Marie Dupont"}]}, meta
    )
    assert "privaite_map" not in meta


@pytest.mark.asyncio
async def test_inlet_clears_attacker_supplied_map():
    # a client-injected map must not drive outlet restoration to attacker text.
    module = _load_filter()
    flt = module.Filter()
    flt.valves.preset = "light"
    flt.valves.languages = "en"

    meta: dict = {"privaite_map": {"the report": "ATTACKER CONTROLLED"}}
    await flt.inlet({"messages": [{"role": "user", "content": "summarize"}]}, meta)

    reply = {"messages": [{"role": "assistant", "content": "here is the report"}]}
    out = await flt.outlet(reply, meta)
    assert "ATTACKER CONTROLLED" not in out["messages"][0]["content"]


@pytest.mark.asyncio
async def test_outlet_restores_multimodal_text_parts():
    module = _load_filter()
    flt = module.Filter()
    flt.valves.preset = "light"
    flt.valves.languages = "en"

    meta: dict = {}
    body = await flt.inlet(
        {"messages": [{"role": "user", "content": "I am Marie Dupont"}]}, meta
    )
    placeholder = body["messages"][0]["content"].replace("I am ", "")

    reply = {"messages": [{
        "role": "assistant",
        "content": [{"type": "text", "text": f"Hello {placeholder}"}],
    }]}
    out = await flt.outlet(reply, meta)
    assert out["messages"][0]["content"][0]["text"] == "Hello Marie Dupont"


@pytest.mark.asyncio
async def test_outlet_restores_structured_output_items():
    # Open WebUI >= 0.10 stores the reply as structured `output` items and leaves
    # message["content"] empty; restoring only "content" would be a no-op and the
    # user would see placeholders. The message AND reasoning output_text parts
    # must be restored, and a NEW output object must be returned (Open WebUI only
    # persists the restored reply if the output object changed).
    module = _load_filter()
    flt = module.Filter()
    flt.valves.preset = "light"
    flt.valves.languages = "en"

    meta: dict = {}
    body = await flt.inlet(
        {"messages": [{"role": "user", "content": "I am Marie Dupont"}]}, meta
    )
    placeholder = body["messages"][0]["content"].replace("I am ", "")

    original_output = [
        {
            "type": "reasoning",
            "content": [{"type": "output_text", "text": f"Thinking about {placeholder}"}],
        },
        {
            "type": "message",
            "content": [{"type": "output_text", "text": f"Hello {placeholder}"}],
        },
    ]
    reply = {"messages": [{"role": "assistant", "content": "", "output": original_output}]}
    out = await flt.outlet(reply, meta)

    restored = out["messages"][0]["output"]
    assert restored[0]["content"][0]["text"] == "Thinking about Marie Dupont"
    assert restored[1]["content"][0]["text"] == "Hello Marie Dupont"
    # A new object is returned so Open WebUI detects the change and persists it.
    assert restored is not original_output
    assert original_output[1]["content"][0]["text"] == f"Hello {placeholder}"


@pytest.mark.asyncio
async def test_outlet_leaves_output_untouched_when_no_pii():
    # A clean reply (nothing to restore) must not be rewritten: outlet returns the
    # SAME output object so Open WebUI treats it as unchanged and skips persisting.
    module = _load_filter()
    flt = module.Filter()
    flt.valves.preset = "light"
    flt.valves.languages = "en"

    meta: dict = {}
    await flt.inlet(
        {"messages": [{"role": "user", "content": "I am Marie Dupont"}]}, meta
    )

    original_output = [
        {"type": "message", "content": [{"type": "output_text", "text": "no pii here"}]}
    ]
    reply = {"messages": [{"role": "assistant", "content": "", "output": original_output}]}
    out = await flt.outlet(reply, meta)
    assert out["messages"][0]["output"] is original_output


@pytest.mark.asyncio
async def test_inlet_blocks_configured_type():
    module = _load_filter()
    flt = module.Filter()
    flt.valves.preset = "light"
    flt.valves.languages = "en"
    flt.valves.block_entities = "EMAIL_ADDRESS"

    meta: dict = {}
    body = {"messages": [{"role": "user", "content": "email me at bob@example.com"}]}
    with pytest.raises(Exception) as ei:
        await flt.inlet(body, meta)

    assert "EMAIL_ADDRESS" in str(ei.value)
    assert "bob@example.com" not in str(ei.value)  # value never leaks
    assert "privaite_map" not in meta  # nothing stashed, nothing forwarded


@pytest.mark.asyncio
async def test_inlet_ignores_types_not_present():
    # a blocked type that is absent must not disturb a normal request.
    module = _load_filter()
    flt = module.Filter()
    flt.valves.preset = "light"
    flt.valves.languages = "en"
    flt.valves.block_entities = "US_SSN"

    meta: dict = {}
    body = {"messages": [
        {"role": "user", "content": "I am Marie Dupont, email marie.dupont@acme.com"}
    ]}
    body = await flt.inlet(body, meta)
    anonymized = body["messages"][0]["content"]

    assert "Marie Dupont" not in anonymized
    assert "marie.dupont@acme.com" not in anonymized
