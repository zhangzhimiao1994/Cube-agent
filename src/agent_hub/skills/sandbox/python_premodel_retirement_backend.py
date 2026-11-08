"""Root-only observations for stopped pre-model retirement, never project replay."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from agent_hub.skills.sandbox.project import ProjectExecutionBroker, ProjectInvocation, _Execution
from agent_hub.skills.sandbox.project_contract_broker import _object
from agent_hub.skills.sandbox.python_project_backend import (
    _MAX_CONTROL,
    _check,
    _release_stage_invocation,
)
from agent_hub.skills.sandbox.python_project_protocol import PythonFailedMaintenanceInvocation

if TYPE_CHECKING:
    from typing import Literal

    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.project_contract_broker import _Observed
    from agent_hub.skills.sandbox.python_premodel_retirement import (
        PythonStoppedPremodelMaintenanceRequest,
        PythonStoppedPremodelMaintenanceResponse,
        PythonStoppedPremodelStage,
    )
    from agent_hub.skills.sandbox.python_project_backend import RootConfig

_PROC_ROOT = Path("/proc")


def parse_stopped_stage(
    raw: bytes, name: str, owner: PythonFailedMaintenanceInvocation,
) -> ProjectInvocation | None:
    """Bind an original control marker; its state is not a quiescence proof.

    Root must separately hold admission, pin the directory/control FDs, and prove
    both units quiet. The original bytes are retained without normalization.
    """
    if (type(raw) is not bytes or not 0 < len(raw) <= _MAX_CONTROL
            or type(name) is not str
            or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}-[a-z0-9_]{1,64}", name) is None):
        raise ValueError("invalid stopped pre-model control")
    owner = PythonFailedMaintenanceInvocation.model_validate(owner.model_dump(), strict=True)
    payload = json.loads(raw, object_pairs_hook=_object)
    invocation = _release_stage_invocation(payload, name)
    if any(getattr(invocation, key) != getattr(owner, key)
           for key in ("tenant_id", "run_id", "project_id", "session_id")):
        return None
    return invocation


async def check_stopped_units(
    broker: ProjectExecutionBroker, invocations: tuple[ProjectInvocation, ...], deadline: float,
) -> None:
    """Observe both native units, including pending jobs; never stop or start one."""
    from agent_hub.skills.sandbox.python_project_broker import PythonRootExecution, _admit_unit

    if not 0 < len(invocations) <= 65:
        raise ValueError("invalid stopped pre-model executions")
    seen: set[str] = set()
    for value in invocations:
        _check(deadline)
        invocation = ProjectInvocation.model_validate(value.model_dump(), strict=True)
        if invocation.execution_id in seen:
            continue
        seen.add(invocation.execution_id)
        await _admit_unit(broker, PythonRootExecution(invocation, None), no_job=True)
        _check(deadline)
        await _admit_unit(broker, _Execution(invocation, None), no_job=True)
        _check(deadline)


def consumer_path_matches(path: str, targets: tuple[str, ...]) -> bool:
    """Match kernel path spellings without confusing adjacent cache keys."""
    decoded = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), path)
    decoded = decoded.removesuffix(" (deleted)")
    return any(decoded == target or decoded.startswith(target + "/") for target in targets)


def _process_bytes(path: Path, limit: int, deadline: float) -> bytes:
    _check(deadline)
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("unbounded stopped pre-model process metadata")
    _check(deadline)
    return value


def require_no_native_consumers(targets: tuple[Path, ...], deadline: float) -> None:
    """Read-only host process/FD/mount fence; never emits their private contents.

    Unit quiet checks alone do not prove an orphan outside its original cgroup
    stopped using a retained stage. The caller separately pins every target and
    holds native admission. The current inspector necessarily holds those FDs.
    """
    if sys.platform != "linux" or os.geteuid() != 0 or not targets:
        raise ValueError("native stopped pre-model consumer fence required")
    prefixes = tuple(str(path) for path in targets)
    if any(not path.is_absolute() or ".." in path.parts for path in targets):
        raise ValueError("invalid stopped pre-model consumer target")
    _scan_process_consumers(prefixes, deadline)


def _scan_process_consumers(prefixes: tuple[str, ...], deadline: float) -> None:
    current_pid = os.getpid()
    # The inspector pins the selected controls itself, so its own FDs are
    # exempt. Its mount namespace is not exempt: a private bind/overlay can
    # outlive the original unit and need not appear in any other process.
    _require_no_target_mounts(_PROC_ROOT / str(current_pid) / "mountinfo", prefixes, deadline)
    total_fds = 0
    process_count = 0
    with os.scandir(_PROC_ROOT) as entries:
        for entry in entries:
            _check(deadline)
            if not entry.name.isdigit() or int(entry.name) == current_pid:
                continue
            process_count += 1
            if process_count > 4096:
                raise ValueError("unbounded stopped pre-model process inventory")
            root = _PROC_ROOT / entry.name
            try:
                before = _process_bytes(root / "stat", 65536, deadline)
                # The command in parentheses can contain spaces or ')'. Only
                # the numeric starttime after the final ')' binds PID reuse.
                start = before.rsplit(b")", 1)[1].split()[19]
                for name in ("exe", "cwd", "root"):
                    _check(deadline)
                    try:
                        value = os.readlink(root / name)
                    except FileNotFoundError:
                        continue
                    if consumer_path_matches(value, prefixes):
                        raise ValueError("stopped pre-model environment has a process consumer")
                with os.scandir(root / "fd") as descriptors:
                    for descriptor in descriptors:
                        _check(deadline)
                        total_fds += 1
                        if total_fds > 100_000:
                            raise ValueError("unbounded stopped pre-model FD inventory")
                        try:
                            value = os.readlink(descriptor.path)
                        except FileNotFoundError:
                            continue
                        if consumer_path_matches(value, prefixes):
                            raise ValueError("stopped pre-model environment has an FD consumer")
                maps = _process_bytes(root / "maps", 8 * 1024**2, deadline)
                for line in maps.decode("utf-8", errors="strict").splitlines():
                    fields = line.split(None, 5)
                    if len(fields) == 6 and consumer_path_matches(fields[5], prefixes):
                        raise ValueError("stopped pre-model environment has a mapped consumer")
                _require_no_target_mounts(root / "mountinfo", prefixes, deadline)
                after = _process_bytes(root / "stat", 65536, deadline)
                if after.rsplit(b")", 1)[1].split()[19] != start:
                    raise ValueError("stopped pre-model process identity changed")
            except (FileNotFoundError, ProcessLookupError):
                try:
                    _process_bytes(root / "stat", 65536, deadline)
                except (FileNotFoundError, ProcessLookupError):
                    continue
                # Missing one metadata file is not proof that a live PID has
                # exited. Permission and every other uncertain case also veto.
                raise ValueError("stopped pre-model process metadata disappeared") from None
    _check(deadline)


def _require_no_target_mounts(
    path: Path, prefixes: tuple[str, ...], deadline: float,
) -> None:
    mounts = _process_bytes(path, 8 * 1024**2, deadline)
    for line in mounts.decode("utf-8", errors="strict").splitlines():
        # Include bind roots and overlay lowerdir options. Kernel whitespace
        # in paths is octal escaped and decoded by consumer_path_matches.
        for field in re.split(r"[ ,:=]", line):
            if consumer_path_matches(field, prefixes):
                raise ValueError("stopped pre-model environment has a mounted consumer")


def _selected_cache_path(
    owner: PythonFailedMaintenanceInvocation, origin: str, configuration: RootConfig,
    deadline: float,
) -> Path:
    """Root-only lookup while the cache caller already holds its native lock."""
    from agent_hub.skills.sandbox.project_contract_broker import _open_directory, _root
    from agent_hub.skills.sandbox.python_project_cache import (
        PythonProjectEnvironmentCache,
        _reservation,
    )

    cache = PythonProjectEnvironmentCache(configuration.environment_root, configuration.cache_policy)
    with ExitStack() as stack:
        pins: list[_Observed] = []
        parent = _root(configuration.environment_root, 0, stack, pins, stage=True)
        selected = []
        for key in sorted(cache._catalog(parent, deadline)):
            _check(deadline)
            entry = _open_directory(parent, key, 0, stack, pins, mode=0o700)
            reservation = _reservation(entry, key, configuration.cache_policy, pins)
            if (reservation["origin_execution_id"] == origin
                    and all(reservation["scope"][name] == str(getattr(owner, name))
                            for name in ("tenant_id", "run_id", "project_id", "session_id"))):
                selected.append(configuration.environment_root / key)
        if len(selected) != 1:
            raise ValueError("ambiguous stopped pre-model environment")
        for pin in pins:
            pin.check()
        return selected[0]


def capture_stopped_stages(
    owner: PythonFailedMaintenanceInvocation, origin: str, policy: BrokerPolicy,
    configuration: RootConfig, stage: Path, deadline: float,
) -> tuple[PythonStoppedPremodelStage, ...]:
    """Under native admission, bind retained controls and all physical consumers.

    No directory or marker is changed. This explicit observation does not alter
    normal retained-stage refusal and can never authorize project execution.
    """
    from agent_hub.skills.sandbox.project_contract_broker import (
        _Observed,
        _open_directory,
        _root,
        _stamp,
    )
    from agent_hub.skills.sandbox.python_premodel_retirement import PythonStoppedPremodelStage
    from agent_hub.skills.sandbox.python_project_protocol import base_python_invocation

    if (sys.platform != "linux" or os.geteuid() != 0 or stage.parent != configuration.stage_root
            or origin == owner.execution_id):
        raise ValueError("invalid stopped pre-model Root observation")
    own_seen = False
    records: list[PythonStoppedPremodelStage] = []
    consumers = []
    invocations = [base_python_invocation(owner).model_copy(update={"execution_id": origin})]
    seen_roots: set[Path] = set()
    with ExitStack() as stack:
        pins: list[_Observed] = []
        group: Literal["stage", "runtime"]
        for group, root in (("stage", configuration.stage_root), ("runtime", policy.project_runtime_root)):
            _check(deadline)
            if root in seen_roots:
                continue
            seen_roots.add(root)
            if not root.exists():
                continue
            parent = _root(root, 0, stack, pins, stage=True)
            with os.scandir(parent) as entries:
                names: list[str] = []
                for entry in entries:
                    _check(deadline)
                    if len(names) >= 4096:
                        raise ValueError("unbounded stopped pre-model stage inventory")
                    if entry.name != "active.lock":
                        names.append(entry.name)
            for name in sorted(names):
                _check(deadline)
                directory = _open_directory(parent, name, 0, stack, pins, mode=0o700, full=True)
                descriptor = os.open("execution.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                                     | os.O_CLOEXEC, dir_fd=directory)
                stack.callback(os.close, descriptor)
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                        or stat.S_IMODE(info.st_mode) not in (0o600, 0o644)
                        or not 0 < info.st_size <= 16384):
                    raise ValueError("invalid stopped pre-model stage control")
                pin = _Observed(directory, "execution.json", _stamp(info, full=True), True, descriptor)
                pin.check()
                pins.append(pin)
                raw = os.read(descriptor, 16385)
                if len(raw) != info.st_size or os.read(descriptor, 1):
                    raise ValueError("changed stopped pre-model stage control")
                payload = json.loads(raw, object_pairs_hook=_object)
                invocation = _release_stage_invocation(payload, name)
                if root == stage.parent and name == stage.name:
                    if (own_seen or invocation.execution_id != owner.execution_id
                            or parse_stopped_stage(raw, name, owner) is None
                            or payload["state"] != "RUNNING"):
                        raise ValueError("invalid own stopped pre-model maintenance stage")
                    own_seen = True
                elif invocation.execution_id == owner.execution_id:
                    raise ValueError("duplicate stopped pre-model maintenance execution")
                elif (invocation.tenant_id, invocation.run_id) == (owner.tenant_id, owner.run_id):
                    if parse_stopped_stage(raw, name, owner) is None:
                        raise ValueError("conflicting stopped pre-model stage scope")
                    if len(records) >= 64:
                        raise ValueError("unbounded retained pre-model stages")
                    records.append(PythonStoppedPremodelStage(
                        root=group, name=name, record=raw.decode("ascii"),
                        record_sha256=hashlib.sha256(raw).hexdigest(),
                        identities=(_stamp(os.fstat(directory), full=True), _stamp(info, full=True)),
                    ))
                    consumers.append(root / name)
                    invocations.append(invocation)
                pin.check()
        if not own_seen:
            raise ValueError("missing own stopped pre-model maintenance stage")
        broker = ProjectExecutionBroker(policy)
        asyncio.run(check_stopped_units(broker, tuple(invocations), deadline))
        target = _selected_cache_path(owner, origin, configuration, deadline)
        require_no_native_consumers((target, *consumers), deadline)
        # Keep unit observations on both sides of the slower process inventory.
        asyncio.run(check_stopped_units(broker, tuple(invocations), deadline))
        for pin in pins:
            pin.check()
        return tuple(sorted(records, key=lambda item: (item.root, item.name)))


def maintain_stopped_premodel(
    request: PythonStoppedPremodelMaintenanceRequest, policy: BrokerPolicy,
    configuration: RootConfig, deadline: float, *, stage: Path,
) -> PythonStoppedPremodelMaintenanceResponse:
    """Trusted Root worker entry; caller already owns native admission."""
    from agent_hub.skills.sandbox.python_premodel_retirement import (
        PythonStoppedPremodelMaintenanceResponse,
        PythonStoppedPremodelSnapshot,
    )
    from agent_hub.skills.sandbox.python_project_cache import PythonProjectEnvironmentCache

    def quiet(origin: str) -> tuple[PythonStoppedPremodelStage, ...]:
        return capture_stopped_stages(request.invocation, origin, policy, configuration, stage, deadline)

    value = PythonProjectEnvironmentCache(
        configuration.environment_root, configuration.cache_policy,
    ).maintain_stopped_premodel(request.invocation, deadline=deadline, quiescence_check=quiet,
                              expected=request.snapshot)
    if request.phase == "inspect":
        if value is not None and type(value) is not PythonStoppedPremodelSnapshot:
            raise ValueError("invalid stopped pre-model inspect result")
        return PythonStoppedPremodelMaintenanceResponse(action=request.action,
            execution_id=request.invocation.execution_id, ok=True, retirement_phase="inspect",
            retirement_snapshot=value)
    if type(value) is not bool:
        raise ValueError("invalid stopped pre-model commit result")
    return PythonStoppedPremodelMaintenanceResponse(action=request.action,
        execution_id=request.invocation.execution_id, ok=True, retirement_phase="commit",
        retirement_removed=value)
