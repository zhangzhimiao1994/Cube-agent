"""Liveness and dependency readiness endpoints."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from agent_hub.api.errors import BASE_ERROR_RESPONSES, error_payload, error_responses
from agent_hub.api.schemas import HealthResponse
from agent_hub.observability.metrics import MetricsRegistry

router = APIRouter(tags=["system"], responses=BASE_ERROR_RESPONSES)
ReadinessCheck = Callable[[], Coroutine[Any, Any, None]]


@router.get("/health/live", response_model=HealthResponse)
async def health_live() -> HealthResponse:
    return HealthResponse()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Compatibility liveness endpoint for installers and external monitors."""

    return HealthResponse()


@router.get(
    "/health/ready",
    response_model=HealthResponse,
    responses=error_responses(503),
)
async def health_ready(request: Request) -> HealthResponse | JSONResponse:
    checks = _readiness_checks(request)
    if checks is None:
        return JSONResponse(
            status_code=503,
            content={
                **error_payload("service_unavailable", "service unavailable"),
                "checks": {"database": "missing", "redis": "missing"},
            },
        )
    tasks: dict[str, asyncio.Task[None]] = {
        name: asyncio.create_task(check(), name=f"readiness-{name}")
        for name, check in checks.items()
    }
    statuses = dict.fromkeys(checks, "pending")
    try:
        async with asyncio.timeout(request.app.state.readiness_timeout_seconds):
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            statuses = {
                name: ("failed" if isinstance(result, Exception) else "ok")
                for name, result in zip(tasks, results, strict=True)
            }
    except Exception:  # noqa: BLE001 -- all readiness failures map to one safe result.
        statuses = {}
        for name, task in tasks.items():
            if task.cancelled():
                statuses[name] = "timeout"
            elif task.done():
                try:
                    statuses[name] = "failed" if task.exception() else "ok"
                except asyncio.CancelledError:
                    statuses[name] = "timeout"
            else:
                statuses[name] = "timeout"
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
    if any(status != "ok" for status in statuses.values()):
        return JSONResponse(
            status_code=503,
            content={
                **error_payload("service_unavailable", "service unavailable"),
                "checks": statuses,
            },
        )
    return HealthResponse()


@router.get(
    "/metrics",
    responses={
        200: {
            "description": "Prometheus metrics",
            "content": {"text/plain": {"schema": {"type": "string"}}},
        },
        **error_responses(),
    },
)
async def metrics(request: Request) -> PlainTextResponse:
    registry = getattr(request.app.state, "metrics_registry", None)
    if not isinstance(registry, MetricsRegistry):
        return PlainTextResponse("", media_type="text/plain; version=0.0.4")
    return PlainTextResponse(registry.render_prometheus(), media_type="text/plain; version=0.0.4")


def _readiness_checks(request: Request) -> dict[str, ReadinessCheck] | None:
    database_probe = request.app.state.database_probe
    redis_probe = request.app.state.redis_probe
    if database_probe is None or redis_probe is None:
        return None
    checks: dict[str, ReadinessCheck] = {
        "database": database_probe,
        "redis": redis_probe,
    }
    extra = getattr(request.app.state, "extra_readiness_checks", {})
    if isinstance(extra, dict):
        checks.update(extra)
    return checks
