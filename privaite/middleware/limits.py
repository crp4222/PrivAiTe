from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose declared body size exceeds server.max_request_bytes."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        max_bytes = request.app.state.config.server.max_request_bytes
        content_length = request.headers.get("content-length")

        if max_bytes and content_length is not None:
            try:
                size = int(content_length)
            except ValueError:
                size = 0
            if size > max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "message": f"Request body exceeds the {max_bytes}-byte limit.",
                            "type": "invalid_request_error",
                        }
                    },
                )

        return await call_next(request)
