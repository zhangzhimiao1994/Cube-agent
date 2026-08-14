from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from agent_hub.openclaw.executor import OpenClawCommandResult


class OpenClawRemoteAdapterError(RuntimeError):
    """Remote OpenClaw adapter failed or returned an invalid response."""


@dataclass(frozen=True, slots=True)
class OpenClawRemoteAdapter:
    platform: str
    target_type: str
    target: str
    base_url: str


async def run_remote_openclaw_operation(
    adapter: OpenClawRemoteAdapter,
    *,
    operation_id: str,
    operation: dict[str, object],
    bearer_token: str,
    timeout_seconds: float = 30,
) -> OpenClawCommandResult:
    url = f"{adapter.base_url.rstrip('/')}/v1/openclaw/execute"
    body = {
        "operation_id": operation_id,
        "platform": operation.get("platform"),
        "kind": operation.get("kind"),
        "target": operation.get("target"),
        "argv": operation.get("argv", []),
        "risk_level": operation.get("risk_level"),
        "reason": operation.get("reason"),
        "session_id": operation.get("session_id"),
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {bearer_token}"},
                json=body,
            )
    except httpx.HTTPError as error:
        raise OpenClawRemoteAdapterError("remote OpenClaw adapter request failed") from error
    if response.status_code < 200 or response.status_code >= 300:
        raise OpenClawRemoteAdapterError(f"remote OpenClaw adapter returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as error:
        raise OpenClawRemoteAdapterError("remote OpenClaw adapter returned invalid JSON") from error
    return _result_from_payload(payload)


def _result_from_payload(payload: Any) -> OpenClawCommandResult:
    if not isinstance(payload, dict):
        raise OpenClawRemoteAdapterError("remote OpenClaw adapter response must be an object")
    exit_code = payload.get("exit_code")
    stdout = payload.get("stdout")
    stderr = payload.get("stderr")
    truncated = payload.get("truncated")
    if type(exit_code) is not int or type(stdout) is not str or type(stderr) is not str or type(truncated) is not bool:
        raise OpenClawRemoteAdapterError("remote OpenClaw adapter response shape is invalid")
    return OpenClawCommandResult(
        exit_code=exit_code,
        stdout=_bounded_text(stdout),
        stderr=_bounded_text(stderr),
        truncated=truncated or len(stdout) > 16_384 or len(stderr) > 16_384,
    )


def _bounded_text(value: str, limit: int = 16_384) -> str:
    return value if len(value) <= limit else value[:limit]


__all__ = [
    "OpenClawRemoteAdapter",
    "OpenClawRemoteAdapterError",
    "run_remote_openclaw_operation",
]
