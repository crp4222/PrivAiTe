from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _replay_body(body: bytes, receive: Receive) -> Receive:
    """Yield the already-buffered body once, then delegate to the real receive."""
    sent = False

    async def _receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return _receive


def _replay_message(message: Message, receive: Receive) -> Receive:
    """Yield one buffered control message once, then delegate to the real receive."""
    sent = False

    async def _receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return message
        return await receive()

    return _receive


class RequestSizeLimitMiddleware:
    """Reject requests whose body exceeds max_bytes.

    The Content-Length header is checked first, then the body bytes are counted
    as they stream in, so a chunked request that omits Content-Length cannot slip
    past the limit. The buffered body is replayed to the downstream app unchanged.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.max_bytes:
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > self.max_bytes:
                    await self._too_large(send)
                    return
                break

        body = bytearray()
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] != "http.request":
                # A control message such as http.disconnect: hand back control
                # with the message replayed first so the app still sees it.
                await self.app(scope, _replay_message(message, receive), send)
                return
            chunk = message.get("body", b"")
            # Check the projected size before extending so a single oversized
            # frame is rejected without first being copied into the buffer.
            if len(body) + len(chunk) > self.max_bytes:
                await self._too_large(send)
                return
            body.extend(chunk)
            more_body = message.get("more_body", False)

        await self.app(scope, _replay_body(bytes(body), receive), send)

    async def _too_large(self, send: Send) -> None:
        payload = json.dumps(
            {
                "error": {
                    "message": f"Request body exceeds the {self.max_bytes}-byte limit.",
                    "type": "invalid_request_error",
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
