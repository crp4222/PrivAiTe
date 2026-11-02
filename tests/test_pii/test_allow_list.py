"""The allow list lets a merged span through only when every raw detection
under it is listed, in full, on its own type. Synthetic entities, no detector."""

from __future__ import annotations

import pytest

from privaite.config.schema import AllowListEntry
from privaite.pii.allow_list import (
    ALWAYS_PROTECTED,
    MAX_ALLOWED_LENGTH,
    AllowListVerdict,
    apply_allow_list,
    compile_rules,
)
from privaite.pii.entity import PIIEntity, merge_entities

ALGO = "chacha20-poly1305@openssh.com"
MAIL = "jane.doe@example.org"
TEXT = f"Ciphers {ALGO} and mail {MAIL}"


def entity(value: str, etype: str = "EMAIL_ADDRESS", source: str = "presidio", score: float = 1.0):
    start = TEXT.index(value)
    return PIIEntity(etype, value, start, start + len(value), score, source)


def rules(*entries: dict):
    return compile_rules([AllowListEntry(**entry) for entry in entries])


def split(raw, rule_set, *, strategy: str = "union", protected=ALWAYS_PROTECTED):
    merged = merge_entities(list(raw), strategy=strategy, source_text=TEXT)
    kept, allowed, _not_allowed = apply_allow_list(TEXT, merged, raw, rule_set, protected)
    return [e.text for e in kept], [e.text for e in allowed]


def test_without_rules_everything_is_kept_and_nothing_is_spent():
    raw = [entity(ALGO), entity(MAIL)]
    assert split(raw, ()) == ([ALGO, MAIL], [])


def test_a_value_entry_lets_exactly_that_string_through():
    raw = [entity(ALGO), entity(MAIL)]
    assert split(raw, rules({"value": ALGO})) == ([MAIL], [ALGO])
    assert split(raw, rules({"value": ALGO.upper()})) == ([ALGO, MAIL], [])


def test_a_pattern_must_match_the_whole_detection():
    raw = [entity(ALGO), entity(MAIL)]
    assert split(raw, rules({"pattern": "chacha20"})) == ([ALGO, MAIL], [])
    assert split(raw, rules({"pattern": r"[a-z0-9-]+@openssh\.com"})) == ([MAIL], [ALGO])


def test_a_span_that_merely_contains_an_allowed_value_stays_redacted():
    wider = f"Ciphers {ALGO}"
    raw = [PIIEntity("LOCATION", wider, 0, len(wider), 0.85, "spacy"), entity(ALGO)]
    # The merge widens the e-mail into the LOCATION span; the e-mail entry
    # covers one of the two detections under it, not both.
    assert split(raw, rules({"value": ALGO})) == ([wider], [])


def test_entity_types_scope_an_entry_to_the_detected_type():
    raw = [entity(ALGO)]
    assert split(raw, rules({"value": ALGO, "entity_types": ["URL"]})) == ([ALGO], [])
    assert split(raw, rules({"value": ALGO, "entity_types": ["EMAIL_ADDRESS"]})) == ([], [ALGO])


@pytest.mark.parametrize("strategy", ["union", "intersection"])
def test_an_allowed_email_never_unmasks_a_name_another_detector_found_inside_it(strategy: str):
    inner = "chacha20-poly1305"
    raw = [entity(ALGO, "EMAIL_ADDRESS", "presidio"), entity(inner, "PERSON", "onnx", 0.6)]
    assert split(raw, rules({"value": ALGO}), strategy=strategy) == ([ALGO], [])
    both = rules({"value": ALGO}, {"value": inner, "entity_types": ["PERSON"]})
    assert split(raw, both, strategy=strategy) == ([], [ALGO])


def test_the_detection_that_kept_a_listed_value_redacted_is_reported():
    host = "openssh.com"
    raw = [entity(ALGO), entity(host, "URL", "presidio", 0.5), entity(MAIL)]
    merged = merge_entities(raw, source_text=TEXT)
    verdict = apply_allow_list(TEXT, merged, raw, rules({"value": ALGO}))
    assert [e.text for e in verdict.redacted] == [ALGO, MAIL]
    assert verdict.allowed == []
    assert [(e.entity_type, e.text) for e in verdict.not_allowed] == [("URL", host)]
    # Nothing under the span was listed: nothing stood in the way, nothing to report.
    verdict = apply_allow_list(TEXT, merged, raw, rules({"value": "unrelated"}))
    assert verdict.not_allowed == []


def test_two_detections_of_one_span_must_each_be_allowed_on_their_own_type():
    raw = [entity(ALGO, "EMAIL_ADDRESS", "presidio"), entity(ALGO, "URL", "onnx", 0.7)]
    assert split(raw, rules({"value": ALGO, "entity_types": ["EMAIL_ADDRESS"]})) == ([ALGO], [])
    assert split(raw, rules({"value": ALGO, "entity_types": ["EMAIL_ADDRESS", "URL"]})) == (
        [],
        [ALGO],
    )


def test_a_credential_needs_an_entry_that_names_secret():
    raw = [entity(ALGO, "SECRET", "secret-rules")]
    assert split(raw, rules({"value": ALGO})) == ([ALGO], [])
    assert split(raw, rules({"pattern": ".+@openssh.com"})) == ([ALGO], [])
    assert split(raw, rules({"value": ALGO, "entity_types": ["SECRET"]})) == ([], [ALGO])


def test_a_blocked_type_is_protected_the_same_way():
    raw = [entity(ALGO)]
    protected = ALWAYS_PROTECTED | {"EMAIL_ADDRESS"}
    assert split(raw, rules({"value": ALGO}), protected=protected) == ([ALGO], [])
    explicit = rules({"value": ALGO, "entity_types": ["EMAIL_ADDRESS"]})
    assert split(raw, explicit, protected=protected) == ([], [ALGO])


def test_a_detection_over_the_length_cap_is_never_allowed():
    for length, expected_allowed in ((MAX_ALLOWED_LENGTH, True), (MAX_ALLOWED_LENGTH + 1, False)):
        value = "a" * length
        text = f"k={value}"
        raw = [PIIEntity("URL", value, 2, 2 + length, 0.9, "stub")]
        kept, allowed, _ = apply_allow_list(text, raw, raw, rules({"pattern": "a+"}))
        assert (len(allowed) == 1) is expected_allowed
        assert (len(kept) == 1) is not expected_allowed


def test_a_detection_the_merge_dropped_plays_no_part():
    # With merge_strategy: intersection an unconfirmed detection is not in the
    # merged result; it then neither blocks nor is reported.
    lone = entity(MAIL)
    merged = [entity(ALGO)]
    verdict = apply_allow_list(TEXT, merged, [entity(ALGO), lone], rules({"value": ALGO}))
    assert verdict == AllowListVerdict([], merged, [])


def test_a_merged_span_no_detection_touches_is_kept():
    merged = [entity(ALGO)]
    verdict = apply_allow_list(TEXT, merged, [], rules({"value": ALGO}))
    assert verdict == AllowListVerdict(merged, [], [])


def test_overlapping_or_unordered_merged_spans_take_the_careful_path():
    # Not a shape merge_entities produces, but the verdict must not depend on it.
    first, second = entity(ALGO), entity(MAIL)
    raw = [first, second]
    kept, allowed, _ = apply_allow_list(TEXT, [second, first], raw, rules({"value": ALGO}))
    assert [e.text for e in kept] == [MAIL]
    assert [e.text for e in allowed] == [ALGO]
    wide = PIIEntity("LOCATION", TEXT, 0, len(TEXT), 0.5, "spacy")
    kept, allowed, _ = apply_allow_list(TEXT, [first, wide], [first, wide], rules({"value": ALGO}))
    assert [e.text for e in kept] == [ALGO, TEXT]
    assert allowed == []
