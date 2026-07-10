#!/usr/bin/env python3
"""See the tool-call PII leak, then see PrivAiTe stop it. Fully local, ~60s.

What this does, on 127.0.0.1 only (nothing leaves your machine):

1. starts a fake OpenAI-compatible "provider" that records every request body
   it receives, exactly like OpenAI/Anthropic would see it;
2. sends an agent-shaped chat request (user text + a tool call whose JSON
   arguments carry an email and a credit card) STRAIGHT to that provider:
   the PII arrives in the clear. Text-only guardrails scrub message text but
   forward tool-call JSON untouched, so this panel is also what they leak;
3. starts a real PrivAiTe proxy in front of the same provider and sends the
   SAME request through it: the provider now receives placeholders, and the
   client still gets the real values back, restored in the reply.

Prerequisites:
    pip install privaite
    python -m spacy download en_core_web_lg

Run:
    python examples/demo_tool_call_leak.py             # light preset, no model download
    python examples/demo_tool_call_leak.py --preset onnx   # default prod preset (downloads model)
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

EMAIL = "marie.dupont@acme-corp.com"
CARD = "4111 1111 1111 1111"
PROXY_KEY = "demo-local-key"  # what the CLIENT sends to PrivAiTe (never the provider key)

RULE = "─" * 74


def agent_request() -> dict:
    """An agent-shaped chat payload: the PII sits in the user text AND inside
    the tool call's JSON arguments, which is where text-only guardrails stop
    looking."""
    return {
        "model": "demo-model",
        "messages": [
            {
                "role": "user",
                "content": f"Email {EMAIL} to confirm tomorrow's appointment. "
                f"Her card on file is {CARD}.",
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "send_email",
                            "arguments": json.dumps(
                                {
                                    "to": EMAIL,
                                    "body": "Confirming tomorrow's appointment. "
                                    f"Card on file: {CARD}.",
                                }
                            ),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "queued"},
            {"role": "user", "content": "Did it go out?"},
        ],
    }


class _RecordingProvider(BaseHTTPRequestHandler):
    """Fake OpenAI-compatible endpoint: records the request body it receives and
    answers by quoting the tool-call arguments back, so the restore step is
    visible in the client's reply."""

    received: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append(body)
        args = _tool_arguments(body)
        reply = (
            f'Yes, the email to {args.get("to", "?")} went out. Note kept: "{args.get("body", "")}"'
        )
        response = {
            "id": "chatcmpl-demo",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "demo-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        payload = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_: object) -> None:  # keep the demo output clean
        pass


def _tool_arguments(body: dict) -> dict:
    for message in body.get("messages", []):
        for call in message.get("tool_calls") or []:
            try:
                return json.loads(call["function"]["arguments"])
            except (KeyError, TypeError, ValueError):
                continue
    return {}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _print_provider_view(title: str, body: dict, note: str = "") -> None:
    user_text = next(
        m["content"] for m in body["messages"] if m["role"] == "user" and m.get("content")
    )
    print(f"\n{RULE}\n {title}\n{RULE}")
    print(f"  user message ......  {user_text}")
    print(f"  tool_call arguments  {_tool_arguments(body)}")
    if note:
        print(f"\n  {note}")


def _wait_ready(url: str, proxy: subprocess.Popen, timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proxy.poll() is not None:
            tail = (proxy.stdout.read() if proxy.stdout else "")[-2000:]
            sys.exit(
                f"PrivAiTe exited during startup:\n{tail}\n"
                "Hint: pip install privaite && python -m spacy download en_core_web_lg"
            )
        try:
            if httpx.get(url, timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.5)
    sys.exit("PrivAiTe did not become ready in time")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preset",
        default="light",
        choices=["light", "onnx"],
        help="light: no model download, classic PII. onnx: the production default "
        "(downloads the detection model on first run).",
    )
    args = parser.parse_args()

    provider_port, proxy_port = _free_port(), _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", provider_port), _RecordingProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(RULE)
    print(" PrivAiTe tool-call leak demo. Everything below runs on 127.0.0.1;")
    print(" the 'provider' is a local fake that records what it receives.")
    print(RULE)

    # 1. Straight to the provider: the baseline every text-only guardrail shares
    #    for tool-call JSON (they scrub message text, then forward this untouched).
    httpx.post(
        f"http://127.0.0.1:{provider_port}/v1/chat/completions",
        json=agent_request(),
        timeout=10.0,
    )
    _print_provider_view(
        "1. WITHOUT PrivAiTe: what the provider actually received",
        _RecordingProvider.received[-1],
        note="The email and card arrive in the clear, inside the tool-call JSON.",
    )

    # 2. The same request, through a real PrivAiTe proxy.
    config = {
        "server": {"host": "127.0.0.1", "port": proxy_port},
        "auth": {"enabled": True},
        "providers": [
            {
                "model_name": "demo-model",
                "litellm_params": {
                    "model": "openai/demo-model",
                    "api_base": f"http://127.0.0.1:{provider_port}/v1",
                    "api_key": "sk-demo-not-real",
                },
            }
        ],
        "pii": {
            "enabled": True,
            "preset": args.preset,
            "detectors": {"presidio": {"languages": ["en"]}},
        },
        "logging": {"format": "text", "level": "warning"},
    }
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "privaite.yaml"
        config_path.write_text(json.dumps(config), encoding="utf-8")  # YAML is a JSON superset
        proxy = subprocess.Popen(
            [sys.executable, "-m", "privaite", "--config", str(config_path)],
            env={**os.environ, "PRIVAITE_API_KEYS": PROXY_KEY},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            print(f"\n  starting PrivAiTe (preset={args.preset}) ...")
            _wait_ready(f"http://127.0.0.1:{proxy_port}/health", proxy)
            client_reply = httpx.post(
                f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                json=agent_request(),
                headers={"Authorization": f"Bearer {PROXY_KEY}"},
                timeout=120.0,
            ).json()
        finally:
            proxy.terminate()
            proxy.wait(timeout=10)
            server.shutdown()

    _print_provider_view(
        "2. WITH PrivAiTe: what the provider received this time",
        _RecordingProvider.received[-1],
        note="Same request, sent through the proxy: placeholders, JSON keys intact.",
    )

    print(f"\n{RULE}\n 3. WHAT THE CLIENT GETS BACK (restored by PrivAiTe)\n{RULE}")
    print(f"  assistant: {client_reply['choices'][0]['message']['content']}")

    leaked = EMAIL in json.dumps(_RecordingProvider.received[-1])
    print(f"\n{RULE}")
    print(f" Provider saw the real email/card through PrivAiTe: {'YES (!)' if leaked else 'no'}")
    print(
        f" Client got the real values back in the reply:      "
        f"{'yes' if EMAIL in client_reply['choices'][0]['message']['content'] else 'NO (!)'}"
    )
    print(RULE)


if __name__ == "__main__":
    main()
