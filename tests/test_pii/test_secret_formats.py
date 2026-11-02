"""Credential formats recognized without a field name: private key blocks, JSON
Web Tokens and HTTP Basic credentials. Synthetic values only."""

from __future__ import annotations

import base64
import json

import pytest

from privaite.config.schema import PIIConfig
from privaite.pii.engine import PIIEngine
from privaite.pii.recognizer_secret import StructuredSecretRecognizer

BODY_LINES = [
    base64.b64encode(bytes((7 * i + 13 * j) % 256 for j in range(48))).decode() for i in range(6)
]


def pem(label: str, headers: str = "", newline: str = "\n") -> str:
    lines = [f"-----BEGIN {label}-----", *([headers] if headers else []), *BODY_LINES]
    return newline.join([*lines, f"-----END {label}-----"])


def spans(text: str) -> list[str]:
    return [text[s.start : s.end] for s in StructuredSecretRecognizer().analyze(text, ["SECRET"])]


@pytest.mark.parametrize(
    "label",
    [
        "RSA PRIVATE KEY",
        "PRIVATE KEY",
        "EC PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
        "PGP PRIVATE KEY BLOCK",
    ],
)
def test_a_private_key_block_is_one_secret_armor_included(label: str):
    block = pem(label)
    text = f"$ cat ~/.ssh/key\n{block}\n$ "
    assert block in spans(text)


def test_an_encrypted_key_keeps_its_headers_inside_the_block():
    block = pem("RSA PRIVATE KEY", headers="Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0123ABCD")
    assert block in spans(f"key = '''{block}'''")


def test_a_private_key_in_a_json_string_is_covered_with_its_escaped_line_breaks():
    block = pem("PRIVATE KEY", newline="\\n")
    text = json.dumps({"type": "service_account"})[:-1] + f', "private_key": "{block}\\n"}}'
    assert any(block in found for found in spans(text))


def test_a_block_cut_short_is_covered_up_to_its_last_base64_line():
    text = "\n".join(["-----BEGIN RSA PRIVATE KEY-----", *BODY_LINES[:3]]) + "\n[truncated]"
    found = spans(text)
    assert any(all(line in value for line in BODY_LINES[:3]) for value in found)
    assert not any("[truncated]" in value for value in found)


@pytest.mark.parametrize(
    "label", ["PUBLIC KEY", "CERTIFICATE", "RSA PUBLIC KEY", "PGP PUBLIC KEY BLOCK"]
)
def test_public_keys_and_certificates_are_left_alone(label: str):
    assert spans(pem(label)) == []


HEADER = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
PAYLOAD = base64.urlsafe_b64encode(b'{"sub":"1234567890","name":"Synthetic"}').decode().rstrip("=")
SIGNATURE = "c3ludGhldGljLXNpZ25hdHVyZS1vbmx5"


@pytest.mark.parametrize(
    ("template", "token"),
    [
        ("{}", f"{HEADER}.{PAYLOAD}.{SIGNATURE}"),
        ("const session = '{}';", f"{HEADER}.{PAYLOAD}.{SIGNATURE}"),
        ("https://app.example.invalid/cb?id_token={}&state=1", f"{HEADER}.{PAYLOAD}.{SIGNATURE}"),
        ("unsigned: {}", f"{HEADER}.{PAYLOAD}."),
    ],
)
def test_a_json_web_token_is_a_secret_wherever_it_sits(template: str, token: str):
    assert token in spans(template.format(token))


@pytest.mark.parametrize(
    "text",
    [
        f"the header {HEADER} alone",
        "eyJ is how a JWT starts",
        f"x{HEADER}.{PAYLOAD}.{SIGNATURE}",
        f"{HEADER}.short.{SIGNATURE}",
    ],
)
def test_jwt_lookalikes_are_left_alone(text: str):
    assert spans(text) == []


BASIC = base64.b64encode(b"synthetic-user:synthetic-password").decode()


@pytest.mark.parametrize(
    "text",
    [
        f"Authorization: Basic {BASIC}",
        f'curl -H "Authorization: Basic {BASIC}" https://api.example.invalid',
        f'headers = {{"Authorization": "Basic {BASIC}"}}',
        f"headers['authorization'] = 'Basic {BASIC}'",
    ],
)
def test_basic_credentials_after_the_authorization_header_are_a_secret(text: str):
    assert BASIC in spans(text)


@pytest.mark.parametrize(
    "text",
    [
        "Basic authentication is supported",
        "Authorization: Basic ${CREDENTIALS}",
        "Authorization: Basic <base64 of user:password>",
        "Authorization: Basic abc",
    ],
)
def test_basic_scheme_without_credentials_is_left_alone(text: str):
    assert spans(text) == []


def test_bearer_tokens_in_a_header_map_are_a_secret_too():
    assert "synthetic.token-123" in spans('{"Authorization": "Bearer synthetic.token-123"}')


@pytest.mark.asyncio
async def test_a_private_key_read_by_an_agent_never_reaches_the_provider():
    engine = PIIEngine(
        PIIConfig(
            preset=None,
            detectors={"presidio": {"enabled": True, "languages": ["en"], "nlp_engine": "blank"}},
        )
    )
    await engine.initialize()
    block = pem("OPENSSH PRIVATE KEY")
    escaped = pem("PRIVATE KEY", newline="\\n")
    out, _ = await engine.process_request(
        [
            {"role": "tool", "content": f"1\t{block.replace(chr(10), chr(10) + '2' + chr(9))}"},
            {"role": "tool", "content": json.dumps({"private_key": escaped.replace("\\n", "\n")})},
            {"role": "tool", "content": block},
        ]
    )
    encoded = json.dumps(out)
    assert not any(line in encoded for line in BODY_LINES)
