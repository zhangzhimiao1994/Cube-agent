from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import logging
import os
import shutil
import socket
import struct
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, model_validator

from agent_hub.skills.sandbox.base import (
    SkillInvocation,
    SkillResult,
    run_subprocess_with_limits,
    validate_execution_id,
)

BROKER_SOCKET_PATH = Path("/run/agent-hub/skill-broker.sock")
_PROTOCOL_VERSION: Literal[1] = 1
_MAX_REQUEST_BYTES = 2_000_000
_MAX_RESPONSE_BYTES = 22_000_000
_FRAME_HEADER_BYTES = 4
_SANDBOX_ROOT = "/run/agent-hub-skill"
_LOGGER = logging.getLogger(__name__)


class BrokerRequestError(RuntimeError):
    pass


class BrokerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[1] = _PROTOCOL_VERSION
    action: Literal["probe", "run", "terminate"]
    invocation: SkillInvocation | None = None
    execution_id: str | None = None

    @model_validator(mode="after")
    def validate_action_payload(self) -> BrokerRequest:
        if self.action == "run":
            if self.invocation is None or self.execution_id is not None:
                raise ValueError("run requests require only invocation")
        elif self.action == "terminate":
            if self.invocation is not None or self.execution_id is None:
                raise ValueError("terminate requests require only execution_id")
            validate_execution_id(self.execution_id)
        elif self.invocation is not None or self.execution_id is not None:
            raise ValueError("probe requests do not accept a payload")
        return self

    @classmethod
    def probe(cls) -> BrokerRequest:
        return cls(action="probe")

    @classmethod
    def run(cls, invocation: SkillInvocation) -> BrokerRequest:
        return cls(action="run", invocation=invocation)

    @classmethod
    def terminate(cls, execution_id: str) -> BrokerRequest:
        validate_execution_id(execution_id)
        return cls(action="terminate", execution_id=execution_id)


class BrokerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    ok: bool
    result: SkillResult | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BrokerPolicy:
    skill_root: Path = Path("/var/lib/agent-hub/skills")
    allowed_uid: int = 10001
    service_group: str = "agent-hub"
    python: str = "/opt/agent-hub/current/.venv/bin/python"
    source_path: Path = Path("/opt/agent-hub/current/src")
    systemd_run: str = "/usr/bin/systemd-run"
    systemctl: str = "/usr/bin/systemctl"
    read_only_roots: tuple[Path, ...] = (
        Path("/var/lib/agent-hub/attachments"),
        Path("/var/lib/agent-hub/workspaces"),
    )


def validate_broker_request(
    request: BrokerRequest,
    *,
    peer_uid: int,
    policy: BrokerPolicy,
) -> BrokerRequest:
    if peer_uid != policy.allowed_uid:
        raise BrokerRequestError("caller uid is not authorized")
    if request.action == "run":
        invocation = request.invocation
        if invocation is None:
            raise BrokerRequestError("run invocation is missing")
        if invocation.network_allowlist:
            raise BrokerRequestError("network allowlist is not supported by the systemd broker")
        if invocation.selected_secret_refs:
            raise BrokerRequestError("secret references are not supported by the systemd broker")
        _require_regular_file(invocation.package_path, policy.skill_root, "package path")
        if invocation.package_path.suffix.casefold() != ".zip":
            raise BrokerRequestError("package path must identify a zip archive")
        _require_directory(invocation.writable_tmp_path, policy.skill_root, "work directory")
        for input_path in invocation.read_only_inputs:
            if not any(_is_regular_file_beneath(input_path, root) for root in policy.read_only_roots):
                raise BrokerRequestError("read-only input path is outside allowed roots")
    return request


def build_broker_systemd_command(
    invocation: SkillInvocation | None,
    policy: BrokerPolicy,
) -> tuple[str, ...]:
    if invocation is None:
        raise BrokerRequestError("run invocation is missing")
    package_path = _systemd_path(invocation.package_path.resolve(strict=True))
    workdir = _systemd_path(invocation.writable_tmp_path.resolve(strict=True))
    command: list[str] = [
        policy.systemd_run,
        "--wait",
        "--collect",
        "--pipe",
        "--quiet",
        "--unit",
        f"agent-hub-skill-{invocation.execution_id}",
        "-p",
        "DynamicUser=yes",
        "-p",
        f"SupplementaryGroups={policy.service_group}",
        "-p",
        "UMask=0077",
        "-p",
        "NoNewPrivileges=yes",
        "-p",
        "ProtectSystem=strict",
        "-p",
        "ProtectHome=yes",
        "-p",
        "PrivateTmp=yes",
        "-p",
        "PrivateDevices=yes",
        "-p",
        "ProtectKernelTunables=yes",
        "-p",
        "ProtectKernelModules=yes",
        "-p",
        "ProtectControlGroups=yes",
        "-p",
        "RestrictSUIDSGID=yes",
        "-p",
        "LockPersonality=yes",
        "-p",
        "PrivateNetwork=yes",
        "-p",
        "IPAddressDeny=any",
        "-p",
        "RestrictAddressFamilies=AF_UNIX",
        "-p",
        "CapabilityBoundingSet=",
        "-p",
        "TasksMax=64",
        "-p",
        f"MemoryMax={invocation.memory_limit_bytes}",
        "-p",
        f"CPUQuota={invocation.cpu_quota_percent}%",
        "-p",
        f"RuntimeMaxSec={invocation.timeout_seconds}s",
        "-p",
        "InaccessiblePaths=/var/lib/agent-hub",
        "-p",
        f"TemporaryFileSystem={_SANDBOX_ROOT}:rw,nosuid,nodev,noexec,size=64M",
        "-p",
        f"BindReadOnlyPaths={package_path}:{_SANDBOX_ROOT}/package.zip",
        "-p",
        f"BindPaths={workdir}:{_SANDBOX_ROOT}/workspace",
        "-p",
        f"WorkingDirectory={_SANDBOX_ROOT}/workspace",
        "-E",
        f"PYTHONPATH={_systemd_path(policy.source_path)}",
        "-E",
        f"AGENT_HUB_EXECUTION_ID={invocation.execution_id}",
        "-E",
        f"AGENT_HUB_PACKAGE_PATH={_SANDBOX_ROOT}/package.zip",
        "-E",
        f"AGENT_HUB_PACKAGE_SHA256={invocation.package_sha256}",
        "-E",
        f"AGENT_HUB_OUTPUT_LIMIT_BYTES={invocation.output_limit_bytes}",
        "-E",
        f"AGENT_HUB_SANDBOX_PROFILE={invocation.sandbox_profile}",
        "-E",
        f"AGENT_HUB_WORKDIR={_SANDBOX_ROOT}/workspace",
        "-E",
        f"AGENT_HUB_TIMEOUT_SECONDS={invocation.timeout_seconds}",
    ]
    for index, input_path in enumerate(invocation.read_only_inputs):
        command.extend(
            (
                "-p",
                f"BindReadOnlyPaths={_systemd_path(input_path.resolve(strict=True))}:{_SANDBOX_ROOT}/inputs/{index}",
            )
        )
    command.extend((policy.python, "-m", "agent_hub.skills.runner"))
    return tuple(command)


def build_broker_terminate_command(execution_id: str, policy: BrokerPolicy) -> tuple[str, ...]:
    validate_execution_id(execution_id)
    return (
        policy.systemctl,
        "kill",
        "--kill-who=all",
        f"agent-hub-skill-{execution_id}.service",
    )


async def request_systemd_broker(
    request: BrokerRequest,
    *,
    socket_path: Path = BROKER_SOCKET_PATH,
) -> BrokerResponse:
    try:
        open_unix_connection = cast(Any, _module_attribute(asyncio, "open_unix_connection"))
    except AttributeError:
        raise OSError("Unix sockets are unavailable for the systemd Skill broker") from None
    timeout_seconds = request.invocation.timeout_seconds + 10 if request.invocation else 10
    async with asyncio.timeout(timeout_seconds):
        reader, writer = await open_unix_connection(socket_path.as_posix())
        try:
            await _write_frame(writer, request.model_dump_json().encode("utf-8"))
            payload = await _read_frame(reader, _MAX_RESPONSE_BYTES)
            return BrokerResponse.model_validate_json(payload, strict=True)
        finally:
            writer.close()
            await writer.wait_closed()


def probe_systemd_broker(socket_path: Path = BROKER_SOCKET_PATH) -> str | None:
    try:
        address_family = _module_attribute(socket, "AF_UNIX")
        with socket.socket(address_family, socket.SOCK_STREAM) as client:
            client.settimeout(6)
            client.connect(socket_path.as_posix())
            payload = BrokerRequest.probe().model_dump_json().encode("utf-8")
            client.sendall(struct.pack("!I", len(payload)) + payload)
            header = _recv_exact(client, _FRAME_HEADER_BYTES)
            length = struct.unpack("!I", header)[0]
            if length < 1 or length > _MAX_RESPONSE_BYTES:
                return "systemd_broker_invalid_response"
            response = BrokerResponse.model_validate_json(_recv_exact(client, length), strict=True)
    except (AttributeError, OSError, ValueError):
        return "systemd_broker_unavailable"
    if response.ok:
        return None
    return response.error or "systemd_transient_unit_unavailable"


class SystemdBroker:
    def __init__(self, policy: BrokerPolicy) -> None:
        self._policy = policy
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._execution_slots = asyncio.Semaphore(8)

    async def handle(self, request: BrokerRequest, *, peer_uid: int) -> BrokerResponse:
        try:
            validated = validate_broker_request(request, peer_uid=peer_uid, policy=self._policy)
            if validated.action == "probe":
                return await self._probe()
            if validated.action == "terminate":
                await self._terminate(validated.execution_id or "")
                return BrokerResponse(ok=True)
            invocation = validated.invocation
            if invocation is None:
                raise BrokerRequestError("run invocation is missing")
            async with self._execution_slots:
                result = await self._run(invocation)
            return BrokerResponse(ok=True, result=result)
        except (BrokerRequestError, OSError, subprocess.SubprocessError, ValueError) as error:
            return BrokerResponse(ok=False, error=str(error))

    async def _run(self, invocation: SkillInvocation) -> SkillResult:
        command = build_broker_systemd_command(invocation, self._policy)
        try:
            return await run_subprocess_with_limits(
                command,
                invocation,
                process_started=lambda process: self._processes.__setitem__(
                    invocation.execution_id, process
                ),
                on_forced_terminate=lambda: self._terminate(invocation.execution_id),
            )
        finally:
            self._processes.pop(invocation.execution_id, None)

    async def _terminate(self, execution_id: str) -> None:
        process = self._processes.get(execution_id)
        if process is not None and process.returncode is None:
            process.kill()
        command = build_broker_terminate_command(execution_id, self._policy)
        killer = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(killer.wait(), timeout=5)

    async def _probe(self) -> BrokerResponse:
        unit = f"agent-hub-skill-probe-{uuid.uuid4().hex[:12]}"
        command = (
            self._policy.systemd_run,
            "--wait",
            "--collect",
            "--quiet",
            "--unit",
            unit,
            "-p",
            "DynamicUser=yes",
            "-p",
            "NoNewPrivileges=yes",
            "-p",
            "ProtectSystem=strict",
            "-p",
            "PrivateNetwork=yes",
            "-p",
            "IPAddressDeny=any",
            "-p",
            "RuntimeMaxSec=5s",
            "/usr/bin/true",
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=6)
        except (OSError, TimeoutError):
            _LOGGER.exception("systemd Skill broker probe could not start a transient unit")
            return BrokerResponse(ok=False, error="systemd_transient_unit_unavailable")
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            _LOGGER.warning("systemd Skill broker probe failed: %s", detail or "no stderr")
            return BrokerResponse(ok=False, error="systemd_transient_unit_unavailable")
        return BrokerResponse(ok=True)


async def _serve_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    broker: SystemdBroker,
) -> None:
    try:
        peer_uid = _peer_uid(writer)
        payload = await _read_frame(reader, _MAX_REQUEST_BYTES)
        request = BrokerRequest.model_validate_json(payload, strict=True)
        response = await broker.handle(request, peer_uid=peer_uid)
    except (BrokerRequestError, OSError, ValueError) as error:
        response = BrokerResponse(ok=False, error=str(error))
    try:
        await _write_frame(writer, response.model_dump_json().encode("utf-8"))
    finally:
        writer.close()
        await writer.wait_closed()


async def _write_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    writer.write(struct.pack("!I", len(payload)) + payload)
    await writer.drain()


async def _read_frame(reader: asyncio.StreamReader, limit: int) -> bytes:
    header = await reader.readexactly(_FRAME_HEADER_BYTES)
    length = struct.unpack("!I", header)[0]
    if length < 1 or length > limit:
        raise BrokerRequestError("broker frame size is invalid")
    return await reader.readexactly(length)


def _recv_exact(client: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = client.recv(length - len(chunks))
        if not chunk:
            raise OSError("broker connection closed")
        chunks.extend(chunk)
    return bytes(chunks)


def _peer_uid(writer: asyncio.StreamWriter) -> int:
    transport_socket = writer.get_extra_info("socket")
    if transport_socket is None or not hasattr(socket, "SO_PEERCRED"):
        raise BrokerRequestError("peer credentials are unavailable")
    credentials = transport_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    _, uid, _ = struct.unpack("3i", credentials)
    return int(uid)


def _require_regular_file(path: Path, root: Path, label: str) -> Path:
    if not _is_regular_file_beneath(path, root):
        raise BrokerRequestError(f"{label} is outside allowed root or is not a regular file")
    return path.resolve(strict=True)


def _is_regular_file_beneath(path: Path, root: Path) -> bool:
    try:
        raw = path.absolute()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return raw == resolved and resolved.is_file()


def _require_directory(path: Path, root: Path, label: str) -> Path:
    try:
        raw = path.absolute()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        raise BrokerRequestError(f"{label} is outside allowed root") from None
    if raw != resolved or not resolved.is_dir():
        raise BrokerRequestError(f"{label} is outside allowed root or is not a directory")
    return resolved


def _systemd_path(path: Path) -> str:
    value = path.as_posix()
    if (
        not value
        or (os.name == "posix" and ":" in value)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise BrokerRequestError("systemd paths must not contain colons or control characters")
    return value


def _activated_socket() -> socket.socket:
    listen_pid = int(os.environ.get("LISTEN_PID", "0") or "0")
    listen_fds = int(os.environ.get("LISTEN_FDS", "0") or "0")
    if listen_pid != os.getpid() or listen_fds != 1:
        raise BrokerRequestError("exactly one systemd-activated socket is required")
    activated = socket.socket(fileno=3)
    activated.setblocking(False)
    return activated


def load_broker_policy() -> BrokerPolicy:
    group_module = importlib.import_module("grp")
    password_module = importlib.import_module("pwd")
    _module_attribute(group_module, "getgrnam")("agent-hub")
    service_user = _module_attribute(password_module, "getpwnam")("agent-hub")
    return BrokerPolicy(allowed_uid=int(service_user.pw_uid))


async def _main_async() -> None:
    policy = load_broker_policy()
    broker = SystemdBroker(policy)
    start_unix_server = cast(Any, _module_attribute(asyncio, "start_unix_server"))
    server = await start_unix_server(
        lambda reader, writer: _serve_client(reader, writer, broker),
        sock=_activated_socket(),
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Hub privileged systemd Skill broker")
    parser.parse_args()
    get_effective_uid = getattr(os, "geteuid", None)
    if get_effective_uid is None or get_effective_uid() != 0:
        raise SystemExit("systemd Skill broker must run as root")
    if shutil.which("systemd-run") != "/usr/bin/systemd-run":
        raise SystemExit("/usr/bin/systemd-run is required")
    if shutil.which("systemctl") != "/usr/bin/systemctl":
        raise SystemExit("/usr/bin/systemctl is required")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main_async())


def _module_attribute(module: object, name: str) -> Any:
    return getattr(module, name)


if __name__ == "__main__":
    main()
