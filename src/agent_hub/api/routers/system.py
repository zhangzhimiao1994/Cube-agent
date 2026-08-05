"""Liveness and dependency readiness endpoints."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agent_hub.api.errors import ERROR_RESPONSES, error_payload

router = APIRouter(tags=["system"], responses=ERROR_RESPONSES)


@router.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", response_model=None)
async def health_ready(request: Request) -> object:
    database_probe = request.app.state.database_probe
    redis_probe = request.app.state.redis_probe
    if database_probe is None or redis_probe is None:
        return JSONResponse(
            status_code=503,
            content=error_payload("service_unavailable", "service unavailable"),
        )
    try:
        async with asyncio.timeout(request.app.state.readiness_timeout_seconds):
            await asyncio.gather(database_probe(), redis_probe())
    except Exception:  # noqa: BLE001 -- all probe failures map to one safe readiness result.
        return JSONResponse(
            status_code=503,
            content=error_payload("service_unavailable", "service unavailable"),
        )
    return {"status": "ok"}
