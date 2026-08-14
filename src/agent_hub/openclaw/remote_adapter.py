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


@dataclass(frozen=True, slots=True)
class OpenClawRemoteAdapterProbe:
    status: str
    platform: str
    capabilities: tuple[str, ...]


async def probe_remote_openclaw_adapter(
    adapter: OpenClawRemoteAdapter,
    *,
    bearer_token: str,
    required_kind: str | None = None,
    timeout_seconds: float = 10,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OpenClawRemoteAdapterProbe:
    url = f"{adapter.base_url.rstrip('/')}/v1/openclaw/health"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, transport=transport) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {bearer_token}"})
    except httpx.HTTPError as error:
        raise OpenClawRemoteAdapterError("remote OpenClaw adapter health request failed") from error
    if response.status_code < 200 or response.status_code >= 300:
        raise OpenClawRemoteAdapterError(f"remote OpenClaw adapter health returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as error:
        raise OpenClawRemoteAdapterError("remote OpenClaw adapter health returned invalid JSON") from error
    probe = _probe_from_payload(payload)
    if probe.status != "available":
        raise OpenClawRemoteAdapterError("remote OpenClaw adapter is not available")
    if probe.platform != adapter.platform:
        raise OpenClawRemoteAdapterError("remote OpenClaw adapter platform mismatch")
    if required_kind is not None and required_kind not in probe.capabilities:
        raise OpenClawRemoteAdapterError(f"remote OpenClaw adapter does not support {required_kind}")
    return probe


async def run_remote_openclaw_operation(
    adapter: OpenClawRemoteAdapter,
    *,
    operation_id: str,
    operation: dict[str, object],
    bearer_token: str,
    timeout_seconds: float = 30,
) -> OpenClawCommandResult:
    kind = operation.get("kind")
    await probe_remote_openclaw_adapter(
        adapter,
        bearer_token=bearer_token,
        required_kind=kind if isinstance(kind, str) else None,
    )
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


def _probe_from_payload(payload: Any) -> OpenClawRemoteAdapterProbe:
    if not isinstance(payload, dict):
        raise OpenClawRemoteAdapterError("remote OpenClaw adapter health response must be an object")
    status = payload.get("status")
    platform = payload.get("platform")
    capabilities = payload.get("capabilities")
    if status == "ok":
        status = "available"
    if status != "available" or not isinstance(platform, str) or not isinstance(capabilities, list):
        raise OpenClawRemoteAdapterError("remote OpenClaw adapter health response shape is invalid")
    parsed_capabilities: list[str] = []
    for capability in capabilities:
        if not isinstance(capability, str) or capability not in {"server_command", "desktop_action", "screen_read", "file_read"}:
            raise OpenClawRemoteAdapterError("remote OpenClaw adapter capability is invalid")
        if capability not in parsed_capabilities:
            parsed_capabilities.append(capability)
    return OpenClawRemoteAdapterProbe(status=status, platform=platform, capabilities=tuple(parsed_capabilities))


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
    "OpenClawRemoteAdapterProbe",
    "probe_remote_openclaw_adapter",
    "run_remote_openclaw_operation",
]