"""Liveness and dependency readiness endpoints."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agent_hub.api.errors import BASE_ERROR_RESPONSES, error_payload, error_responses
from agent_hub.api.schemas import HealthResponse

router = APIRouter(tags=["system"], responses=BASE_ERROR_RESPONSES)


@router.get("/health/live", response_model=HealthResponse)
async def health_live() -> HealthResponse:
    return HealthResponse()


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    responses=error_responses(503),
)
async def health_ready(request: Request) -> HealthResponse | JSONResponse:
    database_probe = request.app.state.database_probe
    redis_probe = request.app.state.redis_probe
    if database_probe is None or redis_probe is None:
        return JSONResponse(
            status_code=503,
            content=error_payload("service_unavailable", "service unavailable"),
        )
    tasks = [
        asyncio.create_task(database_probe(), name="readiness-database"),
        asyncio.create_task(redis_probe(), name="readiness-redis"),
    ]
    try:
        async with asyncio.timeout(request.app.state.readiness_timeout_seconds):
            await asyncio.gather(*tasks)
    except Exception:  # noqa: BLE001 -- all probe failures map to one safe readiness result.
        return JSONResponse(
            status_code=503,
            content=error_payload("service_unavailable", "service unavailable"),
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return HealthResponse()
