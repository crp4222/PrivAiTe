"""Doc-to-code pins.

The gateway's scanned/not-scanned surface is a privacy claim, so it is published
in docs/gateway.md. These tests fail when the published lists and the frozensets
the scrubber actually uses drift apart, in EITHER direction: a type added to the
code without being documented, or documented without being in the code.

The shipped configs make the same kind of claim about reversibility, so they are
pinned to the documented behaviour too.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from privaite.gateway import scrub
from privaite.pii.anonymizer import Anonymizer

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_DOC = REPO_ROOT / "docs" / "gateway.md"
SHIPPED_CONFIGS = (
    REPO_ROOT / "config" / "privaite.example.yaml",
    REPO_ROOT / "config" / "privaite.openai.yaml",
)


def _documented_set(label: str) -> set[str]:
    """The backticked identifiers on the doc bullet introduced by `label`."""
    for line in GATEWAY_DOC.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(f"- {label}:"):
            return set(re.findall(r"`([^`]+)`", stripped))
    raise AssertionError(f"docs/gateway.md has no bullet labelled {label!r}")


def _typed_field_pairs() -> set[str]:
    return {
        f"{item_type}.{field}"
        for item_type, fields in scrub._RESPONSES_DATA_FIELDS.items()
        for field in fields
    }


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (
            "Anthropic blocks relayed byte-for-byte",
            set(scrub._THINKING_TYPES | scrub._ANTHROPIC_OPAQUE_TYPES),
        ),
        (
            "Anthropic tool blocks scanned",
            set(scrub._TOOL_USE_TYPES | scrub._TOOL_RESULT_TYPES),
        ),
        (
            "Unknown Anthropic block, plaintext fields scanned",
            set(scrub._GENERIC_TEXT_FIELDS),
        ),
        (
            "Unknown Anthropic block, JSON payload fields walked",
            set(scrub._GENERIC_DATA_FIELDS),
        ),
        (
            "Responses items relayed byte-for-byte",
            set(scrub._RESPONSES_OPAQUE_TYPES),
        ),
        (
            "Responses content and output parts relayed byte-for-byte",
            set(scrub._BINARY_PART_TYPES),
        ),
    ],
)
def test_documented_gateway_lists_match_the_code(label: str, expected: set[str]) -> None:
    assert _documented_set(label) == expected


def test_documented_typed_item_fields_match_the_code() -> None:
    assert _documented_set("Responses typed item fields scanned") == _typed_field_pairs()


def test_documented_lists_are_not_empty() -> None:
    # A parsing bug that returned empty sets on both sides would make every
    # comparison above pass vacuously.
    assert _documented_set("Responses items relayed byte-for-byte")
    assert _typed_field_pairs()


def _overrides(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text())
    overrides = loaded["pii"]["anonymization"]["entity_overrides"]
    assert isinstance(overrides, dict)
    return overrides


@pytest.mark.parametrize("path", SHIPPED_CONFIGS, ids=lambda p: p.name)
def test_shipped_configs_mask_cards_and_redact_secrets(path: Path) -> None:
    """docs/configuration.md and the README state that the shipped configs mask
    CREDIT_CARD and redact SECRET, and that both are therefore never restored."""
    overrides = _overrides(path)
    assert overrides["CREDIT_CARD"]["method"] == "mask"
    assert overrides["SECRET"]["method"] == "redact"
    # Every other type keeps the reversible default, as documented.
    assert set(overrides) == {"CREDIT_CARD", "SECRET"}


@pytest.mark.parametrize("path", SHIPPED_CONFIGS, ids=lambda p: p.name)
def test_shipped_config_overrides_are_the_irreversible_methods(path: Path) -> None:
    """The documented consequence ("never restored") holds only because both
    methods are in the anonymizer's irreversible set."""
    methods = {override["method"] for override in _overrides(path).values()}
    assert methods <= set(Anonymizer._IRREVERSIBLE)


@pytest.mark.parametrize("path", SHIPPED_CONFIGS, ids=lambda p: p.name)
def test_shipped_configs_explain_the_irreversibility_in_place(path: Path) -> None:
    """An operator reading the YAML must see the tradeoff without opening the
    docs, so the comment above the block is part of the published claim."""
    text = path.read_text()
    head, _, _ = text.partition("    entity_overrides:")
    comment = head.rsplit("\n\n", 1)[-1]
    assert "irreversible" in comment
    assert "NOT restored" in comment
