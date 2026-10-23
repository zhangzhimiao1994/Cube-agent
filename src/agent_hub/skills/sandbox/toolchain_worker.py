"""Offline Root v7 supply helper; only a separate DynamicUser unit downloads.

Authenticated broker/service approval is external to this module. Root derives
the selection from its catalog and immutable captured controls, records a once
claim before supply, and retains every uncertain attempt. No SDK/project code
is executed. Stored receipt data always needs a fresh SDK observation.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from agent_hub.capabilities.go_project_recipe import (
    GoProjectDeclaration,
    parse_go_project_declaration,
)
from agent_hub.capabilities.project_baselines import ProjectBaselineReference, StoredProjectBaseline
from agent_hub.capabilities.project_stack_registry import detect_stacks
from agent_hub.capabilities.scoped_read import _read_no_links
from agent_hub.skills.sandbox.base import run_subprocess_with_limits
from agent_hub.skills.sandbox.project import ProjectExecutionBroker, _Execution, _Limits
from agent_hub.skills.sandbox.project_contract_broker import (
    _encoded,
    _Observed,
    _open_directory,
    _read_control,
    _root,
    _write_all,
    stage_original_contract,
)
from agent_hub.skills.sandbox.python_project_command import _escaped
from agent_hub.skills.sandbox.toolchain_catalog import (
    FilesystemSpace,
    ToolchainArtifact,
    ToolchainCatalog,
    ToolchainSupplyPlan,
    plan_toolchain_supply,
    source_copy_peak,
)
from agent_hub.skills.sandbox.toolchain_protocol import (
    ToolchainError,
    ToolchainInvocation,
    ToolchainReference,
    ToolchainRequest,
    ToolchainResponse,
    ToolchainSupplyReceipt,
    ToolchainSupplyStatus,
    base_toolchain_invocation,
    parse_toolchain_request,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.toolchain_store import PublishedToolchain


class ToolchainWorkerError(ValueError):
    def __init__(self, code: ToolchainError) -> None:
        self.code = code
        super().__init__(code)


def _need(value: object, code: ToolchainError = "toolchain_request_invalid") -> None:
    if not value:
        raise ToolchainWorkerError(code)


def _check(deadline: float) -> None:
    _need(time.monotonic() < deadline, "toolchain_timeout")


def require_source_copy_capacity(
    catalog: ToolchainCatalog,
    invocation: ToolchainInvocation,
    spaces: tuple[FilesystemSpace, ...],
) -> None:
    stage = next(space for space in spaces if space.role == "stage")
    required = source_copy_peak(
        invocation.source_limit_bytes, 10 * invocation.file_limit, stage.allocation_unit
    )
    _need(stage.available_bytes - required >= catalog.min_free_bytes, "toolchain_capacity")


def validate_toolchain_configuration(policy: BrokerPolicy, catalog: ToolchainCatalog) -> None:
    from agent_hub.skills.sandbox.broker import BrokerPolicy

    _need(type(policy) is BrokerPolicy and type(catalog) is ToolchainCatalog)
    ToolchainCatalog.from_json(catalog.to_json())
    _need(
        Path(catalog.stage_root) == policy.toolchain_runtime_root,
        "toolchain_configuration_untrusted",
    )
    roots = tuple(Path(p) for p in (catalog.stage_root, catalog.runtime_root, catalog.state_root))
    protected = (
        policy.source_path,
        policy.project_workspace_root,
        policy.skill_root,
        policy.runtime_root,
        policy.project_runtime_root,
        policy.python_project_runtime_root,
    )
    _need(
        all(not a.is_relative_to(b) and not b.is_relative_to(a) for a in roots for b in protected),
        "toolchain_configuration_untrusted",
    )
    _need(
        policy.systemd_run == "/usr/bin/systemd-run" and policy.systemctl == "/usr/bin/systemctl",
        "toolchain_unsupported",
    )


def toolchain_declaration(stored: StoredProjectBaseline) -> GoProjectDeclaration:
    _need(type(stored) is StoredProjectBaseline and stored.generated_metadata is None)
    _need(detect_stacks(tuple(stored.source_files)) == ("go",), "toolchain_unsupported")
    controls: dict[str, bytes] = {}
    remaining = 65536
    for name in ("go.mod", "go.sum"):
        if name not in stored.source_files:
            continue
        _need(name in stored.contract.protected_files, "toolchain_scope_changed")
        raw = _read_no_links(stored.snapshot, (name,), max_bytes=remaining)
        remaining -= len(raw)
        _need(remaining >= 0)
        expected = stored.source_files[name]
        _need(
            expected == stored.contract.protected_files[name]
            and expected in {hashlib.sha256(tag + raw).hexdigest() for tag in (b"f\0", b"x\0")},
            "toolchain_scope_changed",
        )
        controls[name] = raw
    # This selection validates controls, not source imports or project readiness.
    # The Go runner must independently validate the complete admitted source.
    return parse_go_project_declaration(controls)


def toolchain_reference(published: PublishedToolchain) -> ToolchainReference:
    p = published.observation
    return ToolchainReference(
        artifact_sha256=published.artifact_sha256,
        archive_sha256=p.archive_sha256,
        tree_sha256=p.tree_sha256,
        binary_sha256=p.binary_sha256,
        gofmt_sha256=p.gofmt_sha256,
        total_bytes=p.total_bytes,
        file_count=p.file_count,
        version=p.version,
        platform=cast(Literal["linux_amd64"], p.platform),
    )


def available_toolchain(
    root: Path, artifact: ToolchainArtifact, deadline: float
) -> PublishedToolchain | None:
    from agent_hub.skills.sandbox.toolchain_store import (
        ToolchainStoreError,
        observe_published_toolchain,
    )

    try:
        return observe_published_toolchain(root, artifact=artifact, deadline=deadline)
    except ToolchainStoreError as error:
        if error.code == "toolchain_store_missing":
            return None
        raise


def build_toolchain_fetch_command(
    request: ToolchainRequest,
    policy: BrokerPolicy,
    catalog: ToolchainCatalog,
) -> tuple[str, ...]:
    _need(request.plan is not None)
    inv = request.invocation
    hidden = (Path("/var"), Path("/run"), Path("/tmp"))
    masked = tuple(
        p
        for p in dict.fromkeys(
            (
                Path("/etc/agent-hub"),
                policy.project_workspace_root,
                Path(catalog.runtime_root),
                Path(catalog.state_root),
                Path(catalog.stage_root),
            )
        )
        if not any(p.is_relative_to(r) for r in hidden)
    )
    properties = (
        "DynamicUser=yes",
        "SupplementaryGroups=" + policy.service_group,
        "UMask=0077",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=no",
        "PrivateDevices=yes",
        "ProtectProc=invisible",
        "ProcSubset=pid",
        (
            "TemporaryFileSystem=/run:rw,nosuid,nodev,noexec,size=1048576,mode=0755 "
            "/var:ro,nosuid,nodev,noexec,size=1048576 /tmp:rw,nosuid,nodev,noexec,size=8388608"
        ),
        "BindReadOnlyPaths=/etc/resolv.conf",
        "CapabilityBoundingSet=",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "InaccessiblePaths=" + " ".join(_escaped(p) for p in masked),
        "ProtectKernelModules=yes",
        "ProtectKernelTunables=yes",
        "ProtectControlGroups=yes",
        "RestrictSUIDSGID=yes",
        "RestrictRealtime=yes",
        "LockPersonality=yes",
        "RemoveIPC=yes",
        "KillMode=control-group",
        "TimeoutStopSec=2s",
        "TasksMax=32",
        "LimitFSIZE=0",
        f"MemoryMax={inv.memory_limit_bytes}",
        f"CPUQuota={inv.cpu_quota_percent}%",
        f"RuntimeMaxSec={min(inv.timeout_seconds, 600) + 2}s",
    )
    command = [
        policy.systemd_run,
        "--wait",
        "--collect",
        "--pipe",
        "--quiet",
        "--unit",
        "agent-hub-project-" + inv.execution_id,
    ]
    for prop in properties:
        command.extend(("-p", prop))
    command.extend(
        (
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "LANG=C.UTF-8",
            Path(policy.python).as_posix().replace("%", "%%"),
            "-I",
            "-B",
            "-m",
            "agent_hub.skills.sandbox.toolchain_fetch",
        )
    )
    return tuple(command)


async def _fetch(
    request: ToolchainRequest, policy: BrokerPolicy, catalog: ToolchainCatalog, deadline: float
) -> bytes:
    assert request.plan is not None
    artifact = request.plan.artifact
    _need(
        6 * artifact.archive_bytes + 64 * 1024**2 <= request.invocation.memory_limit_bytes,
        "toolchain_capacity",
    )
    remaining = min(600.0, deadline - time.monotonic())
    _need(remaining > 0, "toolchain_timeout")
    job = _Execution(base_toolchain_invocation(request.invocation), None)

    async def status() -> bool:
        report = await run_subprocess_with_limits(
            (
                policy.systemctl,
                "show",
                job.unit,
                "--no-pager",
                "--property=LoadState,ActiveState,SubState,MainPID,ControlPID,ControlGroup,TasksCurrent",
            ),
            _Limits(2, 8192),
            stdin_payload=b"",
        )
        return ProjectExecutionBroker._unit_quiet(job, report)

    async def stop() -> None:
        try:
            async with asyncio.timeout(5):
                await run_subprocess_with_limits(
                    (policy.systemctl, "kill", "--kill-who=all", "--signal=SIGKILL", job.unit),
                    _Limits(1, 8192),
                    stdin_payload=b"",
                )
                for _ in range(15):
                    if await status():
                        return
                    await asyncio.sleep(0.1)
        except (TimeoutError, OSError, ValueError):
            pass
        raise ProcessCleanupIncomplete("toolchain_cleanup_incomplete")

    _need(await status(), "toolchain_unknown")
    try:
        result = await run_subprocess_with_limits(
            build_toolchain_fetch_command(request, policy, catalog),
            _Limits(remaining + 2, 4 * ((artifact.archive_bytes + 2) // 3) + 1),
            stdin_payload=_encoded(
                {"artifact": artifact.to_payload(), "timeout_seconds": remaining}
            ),
            on_forced_terminate=stop,
        )
    finally:
        await stop()
    _need(result.exit_code == 0 and not result.timed_out, "toolchain_unavailable")
    try:
        data = base64.b64decode(result.stdout, validate=True)
    except (ValueError, UnicodeError):
        raise ToolchainWorkerError("toolchain_artifact_integrity") from None
    _need(
        len(data) == artifact.archive_bytes and hashlib.sha256(data).hexdigest() == artifact.sha256,
        "toolchain_artifact_integrity",
    )
    return data


def _once(parent: int, name: str, value: object) -> None:
    if sys.platform != "linux":
        raise ToolchainWorkerError("toolchain_unsupported")
    raw = _encoded(value)
    _need(0 < len(raw) <= 1024**2)
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=parent,
    )
    try:
        _write_all(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(parent)


@contextmanager
def _claim_root(
    catalog: ToolchainCatalog, plan: ToolchainSupplyPlan, *, create: bool = False
) -> Iterator[int]:
    _need(sys.platform == "linux", "toolchain_unsupported")
    with ExitStack() as stack:
        pins: list[_Observed] = []
        root = _root(Path(catalog.state_root), 0, stack, pins, stage=True)
        if create:
            try:
                os.mkdir(plan.sha256, 0o700, dir_fd=root)
            except FileExistsError:
                raise ToolchainWorkerError("toolchain_consumed") from None
            os.fsync(root)
        try:
            claim = _open_directory(root, plan.sha256, 0, stack, pins, mode=0o700)
        except FileNotFoundError:
            raise _ClaimMissing from None
        yield claim
        for pin in pins:
            pin.check()


class _ClaimMissing(FileNotFoundError):
    """Only a missing final claim, never an incomplete claim or missing root."""


def _receipt_status(request: ToolchainRequest, catalog: ToolchainCatalog) -> ToolchainSupplyStatus:
    from agent_hub.skills.sandbox.toolchain_store import observe_published_toolchain

    assert request.plan is not None and request.supply_execution_id is not None
    p = request.plan
    state = "unknown"
    receipt = None
    try:
        with _claim_root(catalog, p) as fd:
            observed: list[_Observed] = []
            intent = json.loads(_read_control(fd, "intent.json", 0, observed))
            _need(
                type(intent) is dict
                and set(intent) == {"schema_version", "supply_execution_id", "plan"},
                "toolchain_scope_changed",
            )
            saved = ToolchainSupplyPlan.from_payload(intent["plan"])
            _need(
                type(intent["schema_version"]) is int
                and intent["schema_version"] == 1
                and intent["supply_execution_id"] == request.supply_execution_id
                and saved.sha256 == p.sha256,
                "toolchain_scope_changed",
            )
            if "receipt.json" in os.listdir(fd):
                raw = _read_control(fd, "receipt.json", 0, observed)
                receipt = ToolchainSupplyReceipt.model_validate_json(raw, strict=True)
                _need(
                    receipt.plan.sha256 == p.sha256
                    and receipt.supply_execution_id == request.supply_execution_id,
                    "toolchain_scope_changed",
                )
                live = observe_published_toolchain(
                    Path(catalog.runtime_root),
                    artifact=p.artifact,
                    deadline=time.monotonic() + request.invocation.timeout_seconds,
                )
                _need(toolchain_reference(live) == receipt.environment, "toolchain_plan_changed")
                state = "supplied"
            for pin in observed:
                pin.check()
    except _ClaimMissing:
        state, receipt = "absent", None
    except FileNotFoundError:
        state, receipt = "unknown", None
    return ToolchainSupplyStatus(
        state=cast("LiteralStatus", state),
        plan_sha256=p.sha256,
        supply_execution_id=request.supply_execution_id,
        receipt=receipt,
    )


type LiteralStatus = Literal["absent", "in_progress", "supplied", "unknown"]


def execute_toolchain_request(
    request: ToolchainRequest,
    policy: BrokerPolicy,
    catalog: ToolchainCatalog,
    stage: Path,
    *,
    configuration_path: Path = Path("/etc/agent-hub/toolchains.json"),
) -> ToolchainResponse:
    from agent_hub.skills.sandbox.python_project_backend import _admission
    from agent_hub.skills.sandbox.toolchain_configuration import (
        measure_toolchain_spaces,
        read_toolchain_configuration,
    )
    from agent_hub.skills.sandbox.toolchain_stage import stage_go_toolchain
    from agent_hub.skills.sandbox.toolchain_store import (
        observe_published_toolchain,
        publish_staged_toolchain,
    )

    _need(sys.platform == "linux" and os.geteuid() == 0, "toolchain_unsupported")
    request = parse_toolchain_request(request.model_dump_json().encode())
    validate_toolchain_configuration(policy, catalog)
    _need(stage.parent == Path(catalog.stage_root))
    deadline = time.monotonic() + request.invocation.timeout_seconds
    invocation = request.invocation

    def response(**values: object) -> ToolchainResponse:
        return ToolchainResponse.model_validate(
            {
                "action": request.action,
                "execution_id": invocation.execution_id,
                "scope": invocation.scope,
                "ok": True,
                **values,
            },
            strict=True,
        )

    if request.plan is not None:
        _need(
            request.plan.catalog_sha256 == catalog.sha256
            and request.plan.artifact in catalog.artifacts
            and (request.plan.runtime_root, request.plan.stage_root, request.plan.state_root)
            == (catalog.runtime_root, catalog.stage_root, catalog.state_root),
            "toolchain_plan_changed",
        )
    if request.action == "inspect_project_toolchain_supply":
        return response(status=_receipt_status(request, catalog))
    if request.action == "inspect_project_toolchain":
        assert request.plan is not None
        profile = observe_published_toolchain(
            Path(catalog.runtime_root), artifact=request.plan.artifact, deadline=deadline
        )
        return response(environment=toolchain_reference(profile))
    with ExitStack() as stack:
        pins: list[_Observed] = []
        root = _root(policy.python_project_runtime_root, 0, stack, pins, stage=True)
        # Cooperating Python workers use this same native lock. It is held
        # through download, staging, publication and durable outcome recording.
        with _admission(root, deadline):
            current = read_toolchain_configuration(configuration_path, deadline=deadline)
            _need(current == catalog, "toolchain_plan_changed")
            require_source_copy_capacity(
                catalog,
                invocation,
                measure_toolchain_spaces(
                    catalog,
                    policy.project_workspace_root,
                    execution_owner_uid=policy.allowed_uid,
                    deadline=deadline,
                ),
            )
            original = stage_original_contract(
                policy.project_workspace_root,
                base_toolchain_invocation(invocation),
                ProjectBaselineReference.from_payload(invocation.original_reference.model_dump()),
                stage,
                policy.allowed_uid,
            )
            declaration = toolchain_declaration(original)
            spaces = measure_toolchain_spaces(
                catalog,
                policy.project_workspace_root,
                execution_owner_uid=policy.allowed_uid,
                deadline=deadline,
            )
            fresh = plan_toolchain_supply(
                catalog,
                declaration,
                invocation.scope,
                spaces,
                execution_bytes=invocation.workspace_limit_bytes + invocation.source_limit_bytes,
                source_limit_bytes=invocation.source_limit_bytes,
                source_entry_limit=10 * invocation.file_limit,
            )
            live = available_toolchain(Path(catalog.runtime_root), fresh.artifact, deadline)
            if request.action == "plan_project_toolchain":
                return response(
                    plan=fresh,
                    missing=live is None,
                    environment=None if live is None else toolchain_reference(live),
                )
            assert request.plan is not None
            _need(fresh.sha256 == request.plan.sha256, "toolchain_plan_changed")
            try:
                with _claim_root(catalog, request.plan, create=True) as claim:
                    _once(
                        claim,
                        "intent.json",
                        {
                            "schema_version": 1,
                            "supply_execution_id": invocation.execution_id,
                            "plan": request.plan.to_payload(),
                        },
                    )
                if live is None:
                    data = asyncio.run(_fetch(request, policy, catalog, deadline))
                    _check(deadline)
                    _need(
                        read_toolchain_configuration(configuration_path, deadline=deadline)
                        == catalog,
                        "toolchain_plan_changed",
                    )
                    # Fresh capacity checks remain independent of the approved
                    # snapshot. Publication never inherits an old free reading.
                    plan_toolchain_supply(
                        catalog,
                        declaration,
                        invocation.scope,
                        measure_toolchain_spaces(
                            catalog,
                            policy.project_workspace_root,
                            execution_owner_uid=policy.allowed_uid,
                            deadline=deadline,
                        ),
                        execution_bytes=fresh.execution_bytes,
                        source_limit_bytes=invocation.source_limit_bytes,
                        source_entry_limit=10 * invocation.file_limit,
                    )
                    sdk_stage = stage / "sdk"
                    sdk_stage.mkdir(mode=0o700)
                    stage_go_toolchain(
                        data, artifact=fresh.artifact, stage=sdk_stage, deadline=deadline
                    )
                    live = publish_staged_toolchain(
                        Path(catalog.runtime_root),
                        sdk_stage,
                        data,
                        artifact=fresh.artifact,
                        deadline=deadline,
                    )
                _check(deadline)
                environment = toolchain_reference(
                    observe_published_toolchain(
                        Path(catalog.runtime_root), artifact=fresh.artifact, deadline=deadline
                    )
                )
                receipt = ToolchainSupplyReceipt(
                    plan=request.plan,
                    supply_execution_id=invocation.execution_id,
                    environment=environment,
                    reused=live.reused,
                )
                with _claim_root(catalog, request.plan) as claim:
                    _once(claim, "receipt.json", receipt.model_dump(mode="json"))
                for pin in pins:
                    pin.check()
                return response(receipt=receipt)
            except ToolchainWorkerError as error:
                if error.code == "toolchain_consumed":
                    raise
                raise ProcessCleanupIncomplete("toolchain_cleanup_incomplete") from None
            except BaseException:  # noqa: BLE001 - uncertain claims must retain their stages.
                # A consumed claim remains inspect-only. The broker must retain
                # its stage until helper/fetch drain AND native outcome are known.
                raise ProcessCleanupIncomplete("toolchain_cleanup_incomplete") from None


def main() -> int:
    request = None
    try:
        _need(
            sys.platform == "linux"
            and os.geteuid() == 0
            and sys.flags.isolated
            and sys.dont_write_bytecode
            and not sys.flags.optimize,
            "toolchain_unsupported",
        )
        os.umask(0o077)
        parser = argparse.ArgumentParser(allow_abbrev=False)
        parser.add_argument("--stage", type=Path, required=True)
        args = parser.parse_args()
        from agent_hub.skills.sandbox.broker import load_broker_policy
        from agent_hub.skills.sandbox.toolchain_configuration import read_toolchain_configuration

        catalog = read_toolchain_configuration(deadline=time.monotonic() + 10)
        _need(args.stage.parent == Path(catalog.stage_root))
        with ExitStack() as stack:
            pins: list[_Observed] = []
            fd = _root(args.stage, 0, stack, pins, stage=True)
            request = parse_toolchain_request(_read_control(fd, "request.json", 0, pins))
            for pin in pins:
                pin.check()
        result = execute_toolchain_request(request, load_broker_policy(), catalog, args.stage)
        result.require_for(request)
        print(result.model_dump_json(), flush=True)
        return 0
    except ProcessCleanupIncomplete:
        return 3
    except Exception as error:  # noqa: BLE001 - no privileged source/path/traceback output.
        if request is not None:
            from agent_hub.skills.sandbox.toolchain_broker import toolchain_error

            code = toolchain_error(error)
            result = ToolchainResponse(
                action=request.action,
                execution_id=request.invocation.execution_id,
                scope=request.invocation.scope,
                ok=False,
                error=code,
            )
            print(result.model_dump_json(), flush=True)
            return 0
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
