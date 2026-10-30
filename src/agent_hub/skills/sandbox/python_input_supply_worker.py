"""Offline privileged supply worker; only its distinct nonprivileged fetch unit networks."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import re
import shutil
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from agent_hub.skills.sandbox.base import SkillResult, run_subprocess_with_limits
from agent_hub.skills.sandbox.project import (
    ProjectExecutionBroker,
    ProjectInvocation,
    _Execution,
    _Limits,
    _uninterrupted,
)
from agent_hub.skills.sandbox.project_contract_broker import (
    _encoded,
    _object,
    _Observed,
    _open_directory,
    _read_control,
    _root,
    _write_all,
    stage_original_contract,
)
from agent_hub.skills.sandbox.python_input_supply_protocol import (
    PythonInputSupplyReceipt,
    PythonInputSupplyRequest,
    PythonInputSupplyResponse,
    PythonInputSupplyStatus,
)
from agent_hub.skills.sandbox.python_project_command import _escaped
from agent_hub.skills.sandbox.python_project_protocol import base_python_invocation
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
    from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
    from agent_hub.skills.sandbox.python_project_backend import RootConfig


def build_fetch_command(
    invocation: ProjectInvocation, policy: BrokerPolicy, *,
    configuration: RootConfig, supply: RootSupplyConfig,
) -> tuple[str, ...]:
    hidden_roots = (Path("/var"), Path("/run"), Path("/tmp"))
    masked = tuple(p for p in dict.fromkeys((Path("/etc/agent-hub"),
        policy.project_workspace_root, configuration.environment_root,
        configuration.wheel_root, supply.state_root))
        if not any(p.is_relative_to(root) for root in hidden_roots))
    properties = (
        "DynamicUser=yes", "SupplementaryGroups=" + policy.service_group,
        "UMask=0077", "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes",
        "PrivateTmp=no", "PrivateDevices=yes", "ProtectProc=invisible", "ProcSubset=pid",
        # Empty filesystems hide host data without conflicting nested inaccessible mounts.
        ("TemporaryFileSystem=/run:rw,nosuid,nodev,noexec,size=1048576,mode=0755 "
         "/var:ro,nosuid,nodev,noexec,size=1048576 /tmp:rw,nosuid,nodev,noexec,size=8388608"),
        "BindReadOnlyPaths=/etc/resolv.conf",
        "CapabilityBoundingSet=", "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "InaccessiblePaths=" + " ".join(_escaped(p) for p in masked),
        "ProtectKernelModules=yes", "ProtectKernelTunables=yes", "ProtectControlGroups=yes",
        "RestrictSUIDSGID=yes", "RestrictRealtime=yes", "LockPersonality=yes", "RemoveIPC=yes",
        "KillMode=control-group", "TimeoutStopSec=2s", "TasksMax=32", "LimitFSIZE=0",
        f"MemoryMax={min(invocation.memory_limit_bytes, 1024**3)}",
        f"CPUQuota={invocation.cpu_quota_percent}%",
        f"RuntimeMaxSec={min(invocation.timeout_seconds, supply.fetch_timeout_seconds) + 2}s",
    )
    command = [policy.systemd_run, "--wait", "--collect", "--pipe", "--quiet", "--unit",
               "agent-hub-project-" + invocation.execution_id]
    for prop in properties:
        command.extend(("-p", prop))
    command.extend(("/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LANG=C.UTF-8",
        Path(policy.python).as_posix().replace("%", "%%"), "-I", "-B", "-m",
        "agent_hub.skills.sandbox.python_input_fetch", "--worker-batch"))
    return tuple(command)


def validate_configuration(policy: BrokerPolicy, configuration: RootConfig, supply: RootSupplyConfig) -> None:
    from agent_hub.skills.sandbox.python_input_supply_catalog import InputSupplyError

    state = supply.state_root
    if (configuration.stage_root != policy.python_project_runtime_root
            or re.fullmatch(r"/var/lib/agent-hub-python-input-supply(?:-[a-f0-9]{32})?", str(state)) is None
            or any(state.is_relative_to(p) or p.is_relative_to(state) for p in (
                configuration.stage_root, configuration.wheel_root, configuration.runtime_root,
                configuration.environment_root, policy.project_workspace_root, policy.source_path))):
        raise InputSupplyError("input_supply_configuration_untrusted")
    with ExitStack() as stack:
        pins: list[_Observed] = []
        state_fd = _root(state, 0, stack, pins, stage=True)
        wheel_fd = _root(configuration.wheel_root, 0, stack, pins, stage=True)
        if os.fstat(state_fd).st_dev != os.fstat(wheel_fd).st_dev:
            raise InputSupplyError("input_supply_configuration_untrusted")
        for pin in pins:
            pin.check()


def _once(parent: int, name: str, value: object) -> None:
    if sys.platform != "linux":
        raise ValueError("input_supply_unsupported")
    raw = _encoded(value)
    if not 0 < len(raw) <= 2_000_000:
        raise ValueError("input_supply_request_invalid")
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=parent)
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(parent)


async def _fetch_inputs(
    entries: tuple[WheelDownload, ...], request: PythonInputSupplyRequest, policy: BrokerPolicy,
    configuration: RootConfig, supply: RootSupplyConfig, deadline: float,
) -> dict[str, bytes]:
    return await fetch_catalog_wheels(entries, base_python_invocation(request.invocation),
        policy, configuration, supply, deadline)


async def _run_fetch_unit(
    invocation: ProjectInvocation, policy: BrokerPolicy, configuration: RootConfig,
    supply: RootSupplyConfig, deadline: float, *, payload_raw: bytes, maximum: int,
    registry: bool = False,
) -> SkillResult:
    """Shared fixed fetch-unit lifetime; callers validate their own bounded data schema."""
    from agent_hub.skills.sandbox.python_input_supply_catalog import InputSupplyError

    if type(invocation) is not ProjectInvocation or invocation.install_allowed is not False:
        raise InputSupplyError("input_supply_request_invalid")
    invocation = ProjectInvocation.model_validate(vars(invocation), strict=True)
    if (type(registry) is not bool or type(payload_raw) is not bytes
        or not 0 < len(payload_raw) <= 512 * 1024 or type(maximum) is not int
        or not 0 < maximum <= 96 * 1024**2):
        raise InputSupplyError("input_supply_request_invalid")
    remaining = min(deadline - time.monotonic(), supply.fetch_timeout_seconds)
    if remaining <= 0:
        raise InputSupplyError("input_supply_timeout")
    job = _Execution(invocation, None)
    command = build_fetch_command(invocation, policy, configuration=configuration, supply=supply)

    if registry:
        module = "agent_hub.skills.sandbox.python_input_fetch"
        if command.count(module) != 1 or command[-1] != "--worker-batch":
            raise InputSupplyError("input_supply_request_invalid")
        command = tuple("agent_hub.skills.sandbox.python_registry_fetch" if item == module
            else "--metadata-worker" if item == "--worker-batch" else item for item in command)

    async def stop() -> None:
        try:
            async with asyncio.timeout(3):
                await run_subprocess_with_limits((policy.systemctl, "kill", "--kill-who=all",
                    "--signal=SIGKILL", job.unit), _Limits(1, 8192), stdin_payload=b"")
                until = asyncio.get_running_loop().time() + 1.5
                while asyncio.get_running_loop().time() < until:
                    quiet = await run_subprocess_with_limits((policy.systemctl, "show", job.unit,
                        "--no-pager", "--property=LoadState,ActiveState,SubState,MainPID,ControlPID,ControlGroup,TasksCurrent"),
                        _Limits(0.5, 8192), stdin_payload=b"")
                    if ProjectExecutionBroker._unit_quiet(job, quiet):
                        return
                    await asyncio.sleep(0.1)
        except TimeoutError:
            raise ProcessCleanupIncomplete("input_supply_cleanup_incomplete") from None
        else:
            raise ProcessCleanupIncomplete("input_supply_cleanup_incomplete")

    initial = await run_subprocess_with_limits((policy.systemctl, "show", job.unit,
        "--no-pager", "--property=LoadState,ActiveState,SubState,MainPID,ControlPID,ControlGroup,TasksCurrent"),
        _Limits(min(3, remaining), 8192), stdin_payload=b"")
    if not ProjectExecutionBroker._unit_quiet(job, initial):
        raise ProcessCleanupIncomplete("input_supply_cleanup_incomplete")
    try:
        remaining = min(deadline - time.monotonic(), supply.fetch_timeout_seconds)
        if remaining <= 0:
            raise InputSupplyError("input_supply_timeout")
        result = await run_subprocess_with_limits(command,
            _Limits(remaining + 2, maximum), stdin_payload=payload_raw,
            on_forced_terminate=stop)
    finally:
        await _uninterrupted(stop())
    if result.exit_code != 0 or result.timed_out:
        raise InputSupplyError("input_supply_unavailable")
    return result


async def fetch_catalog_wheels(
    entries: tuple[WheelDownload, ...], invocation: ProjectInvocation, policy: BrokerPolicy,
    configuration: RootConfig, supply: RootSupplyConfig, deadline: float,
) -> dict[str, bytes]:
    from dataclasses import asdict

    from agent_hub.skills.sandbox.python_input_supply_catalog import InputSupplyError

    if type(invocation) is not ProjectInvocation:
        raise InputSupplyError("input_supply_request_invalid")
    baseinvocation = ProjectInvocation.model_validate(vars(invocation), strict=True)
    if baseinvocation.install_allowed is not False:
        raise InputSupplyError("input_supply_request_invalid")

    remaining = min(deadline - time.monotonic(), supply.fetch_timeout_seconds)
    if remaining <= 0:
        raise InputSupplyError("input_supply_timeout")
    if not entries:
        return {}
    payload_raw = _encoded({"artifacts": [asdict(entry) for entry in entries], "timeout_seconds": remaining})
    total = sum(entry.size_bytes for entry in entries)
    if total > supply.max_total_bytes or len(payload_raw) > 512 * 1024:
        raise InputSupplyError("input_supply_quota")
    maximum = sum(4 * math.ceil(entry.size_bytes / 3) + 6 * len(entry.filename) + 256 for entry in entries) + 8192
    result = await _run_fetch_unit(baseinvocation,policy,configuration,supply,deadline,
        payload_raw=payload_raw,maximum=maximum)
    try:
        payload = json.loads(result.stdout, object_pairs_hook=_object)
        if (type(payload) is not dict or set(payload) != {"schema_version", "artifacts"}
                or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
                or type(payload["artifacts"]) is not list or len(payload["artifacts"]) != len(entries)):
            raise ValueError
        data = {}
        for row, entry in zip(payload["artifacts"], entries, strict=True):
            if (type(row) is not dict or set(row) != {"filename", "body_base64"}
                    or row["filename"] != entry.filename or type(row["body_base64"]) is not str):
                raise ValueError
            raw = base64.b64decode(row["body_base64"], validate=True)
            if len(raw) != entry.size_bytes:
                raise ValueError
            data[entry.filename] = raw
        return data
    except (ValueError, TypeError, UnicodeError):
        raise InputSupplyError("input_supply_artifact_integrity") from None


def _status(
    request: PythonInputSupplyRequest, supply: RootSupplyConfig,
) -> PythonInputSupplyResponse:
    from agent_hub.skills.sandbox.python_input_supply_catalog import InputSupplyError

    assert request.expected_plan_sha256 is not None and request.supply_execution_id is not None
    with ExitStack() as stack:
        pins: list[_Observed] = []
        root = _root(supply.state_root, 0, stack, pins, stage=True)
        try:
            claim = _open_directory(root, request.expected_plan_sha256, 0, stack, pins, mode=0o700)
        except FileNotFoundError:
            status = PythonInputSupplyStatus(state="absent", plan_sha256=request.expected_plan_sha256,
                                            supply_execution_id=request.supply_execution_id)
        else:
            try:
                intent = json.loads(_read_control(claim, "intent.json", 0, pins), object_pairs_hook=_object)
                if (type(intent) is not dict
                        or set(intent) != {"schema_version", "plan", "supply_execution_id"}
                        or type(intent["schema_version"]) is not int or intent["schema_version"] != 1):
                    raise InputSupplyError("input_supply_scope_changed")
                from agent_hub.skills.sandbox.python_input_supply_protocol import (
                    PythonInputSupplyPlan,
                )

                plan = PythonInputSupplyPlan.model_validate_json(_encoded(intent["plan"]), strict=True)
                plan.require_scope(request.invocation)
                if (plan.sha256 != request.expected_plan_sha256
                        or intent.get("supply_execution_id") != request.supply_execution_id):
                    raise InputSupplyError("input_supply_scope_changed")
                try:
                    receipt = PythonInputSupplyReceipt.model_validate_json(
                        _read_control(claim, "receipt.json", 0, pins), strict=True)
                except FileNotFoundError:
                    receipt = None
                if receipt is not None and (receipt.plan != plan
                        or receipt.supply_execution_id != request.supply_execution_id):
                    raise InputSupplyError("input_supply_scope_changed")
                status = PythonInputSupplyStatus(state="supplied" if receipt is not None else "unknown",
                    plan_sha256=plan.sha256, supply_execution_id=request.supply_execution_id, receipt=receipt)
            except FileNotFoundError:
                status = PythonInputSupplyStatus(state="unknown", plan_sha256=request.expected_plan_sha256,
                                                supply_execution_id=request.supply_execution_id)
        for pin in pins:
            pin.check()
    return PythonInputSupplyResponse(action=request.action, execution_id=request.invocation.execution_id,
                                     ok=True, status=status)


def execute(
    request: PythonInputSupplyRequest, policy: BrokerPolicy, configuration: RootConfig,
    supply: RootSupplyConfig, stage: Path,
) -> PythonInputSupplyResponse:
    from agent_hub.capabilities.project_baselines import ProjectBaselineReference
    from agent_hub.skills.sandbox.python_input_publish import publish_verified_wheel
    from agent_hub.skills.sandbox.python_input_supply_catalog import (
        InputSupplyError,
        plan_inputs,
        read_supply_configuration,
    )
    from agent_hub.skills.sandbox.python_project_backend import (
        _admission,
        _roots,
        read_root_configuration,
    )
    from agent_hub.skills.sandbox.python_resolution_overlay import effective_supply, has_overlay

    validate_configuration(policy, configuration, supply)
    base_supply = supply
    deadline = time.monotonic() + request.invocation.timeout_seconds
    with _roots(policy, configuration, stage, False) as roots:
        if request.action == "inspect_python_project_input_supply" and not has_overlay(
            base_supply, base_python_invocation(request.invocation),
            source_sha256=request.invocation.original_reference.input_source_sha256, deadline=deadline,
        ):
            response = _status(request, supply)
            response.require_for(request)
            return response
        with _admission(roots.stage_root_fd, deadline):
            original = stage_original_contract(policy.project_workspace_root,
                base_python_invocation(request.invocation),
                ProjectBaselineReference.from_payload(request.invocation.original_reference.model_dump()),
                stage, policy.allowed_uid)
            supply = effective_supply(base_supply, base_python_invocation(request.invocation),
                source_sha256=original.reference.input_source_sha256, deadline=deadline)
            if request.action == "inspect_python_project_input_supply":
                response = _status(request, supply)
                response.require_for(request)
                return response
            prepared = plan_inputs(original, request.invocation, configuration, supply, deadline=deadline)
            if request.action == "plan_python_project_inputs":
                return PythonInputSupplyResponse(action=request.action, execution_id=request.invocation.execution_id,
                    ok=True, plan=prepared.plan, missing_wheels=prepared.missing_wheels)
            if prepared.plan.sha256 != request.expected_plan_sha256:
                raise InputSupplyError("input_supply_plan_changed")
            required_space = sum(a.size_bytes for a in prepared.plan.artifacts)
            if (required_space > supply.max_total_bytes
                    or shutil.disk_usage(supply.state_root).free < supply.min_free_bytes + 2 * required_space):
                raise InputSupplyError("input_supply_quota")
            with ExitStack() as stack:
                pins: list[_Observed] = []
                root = _root(supply.state_root, 0, stack, pins, stage=True)
                try:
                    os.mkdir(prepared.plan.sha256, 0o700, dir_fd=root)
                except FileExistsError:
                    raise InputSupplyError("input_supply_consumed") from None
                os.fsync(root)
                claim = _open_directory(root, prepared.plan.sha256, 0, stack, pins, mode=0o700)
                _once(claim, "intent.json", {"schema_version": 1,
                    "supply_execution_id": request.invocation.execution_id,
                    "plan": prepared.plan.model_dump(mode="json")})
                for pin in pins:
                    pin.check()
        downloaded = asyncio.run(_fetch_inputs(prepared.downloads, request, policy, configuration, supply, deadline))
        with _admission(roots.stage_root_fd, deadline):
            current_base = read_supply_configuration()
            current_supply = effective_supply(current_base, base_python_invocation(request.invocation),
                source_sha256=original.reference.input_source_sha256, deadline=deadline)
            if (read_root_configuration().to_payload() != configuration.to_payload()
                    or current_base.digest != base_supply.digest
                    or current_supply.digest != supply.digest):
                raise InputSupplyError("input_supply_plan_changed")
            current = plan_inputs(original, request.invocation, configuration, supply, deadline=deadline)
            if current.plan != prepared.plan:
                raise InputSupplyError("input_supply_plan_changed")
            published: list[str] = []
            reused: list[str] = []
            entries = {entry.filename: entry for entry in supply.catalog}
            claim_path = supply.state_root / prepared.plan.sha256
            for artifact in prepared.plan.artifacts:
                if artifact.filename not in current.missing_wheels:
                    reused.append(artifact.filename)
                    continue
                data = downloaded.get(artifact.filename)
                if data is None:
                    raise InputSupplyError("input_supply_plan_changed")
                outcome = publish_verified_wheel(configuration.wheel_root, claim_path,
                    entries[artifact.filename], data, deadline=deadline)
                (published if outcome == "published" else reused).append(artifact.filename)
            after = plan_inputs(original, request.invocation, configuration, supply, deadline=deadline)
            if after.plan != prepared.plan or after.missing_wheels:
                raise ProcessCleanupIncomplete("input_supply_cleanup_incomplete")
            receipt = PythonInputSupplyReceipt(plan=prepared.plan,
                supply_execution_id=request.invocation.execution_id,
                published=tuple(sorted(published)), reused=tuple(sorted(reused)))
            with ExitStack() as stack:
                pins = []
                root = _root(supply.state_root, 0, stack, pins, stage=True)
                claim = _open_directory(root, prepared.plan.sha256, 0, stack, pins, mode=0o700)
                # A response cannot precede durable publication evidence.
                try:
                    _once(claim, "receipt.json", receipt.model_dump(mode="json"))
                except BaseException:  # noqa: BLE001 - publication without durable receipt is uncertain.
                    raise ProcessCleanupIncomplete("input_supply_cleanup_incomplete") from None
                for pin in pins:
                    pin.check()
            return PythonInputSupplyResponse(action=request.action,
                execution_id=request.invocation.execution_id, ok=True, receipt=receipt)


def main() -> int:
    request = None
    try:
        if (sys.platform != "linux" or os.geteuid() != 0 or os.getegid() != 0
                or not sys.flags.isolated or not sys.dont_write_bytecode or sys.flags.optimize):
            raise ValueError
        os.umask(0o077)
        parser = argparse.ArgumentParser(allow_abbrev=False)
        parser.add_argument("--stage", type=Path, required=True)
        args = parser.parse_args()
        from agent_hub.skills.sandbox.broker import load_broker_policy
        from agent_hub.skills.sandbox.python_input_supply_catalog import (
            InputSupplyError,
            read_supply_configuration,
        )
        from agent_hub.skills.sandbox.python_project_backend import read_root_configuration

        configuration = read_root_configuration()
        if args.stage.parent != configuration.stage_root:
            raise ValueError
        with ExitStack() as stack:
            pins: list[_Observed] = []
            parent = _root(args.stage, 0, stack, pins, stage=True)
            request = PythonInputSupplyRequest.model_validate_json(
                _read_control(parent, "request.json", 0, pins), strict=True)
            for pin in pins:
                pin.check()
        response = execute(request, load_broker_policy(), configuration, read_supply_configuration(), args.stage)
        response.require_for(request)
        print(response.model_dump_json(), flush=True)
        return 0
    except ProcessCleanupIncomplete:
        return 3
    except Exception as error:  # noqa: BLE001 - privileged boundaries never emit paths, bytes or tracebacks.
        if request is not None:
            from agent_hub.skills.sandbox.python_input_supply_catalog import InputSupplyError

            response = PythonInputSupplyResponse(action=request.action,
                execution_id=request.invocation.execution_id, ok=False,
                error=error.code if isinstance(error, InputSupplyError) else "input_supply_unavailable")
            print(response.model_dump_json(), flush=True)
            return 0
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
