from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast
from urllib.parse import urlsplit

import httpx

from agent_hub.skills.sandbox.base import (
    SkillInvocation,
    SkillResult,
    run_subprocess_with_limits,
    validate_execution_id,
)

REMOTE_SANDBOX_PROTOCOL = "agent-hub.skill-sandbox"
REMOTE_SANDBOX_VERSION = 1
RemoteProvider = Literal["modal", "daytona", "vercel"]
_SAFE_SSH_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_MAX_ARCHIVE_FILES = 2_000
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HttpRemoteSandboxSettings:
    provider: RemoteProvider
    endpoint: str
    bearer_token: str | None = None
    connect_timeout_seconds: float = 10.0
    max_request_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if parsed.scheme != "https":
            raise ValueError("remote sandbox endpoint must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("remote sandbox endpoint must not contain credentials")
        if not parsed.hostname or parsed.query or parsed.fragment:
            raise ValueError("remote sandbox endpoint must be an absolute base URL")
        if self.max_request_bytes < 1:
            raise ValueError("max_request_bytes must be positive")


class HttpRemoteSkillSandbox:
    def __init__(
        self,
        settings: HttpRemoteSandboxSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def run(self, invocation: SkillInvocation) -> SkillResult:
        payload = _run_payload(invocation, max_request_bytes=self._settings.max_request_bytes)
        response = await self._request("/v1/invoke", payload, invocation.timeout_seconds)
        result, archive = _parse_remote_response(response)
        if archive is not None:
            _restore_workspace_archive(archive, invocation.writable_tmp_path)
        return result

    async def terminate(self, execution_id: str) -> None:
        validate_execution_id(execution_id)
        response = await self._request(
            "/v1/terminate",
            {
                "protocol": REMOTE_SANDBOX_PROTOCOL,
                "version": REMOTE_SANDBOX_VERSION,
                "action": "terminate",
                "execution_id": execution_id,
            },
            30,
        )
        if response.get("terminated") is not True:
            raise OSError("remote sandbox did not confirm termination")

    async def _request(
        self,
        path: str,
        payload: dict[str, object],
        timeout_seconds: int,
    ) -> dict[str, object]:
        headers = {"Content-Type": "application/json"}
        if self._settings.bearer_token:
            headers["Authorization"] = f"Bearer {self._settings.bearer_token}"
        timeout = httpx.Timeout(
            timeout_seconds + self._settings.connect_timeout_seconds,
            connect=self._settings.connect_timeout_seconds,
        )
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=False,
                timeout=timeout,
            ) as client:
                response = await client.post(
                    f"{self._settings.endpoint.rstrip('/')}{path}",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                value = response.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as error:
            raise OSError("remote sandbox request failed") from error
        if not isinstance(value, dict):
            raise OSError("remote sandbox response must be an object")
        return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True)
class SshSandboxSettings:
    host: str
    user: str
    known_hosts_file: Path
    port: int = 22
    identity_file: Path | None = None
    executable: str = "ssh"
    runner: str = "agent-hub-skill-remote-runner"
    connect_timeout_seconds: int = 10
    max_request_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if _SAFE_SSH_VALUE.fullmatch(self.host) is None:
            raise ValueError("SSH host is invalid")
        if _SAFE_SSH_VALUE.fullmatch(self.user) is None:
            raise ValueError("SSH user is invalid")
        if _SAFE_SSH_VALUE.fullmatch(self.runner) is None:
            raise ValueError("SSH runner is invalid")
        if not 1 <= self.port <= 65535:
            raise ValueError("SSH port is invalid")
        if not self.known_hosts_file.is_file():
            raise ValueError("SSH known_hosts file is required")
        if self.identity_file is not None and not self.identity_file.is_file():
            raise ValueError("SSH identity file does not exist")


class SshSkillSandbox:
    def __init__(self, settings: SshSandboxSettings) -> None:
        self._settings = settings

    async def run(self, invocation: SkillInvocation) -> SkillResult:
        payload = _run_payload(invocation, max_request_bytes=self._settings.max_request_bytes)
        envelope = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        transport = await run_subprocess_with_limits(
            build_ssh_command(self._settings, action="run"),
            invocation,
            stdin_payload=envelope,
        )
        if transport.timed_out:
            return transport
        if transport.exit_code != 0:
            raise OSError("SSH sandbox runner failed")
        try:
            response = json.loads(transport.stdout)
        except json.JSONDecodeError as error:
            raise OSError("SSH sandbox returned invalid JSON") from error
        if not isinstance(response, dict):
            raise OSError("SSH sandbox response must be an object")
        result, archive = _parse_remote_response(cast(dict[str, object], response))
        if archive is not None:
            _restore_workspace_archive(archive, invocation.writable_tmp_path)
        return result

    async def terminate(self, execution_id: str) -> None:
        validate_execution_id(execution_id)
        process = await asyncio.create_subprocess_exec(
            *build_ssh_command(self._settings, action="terminate", execution_id=execution_id),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=self._settings.connect_timeout_seconds + 30,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise OSError("SSH sandbox termination timed out") from None
        if process.returncode != 0:
            raise OSError("SSH sandbox termination failed")


def build_ssh_command(
    settings: SshSandboxSettings,
    *,
    action: Literal["run", "terminate", "health"],
    execution_id: str | None = None,
) -> tuple[str, ...]:
    command = [
        settings.executable,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={settings.known_hosts_file}",
        "-o",
        f"ConnectTimeout={settings.connect_timeout_seconds}",
        "-p",
        str(settings.port),
    ]
    if settings.identity_file is not None:
        command.extend(("-i", str(settings.identity_file)))
    command.append(f"{settings.user}@{settings.host}")
    command.extend((settings.runner, action))
    if execution_id is not None:
        command.append(validate_execution_id(execution_id))
    return tuple(command)


def configured_remote_skill_sandboxes() -> dict[str, HttpRemoteSkillSandbox | SshSkillSandbox]:
    sandboxes: dict[str, HttpRemoteSkillSandbox | SshSkillSandbox] = {}
    ssh_settings = ssh_settings_from_environment()
    if ssh_settings is not None:
        sandboxes["ssh"] = SshSkillSandbox(ssh_settings)
    for provider in ("modal", "daytona", "vercel"):
        settings = http_remote_settings_from_environment(provider)
        if settings is not None:
            sandboxes[provider] = HttpRemoteSkillSandbox(settings)
    return sandboxes


def remote_backend_unavailable_reason(backend: str) -> str | None:
    if backend == "ssh":
        try:
            ssh_settings = ssh_settings_from_environment()
        except ValueError:
            return "ssh_configuration_invalid"
        if ssh_settings is None:
            return "ssh_not_configured"
        return _probe_ssh(ssh_settings)
    if backend in {"modal", "daytona", "vercel"}:
        provider = cast(RemoteProvider, backend)
        try:
            http_settings = http_remote_settings_from_environment(provider)
        except ValueError:
            return f"{provider}_configuration_invalid"
        if http_settings is None:
            return f"{provider}_not_configured"
        return _probe_http(http_settings)
    return "remote_backend_unknown"


def ssh_settings_from_environment() -> SshSandboxSettings | None:
    host = os.getenv("AGENT_HUB_SKILL_SSH_HOST", "").strip()
    user = os.getenv("AGENT_HUB_SKILL_SSH_USER", "").strip()
    known_hosts = os.getenv("AGENT_HUB_SKILL_SSH_KNOWN_HOSTS_FILE", "").strip()
    configured = [bool(host), bool(user), bool(known_hosts)]
    if not any(configured):
        return None
    if not all(configured):
        raise ValueError("SSH host, user and known_hosts must be configured together")
    identity = os.getenv("AGENT_HUB_SKILL_SSH_IDENTITY_FILE", "").strip()
    return SshSandboxSettings(
        host=host,
        user=user,
        known_hosts_file=Path(known_hosts),
        port=int(os.getenv("AGENT_HUB_SKILL_SSH_PORT", "22")),
        identity_file=Path(identity) if identity else None,
        executable=os.getenv("AGENT_HUB_SKILL_SSH_EXECUTABLE", "ssh").strip() or "ssh",
        runner=(
            os.getenv("AGENT_HUB_SKILL_SSH_RUNNER", "agent-hub-skill-remote-runner").strip()
            or "agent-hub-skill-remote-runner"
        ),
    )


def http_remote_settings_from_environment(
    provider: RemoteProvider,
) -> HttpRemoteSandboxSettings | None:
    prefix = f"AGENT_HUB_SKILL_{provider.upper()}"
    endpoint = os.getenv(f"{prefix}_ENDPOINT", "").strip()
    if not endpoint:
        return None
    token = os.getenv(f"{prefix}_TOKEN", "").strip()
    return HttpRemoteSandboxSettings(
        provider=provider,
        endpoint=endpoint,
        bearer_token=token or None,
    )


def _probe_http(settings: HttpRemoteSandboxSettings) -> str | None:
    headers: dict[str, str] = {}
    if settings.bearer_token:
        headers["Authorization"] = f"Bearer {settings.bearer_token}"
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=settings.connect_timeout_seconds,
        ) as client:
            response = client.get(
                f"{settings.endpoint.rstrip('/')}/v1/health",
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError, ValueError):
        return f"{settings.provider}_health_check_failed"
    if not isinstance(payload, dict) or not _valid_protocol_payload(payload):
        return f"{settings.provider}_protocol_mismatch"
    if payload.get("ready") is not True:
        return f"{settings.provider}_health_check_failed"
    return None


def _probe_ssh(settings: SshSandboxSettings) -> str | None:
    try:
        result = subprocess.run(
            build_ssh_command(settings, action="health"),
            check=False,
            capture_output=True,
            text=True,
            timeout=settings.connect_timeout_seconds + 5,
        )
    except (OSError, subprocess.SubprocessError):
        return "ssh_health_check_failed"
    if result.returncode != 0:
        return "ssh_health_check_failed"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "ssh_protocol_mismatch"
    if not isinstance(payload, dict) or not _valid_protocol_payload(payload):
        return "ssh_protocol_mismatch"
    if payload.get("ready") is not True:
        return "ssh_health_check_failed"
    return None


def _valid_protocol_payload(payload: dict[str, object]) -> bool:
    return (
        payload.get("protocol") == REMOTE_SANDBOX_PROTOCOL
        and payload.get("version") == REMOTE_SANDBOX_VERSION
    )


def _run_payload(invocation: SkillInvocation, *, max_request_bytes: int) -> dict[str, object]:
    package = invocation.package_path.read_bytes()
    if hashlib.sha256(package).hexdigest() != invocation.package_sha256:
        raise OSError("skill package checksum mismatch")
    read_only_inputs: list[dict[str, str]] = []
    total_bytes = len(package)
    for index, path in enumerate(invocation.read_only_inputs):
        if not path.is_file():
            raise OSError("remote sandbox read-only input must be a file")
        content = path.read_bytes()
        total_bytes += len(content)
        read_only_inputs.append(
            {
                "name": f"input-{index}-{path.name}",
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        )
    if total_bytes > max_request_bytes:
        raise OSError("remote sandbox request exceeds configured transfer limit")
    return {
        "protocol": REMOTE_SANDBOX_PROTOCOL,
        "version": REMOTE_SANDBOX_VERSION,
        "action": "run",
        "invocation": {
            "execution_id": invocation.execution_id,
            "package_sha256": invocation.package_sha256,
            "package_base64": base64.b64encode(package).decode("ascii"),
            "input": invocation.input,
            "timeout_seconds": invocation.timeout_seconds,
            "output_limit_bytes": invocation.output_limit_bytes,
            "memory_limit_bytes": invocation.memory_limit_bytes,
            "cpu_quota_percent": invocation.cpu_quota_percent,
            "sandbox_profile": invocation.sandbox_profile,
            "network_allowlist": list(invocation.network_allowlist),
            "selected_secret_refs": list(invocation.selected_secret_refs),
            "read_only_inputs": read_only_inputs,
        },
    }


def _parse_remote_response(response: dict[str, object]) -> tuple[SkillResult, bytes | None]:
    if (
        response.get("protocol") != REMOTE_SANDBOX_PROTOCOL
        or response.get("version") != REMOTE_SANDBOX_VERSION
    ):
        raise OSError("remote sandbox protocol mismatch")
    try:
        result = SkillResult.model_validate(response.get("result"), strict=True)
    except ValueError as error:
        raise OSError("remote sandbox result is invalid") from error
    encoded_archive = response.get("workspace_archive_base64")
    if encoded_archive is None:
        return result, None
    if not isinstance(encoded_archive, str):
        raise OSError("remote sandbox workspace archive is invalid")
    try:
        return result, base64.b64decode(encoded_archive, validate=True)
    except ValueError as error:
        raise OSError("remote sandbox workspace archive is invalid") from error


def _restore_workspace_archive(data: bytes, destination: Path) -> None:
    if len(data) > _MAX_ARCHIVE_BYTES:
        raise OSError("remote sandbox workspace archive is too large")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if len(files) > _MAX_ARCHIVE_FILES:
                raise OSError("remote sandbox workspace archive has too many files")
            total = 0
            for item in files:
                path = PurePosixPath(item.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise OSError("remote sandbox workspace archive path is unsafe")
                total += item.file_size
                if total > _MAX_ARCHIVE_BYTES:
                    raise OSError("remote sandbox workspace archive expands too large")
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(item))
    except zipfile.BadZipFile as error:
        raise OSError("remote sandbox workspace archive is not a zip") from error
