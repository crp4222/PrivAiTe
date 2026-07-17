"""Opt-in gateway mode: agent CLIs point their base URL at PrivAiTe, which
scrubs their Anthropic Messages / OpenAI Responses traffic and restores the
responses, relaying the client's own provider token upstream."""

from __future__ import annotations

from privaite.gateway.routes import build_gateway_router

__all__ = ["build_gateway_router"]
