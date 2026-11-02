"""pii.allow_list entries are checked at load time. An entry that cannot mean
what the operator thinks it means is a startup error, like every unsafe config:
a pattern that matches everything is a type switch, not a value exemption."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from privaite.config.schema import PIIConfig
from privaite.pii.allow_list import check_entry

# The recipe documented in docs/configuration.md, as the operator writes it.
SSH_RECIPE = """
pii:
  allow_list:
    - pattern: "[A-Za-z0-9.+_-]+@(openssh\\\\.com|libssh\\\\.org)"
    - value: "openssh.com"
    - value: "KexAlgorithms"
      entity_types: ["ORGANIZATION"]
"""


def test_the_list_is_empty_by_default():
    assert PIIConfig().allow_list == []


def test_the_documented_ssh_recipe_loads_as_written():
    config = PIIConfig(**yaml.safe_load(SSH_RECIPE)["pii"])
    assert [e.pattern or e.value for e in config.allow_list] == [
        "[A-Za-z0-9.+_-]+@(openssh\\.com|libssh\\.org)",
        "openssh.com",
        "KexAlgorithms",
    ]
    assert [e.entity_types for e in config.allow_list] == [None, None, ["ORGANIZATION"]]


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ({}, "exactly one of value or pattern"),
        ({"value": "a", "pattern": "a"}, "exactly one of value or pattern"),
        ({"value": ""}, "must not be empty"),
        ({"pattern": "("}, "invalid allow_list pattern"),
        ({"pattern": ".*"}, "empty string"),
        ({"pattern": r"\w*"}, "empty string"),
        ({"pattern": ".+@.+"}, "sample e-mail"),
        ({"pattern": r"[\s\S]+"}, "sample e-mail"),
        ({"pattern": r"\d[\d -]+"}, "sample e-mail"),
        ({"pattern": r"https?://\S+"}, "sample e-mail"),
        ({"value": "x", "entity_types": []}, "at least one type"),
        ({"value": "x", "entity_type": ["PERSON"]}, "Extra inputs are not permitted"),
    ],
)
def test_an_entry_that_cannot_mean_what_it_says_is_refused(entry: dict, reason: str):
    with pytest.raises(ValidationError, match=reason):
        PIIConfig(allow_list=[entry])


def test_a_refusal_names_the_rule_and_never_echoes_the_pattern():
    with pytest.raises(ValueError) as excinfo:
        check_entry(None, r"[\s\S]+", None)
    assert r"[\s\S]+" not in str(excinfo.value)


def test_narrow_patterns_and_exact_values_are_accepted():
    config = PIIConfig(
        allow_list=[
            {"pattern": r"[a-z0-9-]+@openssh\.com", "entity_types": ["EMAIL_ADDRESS"]},
            {"pattern": r"https://docs\.example\.com/\S*"},
            {"value": "4111 1111 1111 1111", "entity_types": ["CREDIT_CARD"]},
        ]
    )
    assert len(config.allow_list) == 3
