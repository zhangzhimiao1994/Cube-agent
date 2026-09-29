from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from agent_hub.skills.sandbox.base import SkillInvocation, SkillSandbox
from agent_hub.skills.sandbox.broker import BrokerBackend, probe_skill_broker
from agent_hub.skills.sandbox.docker import DockerSkillSandbox
from agent_hub.skills.sandbox.remote import REMOTE_SANDBOX_PROTOCOL, REMOTE_SANDBOX_VERSION
from agent_hub.skills.sandbox.systemd import SystemdSkillSandbox

_MAX_REQUEST_BYTES = 90 * 1024 * 1024
_MAX_WORKSPACE_BYTES = 256 * 1024 * 1024
_MAX_WORKSPACE_FILES = 2_000


class RemoteRunnerError(RuntimeError):
    pass


async def run_request(payload: Mapping[str, object]) -> dict[str, object]:
    _validate_envelope(payload, action="run")
    invocation_payload = payload.get("invocation")
    if not isinstance(invocation_payload, Mapping):
        raise RemoteRunnerError("invocation is required")
    root = Path(os.getenv("AGENT_HUB_REMOTE_RUNNER_ROOT", "/var/lib/agent-hub/skills/remote"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="request-", dir=root) as temp:
        request_root = Path(temp)
        invocation = _materialize_invocation(invocation_payload, request_root)
        backend = os.getenv("AGENT_HUB_REMOTE_RUNNER_BACKEND", "systemd").strip().casefold()
        if backend == "systemd":
            sandbox: SkillSandbox = SystemdSkillSandbox()
        elif backend == "docker":
            sandbox = DockerSkillSandbox()
        else:
            raise RemoteRunnerError("remote runner backend must be systemd or docker")
        result = await sandbox.run(invocation)
        archive = _workspace_archive(invocation.writable_tmp_path)
        return {
            "protocol": REMOTE_SANDBOX_PROTOCOL,
            "version": REMOTE_SANDBOX_VERSION,
            "result": result.model_dump(mode="json"),
            "workspace_archive_base64": base64.b64encode(archive).decode("ascii"),
        }


async def terminate_request(execution_id: str) -> dict[str, object]:
    backend = os.getenv("AGENT_HUB_REMOTE_RUNNER_BACKEND", "systemd").strip().casefold()
    sandbox = DockerSkillSandbox() if backend == "docker" else SystemdSkillSandbox()
    if backend not in {"systemd", "docker"}:
        raise RemoteRunnerError("remote runner backend must be systemd or docker")
    await sandbox.terminate(execution_id)
    return {
        "protocol": REMOTE_SANDBOX_PROTOCOL,
        "version": REMOTE_SANDBOX_VERSION,
        "terminated": True,
    }


def health_response() -> dict[str, object]:
    backend = os.getenv("AGENT_HUB_REMOTE_RUNNER_BACKEND", "systemd").strip().casefold()
    reason = (
        probe_skill_broker(cast(BrokerBackend, backend))
        if backend in {"systemd", "docker"}
        else "invalid_backend"
    )
    return {
        "protocol": REMOTE_SANDBOX_PROTOCOL,
        "version": REMOTE_SANDBOX_VERSION,
        "ready": reason is None,
        "backend": backend,
        "reason": reason,
    }


def _materialize_invocation(payload: Mapping[str, object], root: Path) -> SkillInvocation:
    package_base64 = payload.get("package_base64")
    package_sha256 = payload.get("package_sha256")
    if not isinstance(package_base64, str) or not isinstance(package_sha256, str):
        raise RemoteRunnerError("package payload is invalid")
    try:
        package = base64.b64decode(package_base64, validate=True)
    except ValueError as error:
        raise RemoteRunnerError("package payload is invalid") from error
    if hashlib.sha256(package).hexdigest() != package_sha256:
        raise RemoteRunnerError("package checksum mismatch")
    package_path = root / "skill.zip"
    package_path.write_bytes(package)
    input_root = root / "inputs"
    input_root.mkdir(mode=0o700)
    input_paths: list[Path] = []
    raw_inputs = payload.get("read_only_inputs", [])
    if not isinstance(raw_inputs, list):
        raise RemoteRunnerError("read-only inputs are invalid")
    for index, item in enumerate(raw_inputs):
        if not isinstance(item, Mapping) or not isinstance(item.get("content_base64"), str):
            raise RemoteRunnerError("read-only input is invalid")
        try:
            content = base64.b64decode(cast(str, item["content_base64"]), validate=True)
        except ValueError as error:
            raise RemoteRunnerError("read-only input is invalid") from error
        path = input_root / f"input-{index}"
        path.write_bytes(content)
        input_paths.append(path)
    workdir = root / "workspace"
    workdir.mkdir(mode=0o700)
    values = dict(payload)
    values.pop("package_base64", None)
    values.pop("read_only_inputs", None)
    values["package_path"] = package_path
    values["read_only_inputs"] = tuple(input_paths)
    values["writable_tmp_path"] = workdir
    try:
        return SkillInvocation.model_validate(values, strict=True)
    except ValueError as error:
        raise RemoteRunnerError("invocation payload is invalid") from error


def _workspace_archive(root: Path) -> bytes:
    buffer = io.BytesIO()
    file_count = 0
    total_bytes = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            file_count += 1
            total_bytes += path.stat().st_size
            if file_count > _MAX_WORKSPACE_FILES or total_bytes > _MAX_WORKSPACE_BYTES:
                raise RemoteRunnerError("workspace output exceeds remote runner limits")
            archive.write(path, path.relative_to(root).as_posix())
    return buffer.getvalue()


def _validate_envelope(payload: Mapping[str, object], *, action: str) -> None:
    if (
        payload.get("protocol") != REMOTE_SANDBOX_PROTOCOL
        or payload.get("version") != REMOTE_SANDBOX_VERSION
        or payload.get("action") != action
    ):
        raise RemoteRunnerError("remote sandbox protocol mismatch")


def _read_request() -> Mapping[str, object]:
    data = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
    if len(data) > _MAX_REQUEST_BYTES:
        raise RemoteRunnerError("remote sandbox request is too large")
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RemoteRunnerError("remote sandbox request is invalid JSON") from error
    if not isinstance(payload, Mapping):
        raise RemoteRunnerError("remote sandbox request must be an object")
    return cast(Mapping[str, object], payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Hub remote Skill sandbox runner")
    parser.add_argument("action", choices=("health", "run", "terminate"))
    parser.add_argument("execution_id", nargs="?")
    args = parser.parse_args()
    try:
        if args.action == "health":
            response = health_response()
        elif args.action == "terminate":
            if not args.execution_id:
                raise RemoteRunnerError("execution_id is required")
            response = asyncio.run(terminate_request(args.execution_id))
        else:
            response = asyncio.run(run_request(_read_request()))
        print(json.dumps(response, sort_keys=True, separators=(",", ":")))
    except (OSError, RemoteRunnerError, ValueError) as error:
        print(f"remote runner failed: {error}", file=sys.stderr)
        raise SystemExit(78) from None


if __name__ == "__main__":
    main()
