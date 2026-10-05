"""Synthetic credentials only; tests do not load model weights or call a provider."""

from __future__ import annotations

import json

import pytest

from privaite.config.schema import PIIConfig
from privaite.gateway.scrub import scrub_anthropic_request, scrub_responses_request
from privaite.pii.engine import PIIBlockedError, PIIEngine
from privaite.pii.entity import PIIEntity, merge_entities
from privaite.pii.recognizer_secret import StructuredSecretRecognizer


def encoded_cli_call(command: str, value: str, encoding: str) -> tuple[str, str]:
    def quoted_content(text: str, quote: str) -> str:
        return text.replace("\\", "\\\\").replace(quote, "\\" + quote)

    if encoding == "js-single":
        literal = "'" + quoted_content(command, "'") + "'"
        encoded_value = quoted_content(value, "'")
    else:
        literal = json.dumps(command)
        encoded_value = quoted_content(value, '"')
    call = f"text(await tools.exec_command({{cmd:{literal},max_output_tokens:1000}}));"
    if encoding == "json-command":
        call = json.dumps({"cmd": command, "max_output_tokens": 1000})
    elif encoding == "json-js":
        call = json.dumps(call)
        encoded_value = quoted_content(encoded_value, '"')
    return call, encoded_value


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ('export OPENAI_API_KEY="synthetic-only-123"', "synthetic-only-123"),
        ("presented_key=fixture-only smtp_secret='other fake'", "fixture-only"),
        ("presented_key=fixture-only smtp_secret='other fake'", "other fake"),
        ('{"apiKey": "fake\\"quoted\\\\value"}', 'fake\\"quoted\\\\value'),
        ("'client_secret': 'fake; space, and punctuation!'", "fake; space, and punctuation!"),
        ("DB_PASSWORD: 'demo-only'", "demo-only"),
        ("password=demo-only, status=ok", "demo-only,"),
        ("password=demo,only;{with}[punctuation]=!", "demo,only;{with}[punctuation]=!"),
        ("postgresql://service:fake%40password!@db.example.invalid:5432/app", "fake%40password!"),
        ("Authorization: Bearer synthetic.token-only==", "synthetic.token-only=="),
        ("Authorization: Bearer " + "a" * 4096, "a" * 4096),
    ],
)
def test_reports_only_the_value(text: str, value: str):
    recognizer = StructuredSecretRecognizer()
    spans = recognizer.analyze(text, ["SECRET"])
    assert any(text[s.start : s.end] == value for s in spans)
    for span in spans:
        assert span.entity_type == "SECRET"
        assert span.end > span.start


@pytest.mark.parametrize("language", ["en", "fr"])
@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("connect --password demo-only", "demo-only"),
        ("connect --mot-de-passe demo-only", "demo-only"),
        ("connect --password=demo-only", "demo-only"),
        ("connect --mot-de-passe=demo-only", "demo-only"),
        ("connect --password 'demo only; with spaces!'", "demo only; with spaces!"),
        ('connect --mot-de-passe "demo only; with spaces!"', "demo only; with spaces!"),
        ("connect --password='demo-only'", "demo-only"),
        ('connect --mot-de-passe="demo-only"', "demo-only"),
        ('connect --password\t"demo-only" --user ordinary-name', "demo-only"),
    ],
)
def test_cli_password_options_report_only_the_value(language: str, text: str, value: str):
    spans = StructuredSecretRecognizer(supported_language=language).analyze(text, ["SECRET"])
    assert len(spans) == 1
    assert text[spans[0].start : spans[0].end] == value
    assert spans[0].entity_type == "SECRET"


@pytest.mark.parametrize("flag", ["--password", "--mot-de-passe"])
@pytest.mark.parametrize("encoding", ["js-double", "js-single", "json-command", "json-js"])
@pytest.mark.parametrize(
    ("quote", "value"),
    [
        ('"', "demo-only-cli-password"),
        ("'", "demo only with spaces"),
        ('"', r"demo \"with quotes\" C:\\fixture\\path"),
        ('"', r"demo trailing\\"),
        ("'", 'demo "with quotes" C:\\fixture\\path'),
        ("'", "demo trailing\\"),
    ],
)
def test_encoded_cli_password_reports_the_complete_original_value(
    flag: str, encoding: str, quote: str, value: str
):
    command = f"python3 coffre.py {flag}={quote}{value}{quote} --user ordinary-name"
    text, encoded_value = encoded_cli_call(command, value, encoding)
    spans = StructuredSecretRecognizer().analyze(text, ["SECRET"])
    assert len(spans) == 1
    assert text[spans[0].start : spans[0].end] == encoded_value
    assert spans[0].entity_type == "SECRET"


@pytest.mark.parametrize("encoding", ["js-double", "js-single", "json-command", "json-js"])
@pytest.mark.parametrize("separator", [" = ", " =", "  = "])
@pytest.mark.parametrize("flag", ["--password", "--mot-de-passe"])
def test_encoded_cli_password_preserves_equals_after_whitespace(
    encoding: str, separator: str, flag: str
):
    value = "demo-only-cli-password"
    command = f'connect {flag}{separator}"{value}" --user ordinary-name'
    text, encoded_value = encoded_cli_call(command, value, encoding)
    spans = StructuredSecretRecognizer().analyze(text, ["SECRET"])
    assert len(spans) == 1
    assert text[spans[0].start : spans[0].end] == encoded_value


@pytest.mark.parametrize("encoding", ["js-double", "js-single", "json-command", "json-js"])
def test_encoded_cli_password_keeps_multiple_disjoint_secret_spans(encoding: str):
    first = r"demo \"first quote\" C:\\fixture\\one"
    second = 'demo "second quote" C:\\fixture\\two'
    command = f"one --password \"{first}\" --user ordinary; two --mot-de-passe '{second}'"
    text, encoded_first = encoded_cli_call(command, first, encoding)
    _, encoded_second = encoded_cli_call(command, second, encoding)
    spans = StructuredSecretRecognizer().analyze(text, ["SECRET"])
    assert len(spans) == 2
    assert [text[s.start : s.end] for s in spans] == [encoded_first, encoded_second]
    assert spans[0].end <= spans[1].start


@pytest.mark.parametrize("encoding", ["js-double", "js-single", "json-command", "json-js"])
@pytest.mark.parametrize(
    "command",
    [
        'connect --user "ordinary-name"',
        'connect --password-length "ordinary-name"',
        'connect --mot-de-passe-file "ordinary-name"',
        'ordinary-name--password="demo-only"',
        'ordinary-name--mot-de-passe="demo-only"',
        "connect --password \"\" --mot-de-passe ''",
        'connect --password "unterminated',
        "connect --mot-de-passe 'unterminated",
    ],
)
def test_encoded_cli_password_does_not_report_partial_escapes_or_ordinary_names(
    encoding: str, command: str
):
    text, _ = encoded_cli_call(command, "demo-only", encoding)
    assert StructuredSecretRecognizer().analyze(text, ["SECRET"]) == []


@pytest.mark.parametrize(
    "text",
    [
        "connect --user ordinary-name",
        "password reset for ordinary-name",
        "mot-de-passe reset for ordinary-name",
        "connect --password-length 16 --passwordless enabled",
        "connect --mot-de-passe-file ordinary-name",
        "ordinary-name--password=demo-only",
        "ordinary-name--mot-de-passe=demo-only",
        "connect --password",
        "connect --password=\"\" --mot-de-passe=''",
        'connect --password "unterminated',
    ],
)
def test_cli_password_options_leave_flags_and_ordinary_names_alone(text: str):
    assert StructuredSecretRecognizer().analyze(text, ["SECRET"]) == []


@pytest.mark.parametrize(
    "text",
    [
        # Help output and prose: the word after the flag is not a value.
        "Use --password to set the password for the account.",
        "The --password option is required when --user is given.",
        "  -W, --password           force password prompt",
        "  -p, --password string   Password or Personal Access Token",
        "  --password TEXT  The password (prompted if omitted).",
        "Pass --password PASSWORD on the command line.",
        "--password <value>   Password for the database",
        "connect --password=<password> --user ordinary-name",
        "connect --password=[PASSWORD]",
        "L'option --mot-de-passe est requise pour ouvrir le coffre.",
        # A variable reference is not the secret, and rewriting it breaks the command.
        "connect --password $DB_PASSWORD",
        'connect --password "$DB_PASSWORD"',
        "connect --password=${DB_PASSWORD}",
        'connect --password "${DB_PASSWORD}" --user ordinary-name',
        'connect --password "$(cat /run/secrets/db)"',
        "connect --password $(cat /run/secrets/db)",
    ],
)
def test_cli_secret_options_ignore_help_text_and_variable_references(text: str):
    assert StructuredSecretRecognizer().analyze(text, ["SECRET"]) == []


@pytest.mark.parametrize(
    ("text", "value"),
    [
        # Weak passwords are still values: only help words and metavariables are skipped.
        ("connect --password changeme", "changeme"),
        ("connect --password admin --user ordinary-name", "admin"),
        ("connect --password Passw0rd", "Passw0rd"),
        ("connect --password=TOPSECRET1", "TOPSECRET1"),
        ("connect --password '$DB_PASSWORD'", "$DB_PASSWORD"),
        ('connect --password "$uper$ecret1"', "$uper$ecret1"),
        # The option names follow the assignment vocabulary.
        ("connect --passwd demo-only", "demo-only"),
        ("connect --api-key demo-only-key-1", "demo-only-key-1"),
        ("connect --api_key=demo-only-key-1", "demo-only-key-1"),
        ("connect --client-secret 'demo only'", "demo only"),
        ("connect --access-token demo-only-token-1", "demo-only-token-1"),
        # A bounded prefix, as environment variable names already allow.
        ("connect --db-password demo-only", "demo-only"),
        ("connect --SMTP_PASSWORD=demo-only", "demo-only"),
        ("connect --password-file demo-only", None),
    ],
)
def test_cli_secret_options_keep_real_values_and_share_the_field_names(text: str, value):
    results = StructuredSecretRecognizer().analyze(text, ["SECRET"])
    assert [text[r.start : r.end] for r in results] == ([value] if value else [])


@pytest.mark.parametrize(
    "text",
    [
        "key=customer-id token_count=32 password_length=16 api_key_name=development",
        "monkey=value public_key_fingerprint=abc passwordless=true",
        "https://example.invalid/path user@example.invalid",
        "postgresql://db.example.invalid:5432/app owner@example.invalid",
        "https://example.invalid/path:word followed by owner@example.invalid",
        "password=\"\" api_key=''",
        'password="unterminated',
        "a" * 100_000 + "_API_KEY_SUFFIX=value",
    ],
)
def test_leaves_noncredential_diagnostics_alone(text: str):
    assert StructuredSecretRecognizer().analyze(text, ["SECRET"]) == []


class SecretAndEmailDetector:
    """A higher-confidence email intentionally overlaps the URI password."""

    name = "presidio"

    def __init__(self):
        self.calls = 0

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        self.calls += 1
        entities = [
            PIIEntity(s.entity_type, text[s.start : s.end], s.start, s.end, s.score, self.name)
            for s in StructuredSecretRecognizer().analyze(text, ["SECRET"])
        ]
        if "postgresql://" in text:
            start = text.index("synthetic-password")
            end = text.index(":5432")
            entities.append(PIIEntity("EMAIL_ADDRESS", text[start:end], start, end, 1.0, "onnx"))
        return entities


def make_engine(*, method: str = "redact", block: list[str] | None = None):
    engine = PIIEngine(
        PIIConfig(
            preset=None,
            anonymization={"entity_overrides": {"SECRET": {"method": method}}},
            block_entities=block or [],
            detection_cache={"enabled": True},
        )
    )
    detector = SecretAndEmailDetector()
    engine.detectors = [detector]
    return engine, detector


URI = "postgresql://service:synthetic-password@db.example.invalid:5432/app"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["placeholder", "redact", "mask"])
async def test_codex_cli_password_is_scrubbed_after_previous_tool_output(method: str):
    value = "demo-only-cli-password"
    command = (
        "text(await tools.exec_command({cmd:'python3 coffre.py "
        '--mot-de-passe "demo-only-cli-password"\',max_output_tokens:1000}));\n'
    )
    engine, detector = make_engine(method=method)
    body = {
        "input": [
            {
                "type": "custom_tool_call_output",
                "output": [{"type": "text", "text": f'password="{value}"'}],
            },
            {"type": "custom_tool_call", "name": "exec", "input": command},
        ]
    }
    calls = []
    for _ in range(2):
        out, mapping = await scrub_responses_request(engine, body)
        assert value not in json.dumps(out)
        scrubbed = out["input"][1]["input"]
        assert "--mot-de-passe" in scrubbed
        assert mapping.get_entity_type(value) == "SECRET"
        if method == "placeholder":
            assert scrubbed == command.replace(value, mapping.get_fake(value))
            assert engine.deanonymizer.deanonymize(scrubbed, mapping) == command
        else:
            assert mapping.get_all_fakes() == {}
        calls.append(detector.calls)
    assert calls[0] == calls[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["placeholder", "redact", "mask"])
@pytest.mark.parametrize("encoding", ["js-double", "js-single", "json-command", "json-js"])
@pytest.mark.parametrize(
    ("quote", "value"),
    [
        ('"', "demo-only-cli-password"),
        ('"', r"demo \"with quotes\" C:\\fixture\\path"),
        ("'", 'demo "with quotes" C:\\fixture\\path'),
        ("'", "demo trailing\\"),
    ],
)
async def test_codex_encoded_cli_password_is_scrubbed_on_cold_and_cached_requests(
    method: str, encoding: str, quote: str, value: str
):
    shell = f"python3 coffre.py --mot-de-passe {quote}{value}{quote}"
    command, encoded_value = encoded_cli_call(shell, value, encoding)
    engine, detector = make_engine(method=method)
    body = {
        "input": [
            {
                "type": "custom_tool_call_output",
                "output": [{"type": "text", "text": f"password={json.dumps(value)}"}],
            },
            {"type": "custom_tool_call", "name": "exec", "input": command},
        ]
    }
    calls = []
    for _ in range(2):
        out, mapping = await scrub_responses_request(engine, body)
        assert value not in json.dumps(out)
        scrubbed = out["input"][1]["input"]
        assert "--mot-de-passe" in scrubbed
        assert mapping.get_entity_type(encoded_value) == "SECRET"
        prefix, suffix = command.split(encoded_value)
        assert scrubbed.startswith(prefix) and scrubbed.endswith(suffix)
        if method == "placeholder":
            assert scrubbed == command.replace(encoded_value, mapping.get_fake(encoded_value))
            assert engine.deanonymizer.deanonymize(scrubbed, mapping) == command
        else:
            assert mapping.get_all_fakes() == {}
        calls.append(detector.calls)
    assert calls[0] == calls[1]


@pytest.mark.asyncio
async def test_codex_multiple_encoded_cli_secrets_preserve_exact_source_after_restoration():
    first = r"demo \"first quote\" C:\\fixture\\one"
    second = 'demo "second quote" C:\\fixture\\two'
    shell = f"one --password \"{first}\" --user ordinary; two --mot-de-passe '{second}'"
    command, encoded_first = encoded_cli_call(shell, first, "js-double")
    _, encoded_second = encoded_cli_call(shell, second, "js-double")
    engine, detector = make_engine(method="placeholder")
    body = {"input": [{"type": "custom_tool_call", "name": "exec", "input": command}]}
    for _ in range(2):
        out, mapping = await scrub_responses_request(engine, body)
        scrubbed = out["input"][0]["input"]
        assert scrubbed == command.replace(encoded_first, mapping.get_fake(encoded_first)).replace(
            encoded_second, mapping.get_fake(encoded_second)
        )
        assert engine.deanonymizer.deanonymize(scrubbed, mapping) == command
    assert detector.calls == 1


@pytest.mark.asyncio
async def test_codex_encoded_cli_password_cannot_bypass_block_gate_on_cold_or_cached_requests():
    value = "demo-only-cli-password"
    command, _ = encoded_cli_call(f'connect --password "{value}"', value, "js-double")
    engine, detector = make_engine(method="placeholder", block=["SECRET"])
    body = {"input": [{"type": "custom_tool_call", "name": "exec", "input": command}]}
    for _ in range(2):
        with pytest.raises(PIIBlockedError, match="SECRET") as exc:
            await scrub_responses_request(engine, body)
        assert value not in str(exc.value)
    assert detector.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["redact", "mask"])
async def test_uri_overlap_is_irreversible_on_cold_and_cached_requests(method: str):
    engine, detector = make_engine(method=method)
    for _ in range(2):
        out, mapping = await engine.process_request([{"role": "tool", "content": URI}])
        assert "synthetic-password" not in out[0]["content"]
        assert mapping.get_all_fakes() == {}
        assert engine.deanonymizer.deanonymize(out[0]["content"], mapping) == out[0]["content"]
    assert detector.calls == 1


@pytest.mark.asyncio
async def test_overlap_cannot_bypass_block_gate_on_cold_or_cached_requests():
    engine, detector = make_engine(method="placeholder", block=["SECRET"])
    for _ in range(2):
        with pytest.raises(PIIBlockedError, match="SECRET") as exc:
            await engine.process_request([{"role": "tool", "content": URI}])
        assert "synthetic-password" not in str(exc.value)
    assert detector.calls == 1


@pytest.mark.asyncio
async def test_anonymization_policy_change_invalidates_cached_winner():
    engine, detector = make_engine(method="placeholder")
    _, before = await engine.process_request([{"role": "tool", "content": URI}])
    assert any("synthetic-password" in original for original in before.get_all_fakes().values())
    engine.anonymizer.config.entity_overrides["SECRET"].method = "redact"
    out, after = await engine.process_request([{"role": "tool", "content": URI}])
    assert "[SECRET]" in out[0]["content"]
    assert after.get_all_fakes() == {}
    assert detector.calls == 2


@pytest.mark.asyncio
async def test_global_irreversible_method_outranks_a_reversible_override():
    engine, _ = make_engine()
    engine.anonymizer.config.method = "redact"
    engine.anonymizer.config.entity_overrides = {
        "EMAIL_ADDRESS": engine.anonymizer.config.entity_overrides.pop("SECRET").model_copy(
            update={"method": "placeholder"}
        )
    }
    out, mapping = await engine.process_request([{"role": "tool", "content": URI}])
    assert "[SECRET]" in out[0]["content"]
    assert mapping.get_all_fakes() == {}


@pytest.mark.asyncio
async def test_block_policy_change_invalidates_cached_winner():
    engine, detector = make_engine(method="placeholder")
    await engine.process_request([{"role": "tool", "content": URI}])
    engine._blocked.add("SECRET")
    with pytest.raises(PIIBlockedError, match="SECRET"):
        await engine.process_request([{"role": "tool", "content": URI}])
    assert detector.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["chat", "anthropic", "responses"])
async def test_tool_output_surfaces_scan_secrets_and_preserve_assignment_syntax(surface: str):
    engine, _ = make_engine()
    log = 'INFO status=ok\npresented_key="fixture-only" smtp_secret=other-fixture'
    if surface == "chat":
        out, mapping = await engine.process_request([{"role": "tool", "content": log}])
    elif surface == "anthropic":
        out, mapping = await scrub_anthropic_request(
            engine,
            {"messages": [{"role": "user", "content": [{"type": "tool_result", "content": log}]}]},
        )
    else:
        out, mapping = await scrub_responses_request(
            engine, {"input": [{"type": "function_call_output", "output": log}]}
        )
    encoded = json.dumps(out)
    assert "fixture-only" not in encoded and "other-fixture" not in encoded
    assert "status=ok" in encoded and "smtp_secret=[SECRET]" in encoded
    assert 'presented_key=\\"[SECRET]\\"' in encoded
    assert mapping.get_all_fakes() == {}


@pytest.mark.parametrize("resolution", ["highest_score", "longest_span", "presidio_priority"])
@pytest.mark.parametrize("strategy", ["union", "intersection"])
def test_policy_priority_survives_transitive_overlaps(resolution: str, strategy: str):
    text = "0123456789abcdef"
    entities = [
        PIIEntity("PERSON", text[:9], 0, 9, 1.0, "presidio"),
        PIIEntity("CUSTOM_BLOCKED", text[6:10], 6, 10, 0.5, "onnx"),
        PIIEntity("EMAIL_ADDRESS", text[9:], 9, len(text), 1.0, "presidio"),
    ]
    result = merge_entities(
        entities,
        strategy=strategy,
        overlap_resolution=resolution,
        source_text=text,
        type_priorities={"CUSTOM_BLOCKED": 2, "PERSON": 1},
    )
    assert len(result) == 1
    assert result[0].entity_type == "CUSTOM_BLOCKED"
    assert result[0].text == text


def test_builtin_secret_type_is_producible_with_a_presidio_allowlist():
    engine = PIIEngine(
        PIIConfig(
            preset=None,
            detectors={"presidio": {"enabled": True, "entities": ["PERSON"]}},
            block_entities=["SECRET"],
        )
    )
    engine._validate_block_entities()
