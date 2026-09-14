"""Self-check that reads the wire, not the reply.

`docs/verify.md` used to lead with "turn de-anonymization off and read the
response". That shows what the proxy gave BACK, which is not the same thing as
what the provider RECEIVED, and the difference hides real defects: an ONNX span
that covered only part of a path left a fragment of a name on the wire while the
restored reply looked perfectly clean.

So this captures the outbound request body itself. A throwaway provider on
localhost records exactly what reaches it, the same agent-shaped payload is sent
twice (straight to it, then through a real PrivAiTe app), and every planted
value is looked for in what was recorded. Nothing leaves the machine and no
provider credential is involved.

The proxy runs in-process over an ASGI transport rather than as a subprocess: it
is the real application, with the real startup path (provider router, engine
initialisation, the fail-fast config checks), and there is no port to reserve
and no orphan to leave behind.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

# Deliberately unusable values. They must look like the real thing to the
# detectors and like an obvious fake to a human reading the output: this text
# ends up in terminals, screenshots and issue reports.
PLANTED: tuple[tuple[str, str, str], ...] = (
    ("Marie Dupont", "PERSON", "message text"),
    ("marie.dupont@example.invalid", "EMAIL_ADDRESS", "tool-call argument"),
    ("4111 1111 1111 1111", "CREDIT_CARD", "tool-call argument"),
    ("SERVICE_API_KEY=sk-demo-0000-not-a-real-key", "SECRET", "tool output"),
)

_USER_TEXT = (
    "Hi, I am Marie Dupont. Read the deployment notes and email the summary "
    "to the address in my profile."
)
_TOOL_ARGUMENTS = json.dumps(
    {"to": "marie.dupont@example.invalid", "card_on_file": "4111 1111 1111 1111"}
)
_TOOL_OUTPUT = "deploy.env line 3: SERVICE_API_KEY=sk-demo-0000-not-a-real-key"


def agent_payload(model: str) -> dict[str, Any]:
    """One request with PII in the three places an agent actually puts it.

    Text-only guardrails scrub the first and forward the other two untouched,
    which is the whole point of looking at the wire.
    """
    return {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "user", "content": _USER_TEXT},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_verify_1",
                        "type": "function",
                        "function": {"name": "send_email", "arguments": _TOOL_ARGUMENTS},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_verify_1", "content": _TOOL_OUTPUT},
        ],
    }


@dataclass
class Capture:
    """A throwaway OpenAI-compatible provider that keeps what it was sent."""

    bodies: list[str] = field(default_factory=list)
    _server: Any = None
    _thread: Any = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> Capture:
        bodies = self.bodies

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's name)
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                text = raw.decode("utf-8", "replace")
                bodies.append(text)
                # Echo the first user message back, so whatever the proxy put on
                # the wire is what comes back and the restore path is exercised
                # for real rather than assumed.
                try:
                    echoed = next(
                        m.get("content") or ""
                        for m in json.loads(text)["messages"]
                        if m.get("role") == "user"
                    )
                except Exception:
                    echoed = ""
                body = json.dumps(
                    {
                        "id": "chatcmpl-verify",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "verify",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": echoed},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                """Silence the stdlib access log: it would interleave with the report."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@dataclass
class Finding:
    value: str
    entity_type: str
    where: str
    on_the_wire: bool


@dataclass
class VerificationResult:
    preset: str
    direct: list[Finding]
    through_proxy: list[Finding]
    placeholders: list[str]
    restored: bool
    elapsed_ms: float

    @property
    def leaked(self) -> list[Finding]:
        return [f for f in self.through_proxy if f.on_the_wire]

    @property
    def ok(self) -> bool:
        return not self.leaked and self.restored


def _findings(body: str) -> list[Finding]:
    return [
        Finding(value=value, entity_type=etype, where=where, on_the_wire=value in body)
        for value, etype, where in PLANTED
    ]


def _placeholders(body: str) -> list[str]:
    import re

    return sorted(set(re.findall(r"<[A-Z][A-Z_]*_\d+>", body)))


def _verify_config(base_url: str, preset: str, model: str) -> Any:
    from privaite.config.schema import (
        AuthConfig,
        LiteLLMParams,
        LoggingConfig,
        PIIConfig,
        PrivAiTeConfig,
        ProviderConfig,
        ServerConfig,
    )

    return PrivAiTeConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        # The throwaway provider ignores credentials, and verification must
        # never touch the operator's real keys.
        auth=AuthConfig(enabled=False),
        providers=[
            ProviderConfig(
                model_name=model,
                litellm_params=LiteLLMParams(
                    model=f"openai/{model}",
                    api_base=base_url,
                    api_key="not-a-real-key-local-capture",
                ),
            )
        ],
        pii=PIIConfig(enabled=True, preset=preset),
        logging=LoggingConfig(format="text", level="error"),
    )


async def run_verification(preset: str = "onnx", model: str = "verify-model") -> VerificationResult:
    import httpx

    from privaite.app import create_app

    capture = Capture().start()
    started = time.perf_counter()
    try:
        payload = agent_payload(model)

        # 1. Straight to the provider, the baseline every text-only guardrail
        #    also produces for the tool-call fields.
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(f"{capture.base_url}/chat/completions", json=payload)
        direct = _findings(capture.bodies[-1])

        # 2. The same payload through the real application.
        app = create_app(_verify_config(capture.base_url, preset, model))
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://verify") as client:
                response = await client.post(
                    "/v1/chat/completions", json=payload, timeout=httpx.Timeout(300)
                )
        wire = capture.bodies[-1]
        reply = response.json()["choices"][0]["message"].get("content") or ""

        return VerificationResult(
            preset=preset,
            direct=direct,
            through_proxy=_findings(wire),
            placeholders=_placeholders(wire),
            # The provider echoed back what it received, so the real name
            # reappearing here means the round trip restored it.
            restored="Marie Dupont" in reply,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
    finally:
        capture.stop()


def format_report(result: VerificationResult) -> str:
    lines = [
        "",
        f"PrivAiTe verification, preset {result.preset}. Nothing left this machine.",
        "",
        "Planted values found in the request body the provider received:",
        "",
        f"  {'value':46} {'where':20} {'direct':>8} {'proxied':>9}",
    ]
    for direct, proxied in zip(result.direct, result.through_proxy, strict=True):
        shown = direct.value if len(direct.value) <= 44 else direct.value[:41] + "..."
        lines.append(
            f"  {shown:46} {direct.where:20} "
            f"{'LEAK' if direct.on_the_wire else 'clean':>8} "
            f"{'LEAK' if proxied.on_the_wire else 'clean':>9}"
        )
    lines += [
        "",
        f"Placeholders on the wire: {', '.join(result.placeholders) or 'none'}",
        f"Real values restored in the reply to the client: {'yes' if result.restored else 'no'}",
        f"Round trip: {result.elapsed_ms:.0f} ms",
        "",
    ]
    if result.leaked:
        lines.append("RESULT: FAILED, these reached the provider through the proxy:")
        lines += [f"  {f.value} ({f.entity_type}, {f.where})" for f in result.leaked]
        lines.append("")
        lines.append("Detection is never perfect. Please report this output as an issue:")
        lines.append("  https://github.com/crp4222/PrivAiTe/issues")
    elif not result.restored:
        lines.append(
            "RESULT: FAILED, nothing leaked but the reply came back without the real values."
        )
    else:
        lines.append("RESULT: PASSED, every planted value was replaced before the request left.")
    lines.append("")
    return "\n".join(lines)


def verify(preset: str = "onnx") -> VerificationResult:
    return asyncio.run(run_verification(preset=preset))
