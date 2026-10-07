"""Measured tool-output boundary regressions and cases that must stay masked."""

from __future__ import annotations

import json

import pytest

from privaite.config.schema import AnonymizationConfig, DeanonymizationConfig
from privaite.pii.anonymizer import Anonymizer
from privaite.pii.boundaries import refine_boundary, refine_spans
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
        ("support_email=topsecret@example.com", "SECRET"),
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


# What the model and Presidio really returned on a .env file (0.7.1, shipped
# config): spans that ran into the next line, into the host of a URI, or over
# the variable name. With SECRET redacted, the line was lost for the agent.
ENV_FILE = (
    "DATABASE_URL=postgres://shop:demo-pass-4821@db.internal:5432/shop\n"
    "AWS_SECRET_ACCESS_KEY=demoSecretKey0000notARealKey1234567890ab\n"
    "PAYMENT_API_KEY=sk-demo-0000-not-a-real-key"
)


# Built from parts so the file never holds something a secret scanner reads as a key.
PEM_BLOCK = "\n".join(
    ["-----BEGIN PRIVATE " + "KEY-----", "notARealKeyLine0001", "notARealKeyLine0002"]
    + ["-----END PRIVATE " + "KEY-----"]
)


def refined(text, span, kind="SECRET"):
    return [e.text for e in refine_spans(text, detected(text, span, kind))]


def test_a_secret_that_ran_into_the_next_assignment_is_cut_at_the_line():
    swallowed = (
        "demo-pass-4821@db.internal:5432/shop\n"
        "AWS_SECRET_ACCESS_KEY=demoSecretKey0000notARealKey1234567890ab"
    )
    assert refined(ENV_FILE, swallowed) == [
        "demo-pass-4821",
        "demoSecretKey0000notARealKey1234567890ab",
    ]


def test_the_env_file_keeps_its_names_and_its_host():
    spans = [
        detected(ENV_FILE, "demo-pass-4821", "SECRET", "presidio"),
        detected(ENV_FILE, "sk-demo-0000-not-a-real-key", "SECRET", "presidio"),
        *refine_spans(
            ENV_FILE,
            detected(
                ENV_FILE,
                "demo-pass-4821@db.internal:5432/shop\n"
                "AWS_SECRET_ACCESS_KEY=demoSecretKey0000notARealKey1234567890ab",
                "SECRET",
            ),
        ),
    ]
    config = AnonymizationConfig(entity_overrides={"SECRET": {"method": "redact"}})
    out = Anonymizer(config).anonymize(
        ENV_FILE, merge_entities(spans, source_text=ENV_FILE), PIIMapping()
    )
    assert out == (
        "DATABASE_URL=postgres://shop:[SECRET]@db.internal:5432/shop\n"
        "AWS_SECRET_ACCESS_KEY=[SECRET]\n"
        "PAYMENT_API_KEY=[SECRET]"
    )


@pytest.mark.parametrize(
    ("text", "span", "kind", "expected"),
    [
        # The model took the variable name with the value.
        (
            "API_KEY=sk-demo-0000-not-a-real",
            "API_KEY=sk-demo-0000-not-a-real",
            "SECRET",
            ["sk-demo-0000-not-a-real"],
        ),
        ("export API_KEY=sk-demo-0000", "API_KEY=sk-demo-0000", "SECRET", ["sk-demo-0000"]),
        ("7: API_KEY = sk-demo-0000", "API_KEY = sk-demo-0000", "SECRET", ["sk-demo-0000"]),
        # Presidio's email pattern accepts "=" and took the whole line.
        (
            "SUPPORT_CONTACT=marie.dupont@example.com",
            "SUPPORT_CONTACT=marie.dupont@example.com",
            "EMAIL_ADDRESS",
            ["marie.dupont@example.com"],
        ),
        # A fragment of the variable name is not a value.
        ("SUPPORT_CONTACT=marie.dupont@example.com", "SUP", "SECRET", []),
        (
            "SUPPORT_EMAIL=topsecret@example.com",
            "SUPPORT_EMAIL=topsecret@example.com",
            "SECRET",
            ["topsecret@example.com"],
        ),
        # The host is not part of the password.
        (
            "DATABASE_URL=postgres://shop:demo-pass-4821@db.internal:5432/shop",
            "demo-pass-4821@db.internal:5432/shop",
            "SECRET",
            ["demo-pass-4821"],
        ),
        (
            "url: mysql://root:p@ss@127.0.0.1:3306/app",
            "p@ss@127.0.0.1:3306/app",
            "SECRET",
            ["p@ss"],
        ),
        # Trailing punctuation is not part of an address.
        (
            "to Marie <marie.dupont@example.com>, card",
            "marie.dupont@example.com>,",
            "EMAIL_ADDRESS",
            ["marie.dupont@example.com"],
        ),
        (
            "write to marie.dupont@example.com.",
            "marie.dupont@example.com.",
            "EMAIL_ADDRESS",
            ["marie.dupont@example.com"],
        ),
    ],
)
def test_env_names_hosts_and_trailing_punctuation_are_given_back(text, span, kind, expected):
    assert refined(text, span, kind) == expected
    for piece in refine_spans(text, detected(text, span, kind)):
        assert text[piece.start : piece.end] == piece.text


@pytest.mark.parametrize(
    ("text", "span"),
    [
        # A base32 seed with its padding is not NAME=value.
        ("JBSWY3DPEHPK3PXP====", "JBSWY3DPEHPK3PXP===="),
        ("JBSWY3DPEHPK3PXP=", "JBSWY3DPEHPK3PXP="),
        # Lower-case and mixed-case names stay ambiguous: untouched, as before.
        ("password=Pa=ss!word", "password=Pa=ss!word"),
        ("Pa=ss!word9", "Pa=ss!word9"),
        ("a=complicated>password!", "a=complicated>password!"),
        # Not at the start of a line.
        ("the value is API_KEY=sk-demo-0000", "API_KEY=sk-demo-0000"),
        # A query string can carry another credential: the tail is kept.
        (
            "DB=postgres://u:pw1234@h/db?sslpassword=hunter2abc",
            "pw1234@h/db?sslpassword=hunter2abc",
        ),
        # No userinfo: nothing to cut.
        ("see https://example.com/a@b for details", "example.com/a@b"),
        # A PEM block is one secret over many lines.
        (PEM_BLOCK, PEM_BLOCK),
    ],
)
def test_a_secret_span_is_left_whole_when_the_structure_is_not_certain(text, span):
    assert refined(text, span) == [span]


def test_a_cut_never_uncovers_a_character_of_the_value():
    """Whatever the model span, only the name, the host and line breaks come back."""
    pieces = refine_spans(
        ENV_FILE,
        detected(ENV_FILE, ENV_FILE[ENV_FILE.index("shop:demo") :], "SECRET"),
    )
    covered = "".join(p.text for p in pieces)
    for value in (
        "demo-pass-4821",
        "demoSecretKey0000notARealKey1234567890ab",
        "sk-demo-0000-not-a-real-key",
    ):
        assert value in covered


def test_windows_line_endings_are_cut_the_same_way():
    text = (
        "DB_URL=postgres://shop:demo-pass-4821@db.internal/shop\r\nAPI_TOKEN=demo0000token1234\r\n"
    )
    span = "demo-pass-4821@db.internal/shop\r\nAPI_TOKEN=demo0000token1234\r\n"
    assert refined(text, span) == ["demo-pass-4821", "demo0000token1234"]


def test_a_span_that_starts_on_a_line_break_loses_only_the_break():
    text = "note\nAPI_KEY=sk-demo-0000"
    assert refined(text, "\nAPI_KEY=sk-demo-0000") == ["sk-demo-0000"]


def test_a_lower_case_email_field_still_gives_its_name_back():
    """The September rule: a name that says it holds an address, in any case."""
    text = "support_email=camille@example.com"
    entity = refine_boundary(text, detected(text, text))
    assert entity.text == "camille@example.com"


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("call +33 6 12 34 56 78\nor +33 6 98 76 54 32", "PHONE_NUMBER"),
        ("https://example.com/users/camille", "URL"),
    ],
)
def test_spans_with_no_recognizable_wrapper_are_untouched(text, kind):
    original = detected(text + "\nnext line", text, kind)
    assert refine_boundary(text + "\nnext line", original) == original
    original = detected(text, text, kind)
    assert refine_boundary(text, original) == original
    assert refine_spans(text, original) == [original]


def test_a_span_outside_the_text_is_returned_as_is():
    stray = PIIEntity("SECRET", "x", 40, 41, 0.9, "onnx")
    assert refine_spans("short", stray) == [stray]
    assert refine_boundary("short", stray) == stray


# The model on a .env file: fragments in the name, spans that start mid-name,
# an empty span. Measured on 150 generated files with 0.7.1: 374 of 1327
# variable names and 35 lines were lost to them.
@pytest.mark.parametrize(
    ("text", "span", "kind"),
    [
        ("HOST=0.0.0.0", "HOST", "SECRET"),
        ("TZ_NAME=Europe/Paris", "TZ_NAME", "URL"),
        ("SUPPORT_CONTACT=ops@example.com", "SUP", "EMAIL_ADDRESS"),
        ("SUPPORT_CONTACT=ops@example.com", "SUPPORT_CONTACT=", "SECRET"),
        ("CACHE_TTL=3600", "TL=", "SECRET"),
        ("export API_KEY=sk-demo-0000", "export API", "URL"),
    ],
)
def test_a_model_span_that_stays_in_an_env_name_is_dropped(text, span, kind):
    assert refine_spans(text, detected(text, span, kind)) == []


@pytest.mark.parametrize(
    ("text", "span", "kind", "expected"),
    [
        (
            "SUPPORT_CONTACT=ops@acme.example.org",
            "PORT_CONTACT=ops@acme.example",
            "URL",
            "ops@acme.example",
        ),
        ("HOST=0.0.0.0", "=0.0.0.0", "URL", "0.0.0.0"),
        ("TZ_NAME=Europe/Paris", "TZ_NAME=Europe/", "EMAIL_ADDRESS", "Europe/"),
        ("export API_KEY=sk-demo-0000", "export API_KEY=sk-demo-0000", "SECRET", "sk-demo-0000"),
    ],
)
def test_a_model_span_that_starts_in_an_env_name_is_cut_to_the_value(text, span, kind, expected):
    assert [e.text for e in refine_spans(text, detected(text, span, kind))] == [expected]


@pytest.mark.parametrize("kind", ["PERSON", "LOCATION", "DATE_TIME", "PHONE_NUMBER"])
def test_other_types_in_an_env_name_are_kept(kind):
    """A person can be a key (MARIE_DUPONT=admin): only types that cannot be a
    name on their own are dropped there."""
    text = "MARIE_DUPONT=admin"
    original = detected(text, "MARIE_DUPONT", kind)
    assert refine_spans(text, original) == [original]


def test_an_empty_model_span_is_dropped():
    """It covers nothing, and the anonymizer would insert a placeholder there."""
    text = "mysql://root:demo-pass-4821@127.0.0.1:3306/app"
    empty = PIIEntity("SECRET", "", 28, 28, 0.9, "onnx")
    assert refine_spans(text, empty) == []


def test_the_colon_before_a_uri_password_is_given_back():
    text = "BROKER_URL=mysql://root:demo-pass-4821@127.0.0.1:3306/app"
    assert refined(text, ":demo-pass-4821") == ["demo-pass-4821"]
    assert refined(text, ":demo-pass-4821@127.0.0.1:3306/app") == ["demo-pass-4821"]
    # No user name: the colon is still the separator.
    assert refined("redis://:demo-pass-4821@cache.internal", ":demo-pass-4821") == [
        "demo-pass-4821"
    ]
    # A colon inside the password stays.
    assert refined("mysql://root:demo:pass:4821@db/app", "demo:pass:4821") == ["demo:pass:4821"]


def test_a_url_or_an_email_that_ran_into_the_next_assignment_is_cut_too():
    text = "SITE=https://example.com/a\nADMIN_EMAIL=ops@example.com"
    assert [e.text for e in refine_spans(text, detected(text, text, "URL"))] == [
        "https://example.com/a",
        "ops@example.com",
    ]


def test_the_at_sign_that_closes_the_userinfo_is_not_a_secret():
    """The model sometimes returns the delimiter alone, which left an empty
    span behind and a second placeholder next to the password."""
    text = "BROKER_URL=mysql://root:demo-pass-4821@127.0.0.1:3306/app"
    at = text.index("@")
    assert refine_spans(text, PIIEntity("SECRET", "@", at, at + 1, 0.9, "onnx")) == []
    # With the host behind it, the host stays covered: only the delimiter goes.
    assert refined(text, "@127.0.0.1:3306/app") == ["127.0.0.1:3306/app"]
    # An "@" that closes nothing is left alone.
    assert refined("notify @ noon", "@") == ["@"]
    # Outside the model path nothing is ever dropped.
    lone = PIIEntity("SECRET", "@", at, at + 1, 0.9, "presidio")
    assert refine_boundary(text, lone) == lone
    empty_password = "mysql://root:@127.0.0.1:3306/app"
    assert refine_spans(empty_password, detected(empty_password, ":@127.0.0.1", "SECRET")) == []


@pytest.mark.parametrize(
    ("span", "kind"),
    [
        ("AKIADEMO0000EXAMPLE", "SECRET"),
        ("AKIADEMO0000EXAMPLE=enabled", "SECRET"),
        ("DEMO0000EXAMPLE", "SECRET"),
    ],
)
def test_a_name_that_looks_like_a_token_is_not_treated_as_a_name(span, kind):
    """Long, one word, with a digit: it can be a value written before an "=",
    so nothing is dropped or cut there."""
    text = "AKIADEMO0000EXAMPLE=enabled"
    original = detected(text, span, kind)
    assert refine_spans(text, original) == [original]


@pytest.mark.parametrize("name", ["AWS_S3_SECRET", "OAUTH2_CLIENT_SECRET", "DB2_PASSWORD", "S3KEY"])
def test_ordinary_names_with_a_digit_are_still_names(name):
    text = f"{name}=demo-pass-4821"
    assert [e.text for e in refine_spans(text, detected(text, text, "SECRET"))] == [
        "demo-pass-4821"
    ]
    assert refine_spans(text, detected(text, name, "SECRET")) == []


@pytest.mark.parametrize(
    "prefix",
    [
        "1\t",
        "     12\t",
        "12\u2192",
        "00012| ",
        "12:",
        "+",
        "-",
        "      - ",
        "ENV ",
        "ARG ",
        "env ",
    ],
)
def test_the_env_name_is_found_behind_a_gutter_or_a_marker(prefix):
    """A read tool numbers the lines, a diff marks them, a compose file lists
    them: the name is still a name."""
    text = f"APP_ENV=production\n{prefix}SUPPORT_CONTACT=ops@example.com\n{prefix}PORT=8080"
    assert refine_spans(text, detected(text, "SUP", "SECRET")) == []
    assert [
        e.text for e in refine_spans(text, detected(text, "SUPPORT_CONTACT=ops@example.com", "URL"))
    ] == ["ops@example.com"]
    line = f"{prefix}DATABASE_URL=postgres://shop:demo-pass-4821@db.internal:5432/shop"
    run = f"{line}\n{prefix}AWS_SECRET_ACCESS_KEY=demoSecretKey0000notARealKey"
    span = "demo-pass-4821@db.internal:5432/shop\n" + prefix + "AWS_SECRET_ACCESS_KEY=demoSecretKey"
    assert [e.text for e in refine_spans(run, detected(run, span, "SECRET"))] == [
        "demo-pass-4821",
        "demoSecretKey",
    ]
