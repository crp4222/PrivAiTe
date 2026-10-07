"""Credentials in .env files and well-known key formats the first rules missed.

Measured on 0.7.1 with the shipped config: `API_TOKEN=...` and
`OPENAI_KEY=sk-...` reached the provider untouched (neither name was in the
vocabulary and the model did not flag them), and the password of
`redis://:password@host` only got a reversible URL placeholder.
"""

from __future__ import annotations

import pytest

from privaite.pii.recognizer_secret import StructuredSecretRecognizer


def secrets(text: str) -> list[str]:
    found = StructuredSecretRecognizer().analyze(text, ["SECRET"])
    return [text[start:end] for start, end in sorted({(r.start, r.end) for r in found})]


# Assembled at run time: the source never holds a string a scanner reads as a key.
def key(prefix: str, body: str) -> str:
    return prefix + body


OPAQUE = "demo0000Token1234notReal"


@pytest.mark.parametrize(
    "name",
    [
        "API_TOKEN",
        "GITHUB_TOKEN",
        "SLACK_BOT_TOKEN",
        "JWT_SECRET",
        "APP_SECRET",
        "SECRET_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "OPENAI_KEY",
        "ENCRYPTION_KEY",
        "TOKEN",
    ],
)
@pytest.mark.parametrize("shape", ["{n}={v}", "export {n}={v}", '{n}="{v}"', "{n} = '{v}'"])
def test_an_env_name_that_means_a_credential_reports_its_opaque_value(name, shape):
    text = "DEBUG=true\n" + shape.format(n=name, v=OPAQUE) + "\nPORT=8080"
    assert secrets(text) == [OPAQUE]


@pytest.mark.parametrize(
    "line",
    [
        # Not an opaque value: a word, a flag, a number, a path, a reference.
        "API_TOKEN=changeme",
        "SECRET_KEY=true",
        "CACHE_KEY=users",
        "MAX_TOKEN=4096",
        "SSH_KEY=/home/app/.ssh/id_ed25519",
        "API_TOKEN=${VAULT_API_TOKEN_2026}",
        "API_TOKEN=$(vault read -field=token secret/app2026)",
        "PUBLIC_KEY=ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDemo0000",
        # Not an env line: code and prose keep their own rules.
        "next_page_token=demo0000Token1234notReal",
        "    token = demo0000Token1234notReal",
        "set SORT_KEY=demo0000Token1234notReal in the console",
        "CACHE_KEYS=demo0000Token1234notReal",
        "MONKEY=demo0000Token1234notReal",
    ],
)
def test_env_names_leave_ordinary_values_and_other_syntax_alone(line):
    assert secrets("DEBUG=true\n" + line) == []


@pytest.mark.parametrize("name", ["DB_PASS", "SMTP_PASS", "MYSQL_PWD", "PASS"])
def test_an_env_password_name_reports_any_value(name):
    assert secrets(f"{name}=correct horse") == ["correct"]
    assert secrets(f'{name}="correct horse"') == ["correct horse"]
    assert secrets(f"{name}=hunter2") == ["hunter2"]


@pytest.mark.parametrize(
    "line",
    [
        "BYPASS=enabled",
        "COMPASS=north",
        "first_pass=done",
        "PASS=",
        "SSH_ASKPASS=/usr/lib/ssh/x11-ssh-askpass",
        # The shell's working directory, in every `env` output.
        "PWD=/home/app/project",
        "OLDPWD=/tmp",
        "PWD = Path(__file__).parent",
    ],
)
def test_names_that_only_contain_pass_are_not_passwords(line):
    assert secrets(line) == []


@pytest.mark.parametrize(
    "uri",
    [
        "redis://:demo-pass-4821@cache.internal:6379/0",
        "REDIS_URL=redis://:demo-pass-4821@cache.internal:6379/0",
        "amqp://:demo-pass-4821@mq.internal",
    ],
)
def test_a_uri_password_with_no_user_name_is_reported(uri):
    assert secrets(uri) == ["demo-pass-4821"]


@pytest.mark.parametrize(
    "value",
    [
        key("sk-", "demo-0000-not-a-real-key"),
        key("sk-", "proj-" + "A1b2C3d4" * 5),
        key("sk_" + "live_", "A1b2C3d4" * 3),
        key("gh" + "p_", "A1b2C3d4" * 5),
        key("github_" + "pat_", "A1b2C3d4_" * 6),
        key("gl" + "pat-", "A1b2C3d4" * 3),
        key("xox" + "b-", "1234567890-A1b2C3d4E5"),
        key("AK" + "IA", "A1B2C3D4E5F6G7H8"),
        key("AI" + "za", "A1b2C3d4" * 4 + "A1b"),
        key("hf" + "_", "A1b2C3d4" * 4 + "A1"),
    ],
)
def test_well_known_key_formats_are_reported_whatever_the_field(value):
    assert secrets(f"the key is {value} for now") == [value]
    assert secrets(f'{{"note": "{value}"}}') == [value]
    assert secrets(f"UNRELATED_NAME={value}") == [value]


@pytest.mark.parametrize(
    "text",
    [
        "sk-demo is the prefix of a key",
        "the task-force-2026-review-of-the-plan is long",
        "risk-assessment-2026-final-version-three",
        "ghp_ is how a token starts",
        "AKIA and AIza are prefixes",
        "hf_hub_download(repo_id, filename, cache_dir)",
        "mask-0000-0000-0000-0000-0000",
    ],
)
def test_prefixes_alone_and_lookalike_words_are_not_keys(text):
    assert secrets(text) == []


# What an agent really sends is rarely the bare file: a read tool numbers the
# lines, a diff marks them, a compose file lists them.
@pytest.mark.parametrize(
    "shape",
    [
        "1\t{n}={v}",
        "     12\t{n}={v}",
        "12\u2192{n}={v}",
        "00012| {n}={v}",
        "12:{n}={v}",
        "+{n}={v}",
        "-{n}={v}",
        "      - {n}={v}",
        "ENV {n}={v}",
        "ARG {n}={v}",
        "env {n}={v} ./run.sh",
        "3\texport {n}={v}",
    ],
)
@pytest.mark.parametrize("name", ["API_TOKEN", "JWT_SECRET", "OPENAI_KEY"])
def test_an_env_credential_behind_a_gutter_or_a_marker(shape, name):
    line = shape.format(n=name, v=OPAQUE)
    assert secrets("APP_ENV=production\n" + line + "\nPORT=8080") == [OPAQUE]


@pytest.mark.parametrize(
    "line",
    [
        # A number that is not a gutter, a marker that is not at the start.
        "see step 3 API_TOKEN=" + OPAQUE,
        "retry with X-API_TOKEN=" + OPAQUE,
        "2026 API_TOKEN=" + OPAQUE,
    ],
)
def test_an_env_name_in_the_middle_of_a_line_is_still_not_trusted(line):
    assert secrets(line) == []


def test_a_password_name_behind_a_gutter():
    assert secrets("7\tDB_PASS=hunter2") == ["hunter2"]
    assert secrets("      - SMTP_PASS=hunter2") == ["hunter2"]
