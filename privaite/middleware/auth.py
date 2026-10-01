from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from privaite.gateway.protocols import GATEWAY_MODELS_PATH, GATEWAY_ROUTE_PATHS
from privaite.utils.security import get_api_keys, verify_api_key

_PUBLIC_PATHS = {"/health", "/ready", "/docs", "/openapi.json", "/redoc"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        config = request.app.state.config

        if not config.auth.enabled:
            return await call_next(request)

        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        # Gateway routes carry the CLIENT'S provider token (subscription or API
        # key), which the gateway relays verbatim so the upstream authenticates
        # the user; there is no PrivAiTe key in that Authorization header to
        # verify. The paths are resolved from the protocol specs, and only
        # bypassed when gateway mode is actually enabled (otherwise they do not
        # exist and stay under the normal auth check).
        if config.gateway.enabled and request.url.path in GATEWAY_ROUTE_PATHS:
            return await call_next(request)

        # GET /v1/models is shared: the core proxy lists its own aliases there,
        # and Codex fetches its upstream catalog from the same path with its own
        # provider token. In gateway mode the route decides, from this flag:
        # a caller holding a valid PrivAiTe key gets PrivAiTe's list, and
        # anyone else is relayed to the Responses upstream, which authenticates
        # them. A valid PrivAiTe key is therefore never sent upstream.
        if config.gateway.enabled and request.url.path == GATEWAY_MODELS_PATH:
            request.state.privaite_client = _holds_privaite_key(request)
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


def _holds_privaite_key(request: Request) -> bool:
    allowed_keys = get_api_keys()
    auth_header = request.headers.get("authorization", "")
    if not allowed_keys or not auth_header.startswith("Bearer "):
        return False
    return verify_api_key(auth_header[7:], allowed_keys)
