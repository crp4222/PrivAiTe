"""Measured tool-output boundary regressions and cases that must stay masked."""

from __future__ import annotations

import json

import pytest

from privaite.config.schema import AnonymizationConfig, DeanonymizationConfig
from privaite.pii.anonymizer import Anonymizer
from privaite.pii.boundaries import refine_boundary
from privaite.pii.deanonymizer import DeAnonymizer
from privaite.pii.entity import PIIEntity, merge_entities
from privaite.pii.mapping import PIIMapping


def detected(text, span, kind="EMAIL_ADDRESS", source="onnx"):
    start = text.index(span)
    return PIIEntity(kind, span, start, start + len(span), 0.99, source)


@pytest.mark.parametrize("prefix", ["", "7: ", "  7: ", "export "])
def test_email_assignment_preserves_label_and_canonicalizes_mapping(prefix):
    value = "camille.martin@example.com"
    text = prefix + "SUPPORT_EMAIL=" + value
    raw = detected(text, "SUPPORT_EMAIL=" + value, source="presidio")
    refined = refine_boundary(text, raw)
    assert refined.text == value
    assert text[refined.start : refined.end] == value
    mapping = PIIMapping()
    anonymized = Anonymizer(AnonymizationConfig()).anonymize(text, [refined], mapping)
    assert anonymized == prefix + "SUPPORT_EMAIL=<EMAIL_ADDRESS_1>"
    assert (
        DeAnonymizer(DeanonymizationConfig()).deanonymize("Email: <EMAIL_ADDRESS_1>", mapping)
        == "Email: " + value
    )


@pytest.mark.parametrize(
    ("text", "span", "kind", "expected"),
    [
        (
            '{"phone": "+33 6 12 34 56 78"}',
            '"+33 6 12 34 56 78',
            "PHONE_NUMBER",
            "+33 6 12 34 56 78",
        ),
        (
            '{"email": "camille@example.com"}',
            'camille@example.com"',
            "EMAIL_ADDRESS",
            "camille@example.com",
        ),
        (
            "Contact <camille@example.com>",
            "camille@example.com>",
            "EMAIL_ADDRESS",
            "camille@example.com",
        ),
        (
            "Contact <camille@example.com>",
            "<camille@example.com>",
            "EMAIL_ADDRESS",
            "camille@example.com",
        ),
        (
            "Email 'camille@example.com'",
            "'camille@example.com",
            "EMAIL_ADDRESS",
            "camille@example.com",
        ),
        (
            "<path>/Users/Camille/project/config.json</path>",
            "<path>/Users/Camille/project/config.json",
            "URL",
            "/Users/Camille/project/config.json",
        ),
    ],
)
def test_only_recognized_delimiters_are_removed(text, span, kind, expected):
    entity = refine_boundary(text, detected(text, span, kind))
    assert entity.text == expected
    assert text[entity.start : entity.end] == expected


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("support+tag=demo@example.com", "EMAIL_ADDRESS"),
        ("user=tag@example.com", "EMAIL_ADDRESS"),
        ("My address is SUPPORT_EMAIL=tag@example.com", "EMAIL_ADDRESS"),
        ('"quoted=local"@example.com', "EMAIL_ADDRESS"),
        ('"unpaired@example.com', "EMAIL_ADDRESS"),
        ("unpaired@example.com>", "EMAIL_ADDRESS"),
        (r"\"escaped@example.com\"", "EMAIL_ADDRESS"),
        ('"a=complicated>password!"', "SECRET"),
        ("<path>/Users/private/file</path>", "SECRET"),
        ("SUPPORT_EMAIL=topsecret@example.com", "SECRET"),
        ("<arbitrary_operator_span>", "CUSTOM_ENTITY"),
    ],
)
def test_ambiguous_punctuation_and_secret_spans_are_untouched(text, kind):
    original = detected(text, text, kind)
    assert refine_boundary(text, original) == original


def test_refinement_precedes_union_and_restores_valid_json():
    value = "+33 6 12 34 56 78"
    text = json.dumps({"phone": value})
    raw = [
        detected(text, '"' + value, "PHONE_NUMBER"),
        detected(text, value, "PHONE_NUMBER", "presidio"),
    ]
    merged = merge_entities([refine_boundary(text, e) for e in raw], source_text=text)
    mapping = PIIMapping()
    anonymized = Anonymizer(AnonymizationConfig()).anonymize(text, merged, mapping)
    assert json.loads(anonymized) == {"phone": "<PHONE_NUMBER_1>"}
    restored = DeAnonymizer(DeanonymizationConfig()).deanonymize(anonymized, mapping)
    assert json.loads(restored) == {"phone": value}


def test_union_still_masks_uncovered_secret_remainder():
    text = "prefix-very-sensitive-tail"
    raw = [detected(text, text, "SECRET"), detected(text, "very-sensitive", "SECRET", "presidio")]
    merged = merge_entities([refine_boundary(text, e) for e in raw], source_text=text)
    assert len(merged) == 1 and merged[0].text == text


def test_same_email_in_different_wrappers_has_one_placeholder():
    mapping = PIIMapping()
    anonymizer = Anonymizer(AnonymizationConfig())
    for text, span in [
        ("SUPPORT_EMAIL=camille@example.com", "SUPPORT_EMAIL=camille@example.com"),
        ("<camille@example.com>", "camille@example.com>"),
        ('"camille@example.com"', "camille@example.com"),
    ]:
        anonymized = anonymizer.anonymize(
            text, [refine_boundary(text, detected(text, span))], mapping
        )
        assert "<EMAIL_ADDRESS_1>" in anonymized
    assert mapping.count == 1
