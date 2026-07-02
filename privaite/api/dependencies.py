from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Request

if TYPE_CHECKING:
    from privaite.config.schema import PrivAiTeConfig
    from privaite.pii.engine import PIIEngine
    from privaite.providers.router import ProviderRouter


def get_config(request: Request) -> PrivAiTeConfig:
    return request.app.state.config


def get_pii_engine(request: Request) -> PIIEngine | None:
    return getattr(request.app.state, "pii_engine", None)


def get_provider_router(request: Request) -> ProviderRouter:
    return request.app.state.provider_router


def record_pii_stats(request: Request, mapping: Any) -> None:
    """Count this request's detections in /stats (per-type counts only, keyed by
    a salted hash of the session id; no values are stored). One helper so chat,
    completions and embeddings all report the same way."""
    if mapping is None or not mapping.has_detections:
        return
    record_pii_counts(request, mapping.entity_type_counts())


def record_pii_counts(request: Request, counts: dict[str, int]) -> None:
    # One tracker.record per REQUEST: the tracker also increments request_count,
    # so a batch endpoint must merge its per-item counts before calling this.
    tracker = getattr(request.app.state, "pii_tracker", None)
    if not tracker or not counts:
        return
    session_id = request.headers.get(
        "x-session-id",
        request.headers.get("authorization", "anonymous"),
    )
    tracker.record(session_id, counts)
