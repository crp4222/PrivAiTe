from __future__ import annotations

from fastapi.responses import JSONResponse


def openai_error(
    message: str,
    error_type: str = "server_error",
    status_code: int = 500,
    code: str | None = None,
) -> JSONResponse:
    body: dict = {
        "error": {
            "message": message,
            "type": error_type,
        }
    }
    if code:
        body["error"]["code"] = code
    return JSONResponse(status_code=status_code, content=body)


def provider_error_response(exc: Exception) -> JSONResponse:
    exc_type = type(exc).__name__

    if "AuthenticationError" in exc_type:
        return openai_error(
            "Provider authentication failed. Check your API key.",
            "auth_error", 401, "provider_auth_error",
        )

    if "RateLimitError" in exc_type:
        return openai_error(
            "Provider rate limit exceeded. Try again later.",
            "rate_limit_error", 429, "rate_limit",
        )

    if "Timeout" in exc_type:
        return openai_error(
            "Provider request timed out.",
            "timeout_error", 504, "timeout",
        )

    if "NotFoundError" in exc_type:
        return openai_error(
            "Model not found on provider.",
            "not_found_error", 404, "model_not_found",
        )

    if "ServiceUnavailableError" in exc_type:
        return openai_error(
            "Provider is temporarily unavailable.",
            "service_unavailable", 503, "provider_unavailable",
        )

    return openai_error(
        "An error occurred with the provider.",
        "server_error", 502, "provider_error",
    )
