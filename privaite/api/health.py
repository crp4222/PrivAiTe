from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> dict:
    checks: dict[str, bool] = {}

    provider_router = getattr(request.app.state, "provider_router", None)
    checks["providers_configured"] = (
        provider_router is not None and len(provider_router.models) > 0
    )

    pii_engine = getattr(request.app.state, "pii_engine", None)
    if pii_engine is not None:
        checks["pii_engine_ready"] = pii_engine.is_ready
    else:
        checks["pii_engine_ready"] = True

    all_ready = all(checks.values())
    return {"ready": all_ready, "checks": checks}


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
