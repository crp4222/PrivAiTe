from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from privaite.utils.security import get_api_keys, verify_api_key

_PUBLIC_PATHS = {"/health", "/ready", "/docs", "/openapi.json", "/redoc"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        config = request.app.state.config

        if not config.auth.enabled:
            return await call_next(request)

        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        allowed_keys = get_api_keys()
        if not allowed_keys:
            # Fail closed: auth is on but nothing can authenticate, so reject
            # rather than silently forwarding every request to the provider.
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": (
                            "Authentication is enabled but no API keys are configured. "
                            "Set PRIVAITE_API_KEYS or disable auth (auth.enabled=false)."
                        ),
                        "type": "auth_error",
                    }
                },
            )

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Missing or invalid authorization header",
                        "type": "auth_error",
                    }
                },
            )

        token = auth_header[7:]
        if not verify_api_key(token, allowed_keys):
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Invalid API key", "type": "auth_error"}},
            )

        return await call_next(request)
