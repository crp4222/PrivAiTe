"""Smoke test of an installed PrivAiTe, run inside the image before it is pushed.

Starts the server against a local capture provider and sends two chat
requests carrying an email, one plain and one with `tools`. Each must come
back 200 with the email restored, reach the provider masked, and the tools
must be forwarded. The unit tests replace litellm or the router; this is the
check that sees the dependencies users actually pull (#45).

    docker run --rm -i --entrypoint python IMAGE - < scripts/smoke_image.py
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from privaite.verify import Capture

EMAIL = "marie.dupont@example.com"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Weather of a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]
READY_TIMEOUT = 300


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(api_base: str, port: int) -> str:
    return json.dumps(
        {
            "server": {"host": "127.0.0.1", "port": port},
            "auth": {"enabled": False},
            "providers": [
                {
                    "model_name": "smoke",
                    "litellm_params": {
                        "model": "openai/smoke",
                        "api_base": api_base,
                        "api_key": "sk-smoke-not-real",
                    },
                }
            ],
            "pii": {"enabled": True, "preset": "light"},
        }
    )


def _wait_ready(base: str, server: subprocess.Popen) -> None:
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise SystemExit(f"privaite exited with {server.returncode} before /ready")
        try:
            if httpx.get(f"{base}/ready", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise SystemExit(f"privaite not ready after {READY_TIMEOUT}s")


def check(base: str, capture: Capture, with_tools: bool) -> list[str]:
    """Send one request; return what went wrong, empty when it passed."""
    body: dict = {
        "model": "smoke",
        "messages": [{"role": "user", "content": f"Write to {EMAIL} about the weather"}],
    }
    if with_tools:
        body["tools"] = TOOLS
    sent_before = len(capture.bodies)
    resp = httpx.post(f"{base}/v1/chat/completions", json=body, timeout=120)
    label = "tools" if with_tools else "plain"
    if resp.status_code != 200:
        return [f"{label}: status {resp.status_code}: {resp.text[:300]}"]
    errors = []
    wire = capture.bodies[sent_before:]
    if len(wire) != 1:
        errors.append(f"{label}: the provider got {len(wire)} requests, expected 1")
    elif EMAIL in wire[0]:
        errors.append(f"{label}: the email reached the provider unmasked")
    elif with_tools and json.loads(wire[0]).get("tools") != TOOLS:
        errors.append(f"{label}: the tools were not forwarded as sent")
    if EMAIL not in resp.json()["choices"][0]["message"]["content"]:
        errors.append(f"{label}: the email was not restored in the response")
    return errors


def main() -> int:
    capture = Capture().start()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "privaite.yaml"
        config.write_text(_config(capture.base_url, port))  # JSON is valid YAML
        server = subprocess.Popen([sys.executable, "-m", "privaite", "--config", str(config)])
        try:
            _wait_ready(base, server)
            errors = check(base, capture, with_tools=False) + check(base, capture, with_tools=True)
        finally:
            server.terminate()
            server.wait(timeout=30)
            capture.stop()
    for error in errors:
        print(f"SMOKE FAIL {error}", file=sys.stderr)
    if not errors:
        print("smoke ok: plain and tools requests masked, forwarded and restored")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
