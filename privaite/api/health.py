from __future__ import annotations

from fastapi import APIRouter, Request, Response

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    """Liveness only: the process answers. It reads no state on purpose, so it
    must never be used as the readiness signal (use /ready for that)."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request, response: Response) -> dict:
    """Readiness: can this proxy actually serve a request right now?

    Returns 503 when it cannot, so a container healthcheck or a Kubernetes probe
    reacts to the real state instead of a constant 200. `pii` distinguishes PII
    deliberately turned off (`pii.enabled: false`) from an engine that should be
    there and is not: with PII enabled and no engine attached, nothing would be
    scrubbed, so the proxy is NOT ready.
    """
    config = getattr(request.app.state, "config", None)

    provider_router = getattr(request.app.state, "provider_router", None)
    providers_configured = provider_router is not None and len(provider_router.models) > 0
    # Gateway mode serves the agent CLI routes without any provider entry, so a
    # gateway-only deployment with 0 providers is legitimately ready.
    gateway_enabled = config is not None and config.gateway.enabled

    pii_enabled = config is not None and config.pii.enabled
    pii_engine = getattr(request.app.state, "pii_engine", None)
    if not pii_enabled:
        pii_status = "disabled"
        pii_ready = True
    elif pii_engine is None:
        pii_status = "missing"
        pii_ready = False
    elif pii_engine.is_ready:
        pii_status = "ready"
        pii_ready = True
    else:
        pii_status = "initializing"
        pii_ready = False

    checks: dict[str, bool] = {
        "providers_configured": providers_configured,
        "pii_engine_ready": pii_ready,
    }
    all_ready = (providers_configured or gateway_enabled) and pii_ready
    if not all_ready:
        response.status_code = 503
    return {
        "ready": all_ready,
        "checks": checks,
        "pii": pii_status,
        "gateway_enabled": gateway_enabled,
    }


@router.get("/stats")
async def stats(request: Request) -> dict:
    tracker = getattr(request.app.state, "pii_tracker", None)
    if tracker is None:
        return {"enabled": False}

    sessions = {}
    with tracker._lock:
        for sid, s in tracker._sessions.items():
            label = sid[:16] + "..." if len(sid) > 16 else sid
            sessions[label] = {
                "requests": s.request_count,
                "total_pii": s.total_pii,
                "by_type": dict(s.pii_count),
            }

    return {"enabled": True, "sessions": sessions}
