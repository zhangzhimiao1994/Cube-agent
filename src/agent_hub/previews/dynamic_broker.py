"""Independent root Unix broker for owned, bounded systemd preview units.

Only administrator configuration selects paths and executables. A connection
owns its handle; disconnect, expiry and shutdown revoke and stop the whole cgroup.
Failed cleanup stays reserved for the broker reaper, never reported as success.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import os
import re
import secrets
import select
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, cast

from agent_hub.previews.cleanup import (
    BrokerCleanupObservation,
    PreviewIdentityV1,
    PreviewOwnerScope,
    ResourceObservation,
    tree_entry,
    utc_now,
)
from agent_hub.previews.dynamic_runner import (
    MAX_FRAME,
    READY_TIMEOUT,
    REQUEST_TIMEOUT,
    FrameStream,
    PreviewStartupFailure,
    ProbeFailure,
    json_object,
    preview_startup_phase,
    probe_phase,
    read_frame,
    validate_wire_http,
    write_frame,
)
from agent_hub.previews.dynamic_runtime import BROKER_SOCKET_PATH, decode_response
from agent_hub.previews.provenance import (
    DYNAMIC_SELECTION_POLICY,
    PreviewProvenanceV1,
    SnapshotManifestV1,
)

_STAGES = {"install", "build", "start", "probe"}
_HANDLE = re.compile(r"[0-9a-f]{32}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
_PLATFORM = sys.platform
_PRIVATE_DISK_MIB = 256
_PROCESS_MEMORY_MIB = 384
_CGROUP_ROOT = Path("/sys/fs/cgroup/system.slice")
_CLEANUP_CONTEXT = threading.local()


class _ResourcePresent(RuntimeError):
    pass


def _remaining(limit: float) -> float:
    deadline = cast(float | None, getattr(_CLEANUP_CONTEXT, "deadline", None))
    if deadline is None:
        return limit
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("preview aggregate cleanup budget exhausted")
    return min(limit, remaining)


def _fact(resource: str, result: str, *, fields: dict[str, str] | None = None) -> ResourceObservation:
    return ResourceObservation(resource, "broker", utc_now(), result, "observed",
                               fields["LoadState"] if fields else None,
                               fields["ActiveState"] if fields else None,
                               int(fields["MainPID"]) if fields and "MainPID" in fields else None,
                               True if fields else None)


def _collect(fact: ResourceObservation) -> None:
    facts = cast(list[ResourceObservation] | None, getattr(_CLEANUP_CONTEXT, "facts", None))
    if facts is not None:
        facts.append(fact)


def _failed_facts(resources: tuple[str, ...], error: Exception) -> None:
    facts = cast(list[ResourceObservation], _CLEANUP_CONTEXT.facts)
    observed = {fact.resource for fact in facts}
    code = "budget_exhausted" if isinstance(error, TimeoutError | subprocess.TimeoutExpired) else "observation_failed"
    facts.extend(ResourceObservation(resource, "broker", utc_now(), "unknown", code)
                 for resource in resources if resource not in observed)


@dataclass(frozen=True, slots=True)
class PreviewBrokerPolicy:
    workspace_root: Path
    allowed_uid: int
    runtime_root: Path = Path("/run/agent-hub-preview")
    trusted_source_root: Path = Path("/opt/agent-hub/current/src/agent_hub")
    node_root: Path = Path("/opt/agent-hub/node")
    max_source_bytes: int = 32 * 1024 * 1024
    max_files: int = 4096
    max_sessions: int = 4
    max_lifetime_seconds: int = 7200

    @property
    def stage_root(self) -> Path:
        return self.workspace_root / ".preview-staging"


def validate_broker_request(payload: dict[str, object], *, peer_uid: int,
                            policy: PreviewBrokerPolicy) -> dict[str, object]:
    if peer_uid != policy.allowed_uid:
        raise ValueError("preview caller uid is not authorized")
    if type(payload.get("version")) is not int or payload["version"] not in {1, 2}:
        raise ValueError("invalid preview protocol version")
    action = payload.get("action")
    fields = {
        "probe": {"version", "action"},
        "start": {"version", "action", "source_root", "preview_id", "lifetime_seconds"},
        "request": {"version", "action", "handle", "request"},
        "stop": {"version", "action", "handle"},
        "recover_stop": {"version", "action", "handle", "recovery_token"},
    }
    if payload["version"] == 2:
        fields["start"] |= {"scope"}
        fields["recover_stop"] |= {"identity", "identity_binding"}
        fields["source_provenance"] = {"version", "action", "handle"}
    if not isinstance(action, str) or action not in fields or set(payload) != fields[action]:
        raise ValueError("invalid preview action fields")
    if action == "start":
        source, preview_id, lifetime = (
            payload["source_root"], payload["preview_id"], payload["lifetime_seconds"],
        )
        if not isinstance(source, str) or not isinstance(preview_id, str) or not _ID.fullmatch(preview_id):
            raise ValueError("invalid source/preview identity")
        if type(lifetime) is not int or not 1 <= lifetime <= policy.max_lifetime_seconds:
            raise ValueError("invalid preview lifetime")
        _source_path(Path(source), policy)
        if payload["version"] == 2:
            if str(uuid.UUID(preview_id)) != preview_id:
                raise ValueError("invalid preview UUID")
            PreviewOwnerScope.from_wire(payload["scope"])
    elif action in {"request", "stop", "recover_stop", "source_provenance"}:
        handle = payload["handle"]
        if not isinstance(handle, str) or not _HANDLE.fullmatch(handle):
            raise ValueError("invalid owned handle")
        if action == "recover_stop":
            token = payload["recovery_token"]
            if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
                raise ValueError("invalid recovery ownership proof")
            if payload["version"] == 2:
                identity = PreviewIdentityV1.from_wire(payload["identity"])
                binding = payload["identity_binding"]
                if identity.kind != "dynamic" or identity.runtime_handle != handle:
                    raise ValueError("invalid recovery identity binding")
                if not isinstance(binding, str) or re.fullmatch(r"[0-9a-f]{64}", binding) is None:
                    raise ValueError("invalid recovery identity binding")
        if action == "request":
            request = payload["request"]
            if not isinstance(request, dict):
                raise ValueError("invalid application request")
            validate_wire_http(cast(dict[str, object], request))
    return payload


def _source_path(root: Path, policy: PreviewBrokerPolicy) -> None:
    staging = policy.stage_root.absolute()
    if not root.is_absolute() or root == staging or not root.is_relative_to(staging):
        raise ValueError("source must be beneath configured preview staging root")
    # Reject aliases and reparse points at every level, including staging parents.
    current = Path(root.anchor)
    for part in root.parts[1:]:
        if part in {".", ".."}:
            raise ValueError("source path alias")
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("source link/reparse point")
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("source path must be a directory")
    if root.resolve(strict=True) != root.absolute():
        raise ValueError("source path alias")


def _manifest(data: bytes) -> None:
    payload = json_object(data)
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        raise ValueError("Node preview requires scripts.start")  # noqa: TRY004
    scripts = cast(dict[str, object], scripts)
    if not isinstance(scripts.get("start"), str) or not cast(str, scripts["start"]).strip():
        raise ValueError("Node preview requires string scripts.start")
    if any(not isinstance(value, str) for value in scripts.values()):
        raise ValueError("invalid Node scripts")
    if payload.get("workspaces"):
        raise ValueError("local workspaces forbidden")
    for field_name in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        dependencies = payload.get(field_name, {})
        if not isinstance(dependencies, dict):
            raise ValueError("invalid dependency metadata")  # noqa: TRY004
        for name, value in cast(dict[str, object], dependencies).items():
            if not isinstance(value, str) or not name or re.search(
                r"(?:file:|link:|git|https?:|ssh:|[/\\])", value,
            ):
                raise ValueError("unsupported dependency source")


def _snapshot(root: Path, policy: PreviewBrokerPolicy, destination: Path | None = None,
              *, legacy: bool = False,
              file_manifest: dict[str, tuple[int, str]] | None = None) -> str:
    _source_path(root, policy)
    digest = hashlib.sha256()
    total = 0
    entries = 0
    manifest: bytes | None = None

    def record(relative: Path, info: os.stat_result, data: bytes | None) -> None:
        nonlocal total, entries, manifest
        entries += 1
        if len(relative.parts) > 64:
            raise ValueError("source directory depth exceeded")
        if entries > policy.max_files:
            raise ValueError("source file count exceeded")
        if info.st_uid not in {0, policy.allowed_uid}:
            raise ValueError("source owner is not authorized")
        if any(part in {"node_modules", ".npmrc", ".git", ".ssh", ".aws"}
               or part == ".env" or part.startswith(".env.") for part in relative.parts):
            raise ValueError("unprepared source or credential file")
        if legacy:
            digest.update(relative.as_posix().encode() + b"\0")
        else:
            digest.update(tree_entry(relative.as_posix(), directory=data is None,
                                     executable=bool(info.st_mode & 0o111), data=data or b""))
        if data is None:
            if legacy:
                digest.update(b"dir\0")
            if destination is not None:
                (destination / relative).mkdir(mode=0o755)
                (destination / relative).chmod(0o755)
        else:
            total += len(data)
            if total > policy.max_source_bytes:
                raise ValueError("source size exceeded")
            if file_manifest is not None:
                file_manifest[relative.as_posix()] = (len(data), hashlib.sha256(data).hexdigest())
            executable = bool(info.st_mode & 0o111)
            if legacy:
                digest.update(b"exec\0" if executable else b"file\0")
                digest.update(data + b"\0")
            if relative.as_posix() == "package.json":
                manifest = data
            if destination is not None:
                path = destination / relative
                with path.open("xb") as stream:
                    stream.write(data)
                path.chmod(0o555 if executable else 0o444)

    if sys.platform == "linux":
        # Anchor traversal with O_NOFOLLOW and dir_fd: a path replacement cannot
        # turn root's copy operation into a privileged arbitrary file read.
        nofollow = cast(int, getattr(os, "O_NOFOLLOW", 0))
        directory = cast(int, getattr(os, "O_DIRECTORY", 0))
        if not nofollow or not directory:
            raise RuntimeError("Linux no-follow file traversal unavailable")

        def walk(fd: int, relative: Path) -> None:
            for name in sorted(os.listdir(fd)):
                path = relative / name
                try:
                    child = os.open(name, os.O_RDONLY | nofollow | os.O_NONBLOCK, dir_fd=fd)
                except OSError as error:
                    raise ValueError("source links or unreadable entries forbidden") from error
                try:
                    info = os.fstat(child)
                    if stat.S_ISDIR(info.st_mode):
                        record(path, info, None)
                        walk(child, path)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        if info.st_size > policy.max_source_bytes - total:
                            raise ValueError("source size exceeded")
                        with os.fdopen(os.dup(child), "rb") as stream:
                            data = stream.read(policy.max_source_bytes - total + 1)
                        after = os.fstat(child)
                        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                            after.st_size, after.st_mtime_ns, after.st_ctime_ns,
                        ):
                            raise ValueError("source changed during preparation")
                        record(path, info, data)
                    else:
                        raise ValueError("source links/special files forbidden")
                finally:
                    os.close(child)

        fd = os.open(root.anchor, os.O_RDONLY | directory | nofollow)
        try:
            for part in root.parts[1:]:
                child = os.open(part, os.O_RDONLY | directory | nofollow, dir_fd=fd)
                os.close(fd)
                fd = child
            walk(fd, Path())
        finally:
            os.close(fd)
    else:
        # Inspection-only portability; broker execution is Linux-only.
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("source links/reparse points forbidden")
            if stat.S_ISDIR(info.st_mode):
                record(path.relative_to(root), info, None)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if info.st_size > policy.max_source_bytes - total:
                    raise ValueError("source size exceeded")
                with path.open("rb") as stream:
                    data = stream.read(policy.max_source_bytes - total + 1)
                after = path.lstat()
                if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
                ):
                    raise ValueError("source changed during preparation")
                record(path.relative_to(root), info, data)
            else:
                raise ValueError("source links/special files forbidden")
    if manifest is None:
        raise ValueError("package.json missing")
    _manifest(manifest)
    return digest.hexdigest()


def inspect_source(root: Path, policy: PreviewBrokerPolicy) -> str:
    return _snapshot(root, policy)


def _systemd_path(path: Path) -> str:
    value = path.as_posix()
    if not value.startswith("/") or any(char.isspace() or char in ":%\\\"" for char in value):
        raise ValueError("unsafe administrator systemd path")
    return value


def build_systemd_command(policy: PreviewBrokerPolicy, handle: str, stage: str,
                          owned: Path, lifetime_seconds: int) -> tuple[str, ...]:
    if not _HANDLE.fullmatch(handle) or stage not in _STAGES:
        raise ValueError("invalid internal unit identity")
    root, work, source = (_systemd_path(owned / name) for name in ("root", "work", "source"))
    trusted = _systemd_path(owned / "trusted")
    node = _systemd_path(policy.node_root)
    # Trusted npm caches full registry metadata, which can exceed an app file's cap.
    # Installation still shares the same bounded disk; generated code keeps its cap.
    file_limit = _PRIVATE_DISK_MIB * 1024 * 1024 if stage == "install" else 33554432
    # npm's written cache pages and its heap share the install cgroup's budget.
    memory_mib = _PROCESS_MEMORY_MIB + (_PRIVATE_DISK_MIB if stage == "install" else 0)
    properties = [
        "Slice=system.slice",
        "BindsTo=agent-hub-preview-broker.service", "After=agent-hub-preview-broker.service",
        f"RequiresMountsFor={work}",
        "DynamicUser=yes", "SupplementaryGroups=", "UMask=0077", "NoNewPrivileges=yes",
        "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes",
        "PrivateMounts=yes", "MountAPIVFS=yes", "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes", "ProtectControlGroups=yes", "ProtectProc=invisible",
        "ProcSubset=pid", "RestrictSUIDSGID=yes", "LockPersonality=yes",
        "CapabilityBoundingSet=", "AmbientCapabilities=", "RestrictRealtime=yes",
        "KillMode=control-group", "SendSIGKILL=yes", "TimeoutStopSec=3s",
        "TasksMax=64", f"MemoryMax={memory_mib}M", "MemorySwapMax=0", "CPUQuota=50%",
        f"LimitFSIZE={file_limit}", "LimitNOFILE=256", "LimitCORE=0",
        f"RuntimeMaxSec={min(lifetime_seconds, 130) if stage in {'install', 'build'} else lifetime_seconds}s",
        f"RootDirectory={root}",
        "InaccessiblePaths=-/home -/root -/var/lib/agent-hub -/run/agent-hub -/run/docker.sock -/etc/agent-hub -/opt/agent-hub -/usr/local",
        "BindReadOnlyPaths=/usr:/usr /bin:/bin /lib:/lib -/lib64:/lib64",
        f"BindReadOnlyPaths={trusted}/dynamic_runner.py:/preview/trusted/dynamic_runner.py",
        f"BindReadOnlyPaths={trusted}/harness/project_validation_sandbox.py:/preview/trusted/harness/project_validation_sandbox.py",
        f"BindReadOnlyPaths={node}:/preview/node",
        "TemporaryFileSystem=/tmp:rw,nosuid,nodev,noexec,size=16M,nr_inodes=4096 /var/tmp:rw,nosuid,nodev,noexec,size=16M,nr_inodes=4096 /run:rw,nosuid,nodev,noexec,size=4M,nr_inodes=1024",
        f"PrivateNetwork={'no' if stage == 'install' else 'yes'}",
        "RestrictAddressFamilies=AF_UNIX AF_INET",
    ]
    if stage == "install":
        properties.append("BindReadOnlyPaths=/etc/resolv.conf:/etc/resolv.conf /etc/hosts:/etc/hosts /etc/ssl/certs:/etc/ssl/certs")
    else:
        properties.append("IPAddressDeny=any")
        properties.append("IPAddressAllow=127.0.0.1/32")
    properties.append(f"BindPaths={work}:/preview/work")
    if stage != "probe":
        properties.append(f"BindReadOnlyPaths={source}:/preview/source")
        if stage == "start":
            properties.append(f"BindReadOnlyPaths={work}/app:/preview/app {work}/app:/preview/work/app")
    command = ["/usr/bin/systemd-run", "--quiet", "--wait", "--pipe", "--collect",
               "--unit", f"agent-hub-preview-{handle}-{stage}"]
    for prop in properties:
        command.extend(("-p", prop))
    command.extend(("--", "/usr/bin/python3", "-I", "/preview/trusted/dynamic_runner.py", stage))
    return tuple(command)


def build_storage_command(policy: PreviewBrokerPolicy, handle: str, owned: Path) -> tuple[str, ...]:
    if not _HANDLE.fullmatch(handle):
        raise ValueError("invalid internal storage identity")
    return (
        "/usr/bin/systemd-mount", "--quiet", "--collect", "--type=tmpfs",
        f"--options=size={_PRIVATE_DISK_MIB}M,nr_inodes=16384,nosuid,nodev,mode=0777",
        "--property=BindsTo=agent-hub-preview-broker.service",
        "--property=After=agent-hub-preview-broker.service", "--property=TimeoutSec=5s",
        "tmpfs", _systemd_path(owned / "work"),
    )


def _cleanup_unit_fields(result: subprocess.CompletedProcess[bytes],
                         required: set[str], unit: str) -> dict[str, str]:
    if result.returncode != 0:
        raise RuntimeError("preview cleanup unit query failed")
    try:
        lines = result.stdout.decode("ascii").removesuffix("\n").split("\n")
    except UnicodeError as error:
        raise RuntimeError("invalid preview cleanup unit observation") from error
    fields: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or key not in required or key in fields:
            raise RuntimeError("invalid preview cleanup unit observation")
        fields[key] = value
    if fields.keys() != required or fields["LoadState"] not in {"loaded", "not-found"}:
        raise RuntimeError("incomplete or unknown preview cleanup unit observation")
    if fields["Id"] != unit:
        raise RuntimeError("invalid resolved preview cleanup unit identity")
    if fields["ActiveState"] not in {
        "active", "reloading", "inactive", "failed", "activating", "deactivating",
        "maintenance", "refreshing",
    }:
        raise RuntimeError("unknown preview cleanup unit active state")
    if fields["LoadState"] == "not-found" and fields["ActiveState"] != "inactive":
        raise RuntimeError("contradictory missing preview unit observation")
    return fields


def _cleanup_cgroup_present(path: Path) -> bool:
    # Only absence of the leaf counts. Every parent must remain observable,
    # immutable and root-owned, so an alias cannot redirect a privileged read.
    for current in (*reversed(path.parents), path):
        try:
            info = current.lstat()
        except FileNotFoundError:
            if current == path:
                return False
            raise
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise RuntimeError("unsafe preview cleanup cgroup directory")
    return True


def _confirm_cgroup_empty(unit: str) -> Literal["absent", "empty"]:
    path = _CGROUP_ROOT / unit
    if not _cleanup_cgroup_present(path):
        return "absent"
    try:
        events = path / "cgroup.events"
        info = events.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise RuntimeError("unsafe preview cleanup cgroup events")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(events, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise RuntimeError("unsafe preview cleanup cgroup events")
            data = stream.read(4097)
        if len(data) > 4096:
            raise RuntimeError("oversized preview cleanup cgroup events")
        try:
            lines = data.decode("ascii").removesuffix("\n").split("\n")
        except UnicodeError as error:
            raise RuntimeError("invalid preview cleanup cgroup events") from error
        values: dict[str, str] = {}
        for line in lines:
            match = re.fullmatch(r"([a-z_]+) ([0-9]+)", line)
            if match is None or match[1] in values:
                raise RuntimeError("invalid preview cleanup cgroup events")
            values[match[1]] = match[2]
        if values.get("populated") != "0":
            if values.get("populated") == "1":
                raise _ResourcePresent("preview cgroup remains populated")
            raise RuntimeError("preview cgroup empty state not confirmed")
    except (OSError, RuntimeError):
        # cgroup removal can race the events read; re-observe the exact leaf
        # through trusted parents before treating unavailable evidence as absent.
        if not _cleanup_cgroup_present(path):
            return "absent"
        raise
    return "empty"


def _owned_storage_mounted(owned: Path) -> bool:
    work = owned / "work"
    parent: os.stat_result | None = None
    # This is the fixed owned tmpfs mountpoint, not a namespace-wide mount scan.
    # Inspect each ancestor without resolving aliases or suppressing OS errors.
    for current in (*reversed(owned.parents), owned, work):
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if current in {owned, work}:
                return False
            raise
        if (not stat.S_ISDIR(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise RuntimeError("unsafe preview cleanup storage directory")
        if current == work:
            assert parent is not None
            return info.st_dev != parent.st_dev or info.st_ino == parent.st_ino
        parent = info
    raise RuntimeError("preview storage observation incomplete")


@dataclass(slots=True)
class _Session:
    handle: str
    preview_id: str
    owner: object
    owned: Path
    expires_at: float
    digest: str = ""
    units: set[str] = field(default_factory=set)
    process: subprocess.Popen[bytes] | None = None
    mounted: bool = False
    mount_unit: str | None = None
    revoked: bool = False
    stopped: bool = False
    ready: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)
    identity: PreviewIdentityV1 | None = None
    observation: BrokerCleanupObservation | None = None
    owned_identity: tuple[int, int] | None = None
    provenance: PreviewProvenanceV1 | None = None


@dataclass(frozen=True, slots=True)
class _Completed:
    owner: object
    observation: BrokerCleanupObservation | None


def _pipe_read(process: subprocess.Popen[bytes], timeout: float,
               diagnostic: bytearray | None = None) -> dict[str, object]:
    assert process.stdout is not None
    stdout = process.stdout
    deadline = time.monotonic() + timeout
    stderr = process.stderr if diagnostic is not None else None

    def exact(size: int) -> bytes:
        nonlocal stderr
        data = bytearray()
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("preview runner IPC timeout")
            readable = select.select([stdout] if stderr is None else [stdout, stderr], [], [], remaining)[0]
            if not readable:
                raise TimeoutError("preview runner IPC timeout")
            if stderr is not None and stderr in readable:
                chunk = os.read(stderr.fileno(), 4096)
                if diagnostic is not None:
                    diagnostic.extend(chunk[:max(0, 4096 - len(diagnostic))])
                if not chunk:
                    stderr = None
            if stdout not in readable:
                continue
            chunk = os.read(stdout.fileno(), size - len(data))
            if not chunk:
                raise EOFError("preview runner exited")
            data.extend(chunk)
        return bytes(data)

    size = struct.unpack("!I", exact(4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ValueError("invalid runner frame size")
    return json_object(exact(size))


def _pipe_write(process: subprocess.Popen[bytes], payload: dict[str, object]) -> None:
    import io
    frame = io.BytesIO()
    write_frame(frame, payload)
    remaining = memoryview(frame.getvalue())
    assert process.stdin is not None
    deadline = time.monotonic() + REQUEST_TIMEOUT
    while remaining:
        timeout = deadline - time.monotonic()
        if timeout <= 0 or not select.select([], [process.stdin], [], timeout)[1]:
            raise TimeoutError("preview runner IPC write timeout")
        written = os.write(process.stdin.fileno(), remaining[:4096])
        remaining = remaining[written:]


class PreviewBroker:
    def __init__(self, policy: PreviewBrokerPolicy) -> None:
        self.policy = policy
        self._sessions: dict[str, _Session] = {}
        self._completed: OrderedDict[str, _Completed] = OrderedDict()
        self._recovery_key = secrets.token_bytes(32)
        self._lock = threading.RLock()

    def _initialize_runtime_root(self) -> None:
        root = self.policy.runtime_root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if sys.platform == "linux":
            _require_root_path(root)
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("unsafe preview runtime root")
        root.chmod(0o700)
        key_path = root / ".recovery-key"
        try:
            with key_path.open("xb") as stream:
                stream.write(self._recovery_key)
                stream.flush()
                os.fsync(stream.fileno())
            key_path.chmod(0o600)
        except FileExistsError:
            info = key_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or (
                sys.platform == "linux" and info.st_mode & 0o077
            ) or info.st_size != 32:
                raise RuntimeError("invalid private recovery key") from None
            self._recovery_key = key_path.read_bytes()

    def _recovery_token(self, handle: str) -> str:
        binding = f"preview-stop-v1\0{self.policy.allowed_uid}\0{self.policy.runtime_root}\0{handle}"
        return hmac.new(self._recovery_key, binding.encode(), hashlib.sha256).hexdigest()

    def _identity_binding(self, identity: PreviewIdentityV1) -> str:
        return hmac.new(self._recovery_key, identity.binding_bytes(
            self.policy.allowed_uid, str(self.policy.runtime_root)), hashlib.sha256).hexdigest()

    def _remember_stopped(self, handle: str, owner: object,
                          observation: BrokerCleanupObservation | None = None) -> None:
        with self._lock:
            self._completed[handle] = _Completed(owner, observation)
            self._completed.move_to_end(handle)
            while len(self._completed) > 128:
                self._completed.popitem(last=False)

    def _recover_stop(self, handle: str, token: str, owner: object) -> dict[str, object]:
        if not hmac.compare_digest(self._recovery_token(handle), token):
            raise ValueError("preview recovery ownership rejected")
        with self._lock:
            session = self._sessions.get(handle)
            completed = handle in self._completed
            if session is None and not completed:
                if len(self._sessions) >= self.policy.max_sessions:
                    raise RuntimeError("preview recovery capacity exceeded")
                session = _Session(handle, "recovery-" + handle, owner,
                                   self.policy.runtime_root / handle, 0, revoked=True, mounted=True)
                session.units = {f"agent-hub-preview-{handle}-{stage}.service" for stage in _STAGES}
                self._sessions[handle] = session
        if session is not None:
            self._stop(session)
            if not session.stopped:
                raise RuntimeError("preview cleanup retained for retry")
        return {"ok": True, "state": "stopped"}

    def _recover_observed(self, payload: dict[str, object], owner: object) -> dict[str, object]:
        handle = cast(str, payload["handle"])
        if not hmac.compare_digest(self._recovery_token(handle), cast(str, payload["recovery_token"])):
            raise ValueError("preview recovery ownership rejected")
        identity = PreviewIdentityV1.from_wire(payload["identity"])
        if not hmac.compare_digest(self._identity_binding(identity), cast(str, payload["identity_binding"])):
            raise ValueError("preview recovery identity binding rejected")
        with self._lock:
            completed = self._completed.get(handle)
            if completed is not None and completed.observation is not None:
                if completed.observation.identity != identity:
                    raise ValueError("preview recovery identity mismatch")
                return self._observation_reply(completed.observation)
            session = self._sessions.get(handle)
            if session is None:
                if len(self._sessions) >= self.policy.max_sessions:
                    raise RuntimeError("preview recovery capacity exceeded")
                session = _Session(handle, identity.preview_id, owner,
                                   self.policy.runtime_root / handle, 0, revoked=True, identity=identity)
                self._sessions[handle] = session
            elif session.identity is not None and session.identity != identity:
                raise ValueError("preview recovery identity mismatch")
            else:
                session.identity = identity
                session.preview_id = identity.preview_id
            # Retained recovery is reserved before any observation can fail.
        self._stop(session)
        assert session.observation is not None
        return self._observation_reply(session.observation)

    @staticmethod
    def _observation_reply(observation: BrokerCleanupObservation) -> dict[str, object]:
        return {"ok": observation.status == "confirmed", "state": "stopped"
                if observation.status == "confirmed" else "cleanup_pending",
                "observation": observation.to_wire()}

    @staticmethod
    def _command(argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(argv, env=_ENV, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=_remaining(10), check=False)

    def _reserve(self, preview_id: str, owner: object, lifetime: int) -> _Session:
        with self._lock:
            if len(self._sessions) >= self.policy.max_sessions:
                raise RuntimeError("preview broker capacity exceeded (includes retained cleanup)")
            if any(item.preview_id == preview_id for item in self._sessions.values()):
                raise ValueError("preview identity already reserved")
            handle = uuid.uuid4().hex
            owned = self.policy.runtime_root / handle
            owned.mkdir(mode=0o755)
            session = _Session(handle, preview_id, owner, owned, time.monotonic() + lifetime)
            info = owned.lstat()
            session.owned_identity = (info.st_dev, info.st_ino)
            self._sessions[handle] = session
            return session

    def _mount_unit_name(self, owned: Path) -> str:
        result = self._command(("/usr/bin/systemd-escape", "--path", "--suffix=mount",
                                str(owned / "work")))
        name = result.stdout.decode("ascii").strip()
        if result.returncode != 0 or not re.fullmatch(r"[A-Za-z0-9_\\.-]{1,240}\.mount", name):
            raise RuntimeError("invalid private mount unit identity")
        return name

    def _prepare_storage(self, session: _Session) -> None:
        session.mount_unit = self._mount_unit_name(session.owned)
        # Record a possible live mount before submitting, including a timed-out
        # PID1 job. Cleanup must query that exact unit before releasing its slot.
        session.mounted = True
        result = self._command(build_storage_command(self.policy, session.handle, session.owned))
        if result.returncode != 0 or not os.path.ismount(session.owned / "work"):
            raise RuntimeError("PID1 private disk unavailable or invisible to broker")
        marker = session.owned / "work/.broker-storage-probe"
        marker.write_bytes(b"preview-storage-v1")
        marker.chmod(0o444)

    def _prepare_trusted(self, session: _Session) -> None:
        # Release files may be 0640 root:agent-hub. Copy only these fixed trusted
        # scripts; generated units must never gain the production reader group.
        trusted = session.owned / "trusted"
        trusted.mkdir(mode=0o755)
        trusted.chmod(0o755)
        (trusted / "harness").mkdir(mode=0o755)
        (trusted / "harness").chmod(0o755)
        for source_name, target_name in (
            ("previews/dynamic_runner.py", "dynamic_runner.py"),
            ("harness/project_validation_sandbox.py", "harness/project_validation_sandbox.py"),
        ):
            source = self.policy.trusted_source_root / source_name
            _require_root_path(source)
            if not stat.S_ISREG(source.lstat().st_mode):
                raise ValueError("trusted preview script must be a regular file")
            with source.open("rb") as stream:
                content = stream.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError("trusted preview script size exceeded")
            target = trusted / target_name
            with target.open("xb") as stream:
                stream.write(content)
            target.chmod(0o444)

    def _stop_storage(self, session: _Session) -> tuple[ResourceObservation, ...]:
        unit = session.mount_unit or self._mount_unit_name(session.owned)
        self._observe_cleanup_unit(unit, service=False)
        stopped = self._command(("/usr/bin/systemctl", "stop", unit))
        fields = self._observe_cleanup_unit(unit, service=False)
        if fields["ActiveState"] != "inactive" or (
            fields["LoadState"] == "loaded" and stopped.returncode != 0
        ):
            raise RuntimeError("PID1 private disk cleanup not confirmed")
        unit_fact = _fact("mount_unit", "inactive", fields=fields)
        _collect(unit_fact)
        if _owned_storage_mounted(session.owned):
            _collect(_fact("work_path", "present"))
            raise RuntimeError("private disk remains mounted")
        try:
            (session.owned / "work").lstat()
        except FileNotFoundError:
            result = "absent"
        else:
            result = "not_mountpoint"
        path_fact = _fact("work_path", result)
        _collect(path_fact)
        session.mounted = False
        return (unit_fact, path_fact)

    def _launch(self, session: _Session, stage: str) -> dict[str, object]:
        unit = f"agent-hub-preview-{session.handle}-{stage}.service"
        session.units.add(unit)
        remaining = max(1, int(session.expires_at - time.monotonic()))
        command = build_systemd_command(self.policy, session.handle, stage, session.owned, remaining)
        process = subprocess.Popen(command, env=_ENV, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE if stage == "probe" else subprocess.DEVNULL,
                                   close_fds=True, bufsize=0)
        session.process = process
        if stage == "probe":
            diagnostic = bytearray()
            try:
                result = _pipe_read(process, min(remaining, 125), diagnostic)
            except (OSError, EOFError, ValueError) as error:
                reason = "timeout" if isinstance(error, TimeoutError) else "bootstrap_failed"
                if b"can't open file '/preview/trusted/dynamic_runner.py'" in diagnostic:
                    if b"[Errno 13]" in diagnostic:
                        reason = "permission_denied"
                    elif b"[Errno 2]" in diagnostic:
                        reason = "not_found"
                raise ProbeFailure("bootstrap", reason) from None
            if result.get("ok") is False:
                phase, reported_reason = result.get("phase"), result.get("reason")
                if isinstance(phase, str) and isinstance(reported_reason, str):
                    try:
                        failure = ProbeFailure(phase, reported_reason)
                    except ValueError:
                        failure = ProbeFailure("runner_protocol", "invalid_result")
                    raise failure
                raise ProbeFailure("runner_protocol", "invalid_result")
        else:
            result = _pipe_read(process, min(remaining, READY_TIMEOUT + 2 if stage == "start" else 125))
            if (set(result) == {"ok", "error", "phase", "reason"}
                    and result["ok"] is False and result["error"] == "preview startup failed"):
                try:
                    startup_failure = PreviewStartupFailure(
                        cast(str, result["phase"]), cast(str, result["reason"]),
                    )
                except ValueError:
                    startup_failure = PreviewStartupFailure(stage, "invalid_result")
                raise startup_failure
        expected = "ready" if stage == "start" else "probe" if stage == "probe" else "prepared"
        if result != {"ok": True, "state": expected}:
            if stage == "probe":
                raise ProbeFailure("runner_protocol", "invalid_result")
            raise PreviewStartupFailure(stage, "invalid_result")
        if stage != "start":
            if process.wait(timeout=5) != 0:
                if stage == "probe":
                    raise ProbeFailure("runner_exit", "nonzero_exit")
                raise PreviewStartupFailure(stage, "supervisor_exit")
            self._stop_unit(unit)
            session.units.remove(unit)
            self._close_process(process)
            session.process = None
        return result

    @staticmethod
    def _close_process(process: subprocess.Popen[bytes]) -> None:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=_remaining(5))

    def _observe_cleanup_unit(self, unit: str, *, service: bool) -> dict[str, str]:
        properties = "Id,LoadState,ActiveState"
        if service:
            properties += ",MainPID,ControlGroup"
        result = self._command(("/usr/bin/systemctl", "show", unit, "--no-pager",
                                "--property=" + properties))
        fields = _cleanup_unit_fields(result, set(properties.split(",")), unit)
        if not service:
            return fields
        if not re.fullmatch(r"0|[1-9][0-9]*", fields["MainPID"]):
            raise RuntimeError("invalid preview cleanup process observation")
        group = fields["ControlGroup"]
        if group not in {"", f"/system.slice/{unit}"}:
            raise RuntimeError("invalid owned systemd cgroup")
        if fields["LoadState"] == "not-found" and (fields["MainPID"] != "0" or group):
            raise RuntimeError("contradictory missing preview unit observation")
        return fields

    def _stop_unit(self, unit: str) -> tuple[ResourceObservation, ...]:
        identity = re.fullmatch(r"agent-hub-preview-([0-9a-f]{32})-([a-z]+)\.service", unit)
        if identity is None or identity[2] not in _STAGES:
            raise RuntimeError("invalid owned preview cleanup unit")
        # PID1 may resolve a well-shaped name as an alias. Verify the resolved
        # identity and cgroup before stopping, then require fresh stopped evidence.
        self._observe_cleanup_unit(unit, service=True)
        stopped = self._command(("/usr/bin/systemctl", "stop", unit))
        if stopped.returncode != 0:
            self._command(("/usr/bin/systemctl", "kill", "--kill-whom=all", "--signal=KILL", unit))
        fields = self._observe_cleanup_unit(unit, service=True)
        if fields["ActiveState"] not in {"inactive", "failed"}:
            _collect(_fact("unit_" + identity[2], "present"))
            raise RuntimeError("preview cgroup stop not confirmed")
        if fields["MainPID"] != "0":
            _collect(_fact("unit_" + identity[2], "present"))
            raise RuntimeError("preview process remains alive")
        if fields["LoadState"] == "loaded" and stopped.returncode != 0:
            raise RuntimeError("preview stop failed; retained for retry")
        unit_fact = _fact("unit_" + identity[2], fields["ActiveState"], fields=fields)
        _collect(unit_fact)
        try:
            group_fact = _fact("cgroup_" + identity[2], _confirm_cgroup_empty(unit))
        except _ResourcePresent:
            _collect(_fact("cgroup_" + identity[2], "present"))
            raise
        _collect(group_fact)
        return (unit_fact, group_fact)

    def _stop(self, session: _Session) -> None:
        session.revoked = True
        if session.stopped:
            return
        previous = getattr(_CLEANUP_CONTEXT, "deadline", None)
        _CLEANUP_CONTEXT.deadline = previous or time.monotonic() + 60
        acquired = False
        try:
            acquired = session.lock.acquire(timeout=_remaining(60))
            if not acquired:
                if session.stopped:
                    return
                if session.identity is None:
                    raise TimeoutError("preview cleanup lock budget exhausted")
                session.observation = BrokerCleanupObservation.create(
                    session.identity, (), requested_at=utc_now(), reason_code="budget_exhausted")
                return
            if session.identity is not None:
                self._stop_observed(session)
            else:
                self._stop_legacy(session)
        finally:
            if acquired:
                session.lock.release()
            _CLEANUP_CONTEXT.deadline = previous

    def _stop_observed(self, session: _Session) -> None:
        with session.lock:
            session.revoked = True
            if session.stopped:
                return
            assert session.identity is not None
            requested = utc_now()
            facts: list[ResourceObservation] = []
            failures: list[Exception] = []
            _CLEANUP_CONTEXT.facts = facts
            try:
                try:
                    _remaining(60)
                    if session.process is not None:
                        self._close_process(session.process)
                        session.process = None
                    facts.append(_fact("attachment", "exited"))
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    failures.append(error)
                    _failed_facts(("attachment",), error)
                for stage in sorted(_STAGES):
                    try:
                        _remaining(60)
                        self._stop_unit(f"agent-hub-preview-{session.handle}-{stage}.service")
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                        failures.append(error)
                        _failed_facts(("unit_" + stage, "cgroup_" + stage), error)
                try:
                    _remaining(60)
                    self._stop_storage(session)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    failures.append(error)
                    _failed_facts(("mount_unit", "work_path"), error)
                if not failures:
                    try:
                        _remaining(60)
                        self._remove_owned(session)
                        facts.append(_fact("owned_directory", "absent"))
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                        failures.append(error)
                        _failed_facts(("owned_directory",), error)
                session.observation = BrokerCleanupObservation.create(
                    session.identity, tuple(facts), requested_at=requested,
                )
                if session.observation.status == "confirmed":
                    with self._lock:
                        session.stopped = True
                        self._remember_stopped(session.handle, session.owner, session.observation)
                        self._sessions.pop(session.handle, None)
            finally:
                _CLEANUP_CONTEXT.facts = None

    def _remove_owned(self, session: _Session) -> None:
        # Verify the fixed parent even when the leaf is already absent.
        _require_root_path(self.policy.runtime_root)
        try:
            info = session.owned.lstat()
        except FileNotFoundError:
            return
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or getattr(info, "st_file_attributes", 0) & 0x400
                or (session.owned_identity is not None
                    and (info.st_dev, info.st_ino) != session.owned_identity)):
            raise RuntimeError("unsafe owned cleanup directory")
        shutil.rmtree(session.owned)
        _remaining(60)
        try:
            session.owned.lstat()
        except FileNotFoundError:
            return
        _collect(_fact("owned_directory", "present"))
        raise RuntimeError("owned cleanup directory remains")

    def _stop_legacy(self, session: _Session) -> None:
        with session.lock:
            session.revoked = True
            if session.stopped:
                return
            # Stop submission/attachment before checking unit state, so a late
            # systemd-run cannot create a unit after a not-found observation.
            if session.process is not None:
                self._close_process(session.process)
                session.process = None
            for unit in tuple(session.units):
                self._stop_unit(unit)
                session.units.remove(unit)
            if session.mounted:
                self._stop_storage(session)
            try:
                info = session.owned.lstat()
            except FileNotFoundError:
                pass
            else:
                if (not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                        or (session.owned_identity is None and info.st_uid != 0)
                        or (session.owned_identity is not None
                            and session.owned_identity != (info.st_dev, info.st_ino))):
                    raise RuntimeError("unsafe recovery directory")
                shutil.rmtree(session.owned)
                try:
                    session.owned.lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise RuntimeError("preview directory remains present")
            with self._lock:
                session.stopped = True
                self._remember_stopped(session.handle, session.owner)
                self._sessions.pop(session.handle, None)

    def _start(self, payload: dict[str, object], owner: object) -> dict[str, object]:
        with preview_startup_phase("storage_prepare"):
            session = self._reserve(cast(str, payload["preview_id"]), owner,
                                    cast(int, payload["lifetime_seconds"]))
        try:
            with session.lock:
                source = Path(cast(str, payload["source_root"]))
                legacy = payload.get("version", 1) == 1
                with preview_startup_phase("source_validate"):
                    initial_digest = _snapshot(source, self.policy, legacy=legacy)
                with preview_startup_phase("storage_prepare"):
                    for name in ("root", "source", "work"):
                        (session.owned / name).mkdir(mode=0o755)
                        (session.owned / name).chmod(0o755)
                # Bounded tmpfs backs *all* generated files, npm cache, data and
                # build output. No generated write touches the root filesystem.
                with preview_startup_phase("trusted_prepare"):
                    self._prepare_trusted(session)
                with preview_startup_phase("storage_prepare"):
                    self._prepare_storage(session)
                with preview_startup_phase("source_copy"):
                    file_manifest: dict[str, tuple[int, str]] = {}
                    digest = _snapshot(source, self.policy, session.owned / "source", legacy=legacy,
                                       file_manifest=file_manifest if not legacy else None)
                with preview_startup_phase("source_validate"):
                    if digest != initial_digest or _snapshot(source, self.policy, legacy=legacy) != digest:
                        raise ValueError("source changed during preparation")
                session.digest = digest
                if not legacy:
                    session.identity = PreviewIdentityV1.create(session.preview_id,
                        PreviewOwnerScope.from_wire(payload["scope"]), kind="dynamic",
                        digest=digest, handle=session.handle)
                    session.provenance = PreviewProvenanceV1(1, session.identity,
                        SnapshotManifestV1.from_manifest(file_manifest,
                            selection_policy=DYNAMIC_SELECTION_POLICY,
                            max_files=self.policy.max_files, max_bytes=self.policy.max_source_bytes),
                        utc_now())
                with preview_startup_phase("install"):
                    self._launch(session, "install")
                with preview_startup_phase("install_validate"):
                    self._validate_prepared_tree(session.owned / "work/app")
                with preview_startup_phase("install_handoff"):
                    self._handoff_work(session.owned / "work")
                with preview_startup_phase("build"):
                    self._launch(session, "build")
                with preview_startup_phase("build_validate"):
                    self._validate_prepared_tree(session.owned / "work/app")
                with preview_startup_phase("build_handoff"):
                    self._handoff_work(session.owned / "work")
                with preview_startup_phase("start"):
                    self._launch(session, "start")
                    if session.expires_at <= time.monotonic():
                        raise TimeoutError("preview lease expired during startup")
                session.ready = True
                if session.identity is not None:
                    return {"ok": True, "state": "ready", "identity": session.identity.to_wire(),
                            "recovery_token": self._recovery_token(session.handle),
                            "identity_binding": self._identity_binding(session.identity)}
                return {"ok": True, "state": "ready", "handle": session.handle,
                        "source_sha256": session.digest,
                        "recovery_token": self._recovery_token(session.handle)}
        except BaseException:
            self._stop(session)
            raise

    @staticmethod
    def _handoff_work(work: Path) -> None:
        # Called only after the stage's entire cgroup is stopped. Re-own before
        # chmod so the root broker needs CAP_CHOWN, not CAP_FOWNER or SYS_ADMIN.
        chown = getattr(os, "chown", None)
        if not callable(chown):
            raise RuntimeError("Linux ownership handoff unavailable")  # noqa: TRY004
        for path in [work, *work.rglob("*")]:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                if not path.resolve(strict=True).is_relative_to(work):
                    raise ValueError("private work link escapes owned disk")
                chown(path, 0, 0, follow_symlinks=False)
                continue
            if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
                raise ValueError("private work special file forbidden")
            chown(path, 0, 0, follow_symlinks=False)
            path.chmod(0o777 if stat.S_ISDIR(info.st_mode) or info.st_mode & 0o111 else 0o666)

    @staticmethod
    def _validate_prepared_tree(root: Path) -> None:
        if not root.is_dir() or root.is_symlink():
            raise ValueError("prepared app missing")
        for path in root.rglob("*"):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                if not path.resolve(strict=True).is_relative_to(root):
                    raise ValueError("prepared dependency link escapes app")
            elif not stat.S_ISDIR(info.st_mode) and not (
                stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            ):
                raise ValueError("prepared special file/hardlink forbidden")

    def handle(self, payload: dict[str, object], *, peer_uid: int,
               owner: object) -> dict[str, object]:
        if _PLATFORM != "linux":
            raise RuntimeError("preview root broker requires Linux")
        validate_broker_request(payload, peer_uid=peer_uid, policy=self.policy)
        action = payload["action"]
        if action == "recover_stop":
            if payload["version"] == 2:
                return self._recover_observed(payload, owner)
            return self._recover_stop(cast(str, payload["handle"]),
                                      cast(str, payload["recovery_token"]), owner)
        if action == "probe":
            probe_session = self._reserve("probe-" + uuid.uuid4().hex, owner, 30)
            try:
                (probe_session.owned / "root").mkdir(mode=0o755)
                (probe_session.owned / "root").chmod(0o755)
                (probe_session.owned / "work").mkdir(mode=0o755)
                with probe_phase("trusted_prepare"):
                    self._prepare_trusted(probe_session)
                with probe_phase("storage_prepare"):
                    self._prepare_storage(probe_session)
                self._launch(probe_session, "probe")
                with probe_phase("storage_roundtrip"):
                    if (probe_session.owned / "work/.runner-storage-probe").read_bytes() != b"preview-storage-v1":
                        raise ProbeFailure("storage_roundtrip", "invalid_result")
                return ({"ok": True, "state": "probe", "version": 2, "cleanup_schema_version": 1}
                        if payload["version"] == 2 else {"ok": True, "state": "probe"})
            finally:
                with probe_phase("cleanup"):
                    self._stop(probe_session)
        if action == "start":
            with self._lock:
                if any(session.owner is owner for session in self._sessions.values()):
                    raise ValueError("connection already owns a preview")
            return self._start(payload, owner)
        with self._lock:
            session = self._sessions.get(cast(str, payload["handle"]))
            completed = self._completed.get(cast(str, payload["handle"]))
            if action == "stop" and completed is not None and completed.owner is owner:
                if payload["version"] == 2:
                    if completed.observation is None:
                        raise ValueError("legacy cleanup has no identity evidence")
                    return self._observation_reply(completed.observation)
                return {"ok": True, "state": "stopped"}
        if session is None or session.owner is not owner:
            raise ValueError("preview ownership rejected")
        if payload["version"] == 2 and session.identity is None:
            raise ValueError("legacy session has no identity evidence")
        if action == "stop":
            self._stop(session)
            if payload["version"] == 2:
                assert session.observation is not None
                return self._observation_reply(session.observation)
            if not session.stopped:
                raise RuntimeError("preview cleanup retained for retry")
            return {"ok": True, "state": "stopped"}
        with session.lock:
            if session.revoked or time.monotonic() >= session.expires_at:
                session.revoked = True
                raise ValueError("preview lease revoked")
            if action == "source_provenance":
                if not session.ready or session.provenance is None:
                    raise ValueError("preview source provenance unavailable")
                if session.provenance.identity != session.identity:
                    raise ValueError("preview provenance identity mismatch")
                return {"ok": True, "provenance": session.provenance.to_wire()}
            process = session.process
            if process is None or process.poll() is not None:
                session.revoked = True
                raise RuntimeError("preview runner not alive")
            try:
                _pipe_write(process, cast(dict[str, object], payload["request"]))
                result = _pipe_read(process, REQUEST_TIMEOUT + 1)
                if type(result.get("ok")) is not bool:
                    raise ValueError("invalid runner response")
                if result["ok"] is not True:
                    return {"ok": False, "error": "application request failed; not replayed"}
                response = result.get("response")
                if set(result) != {"ok", "response"} or not isinstance(response, dict):
                    raise ValueError("invalid runner response")
                decode_response(cast(dict[str, object], response))
                return result
            except (OSError, EOFError, ValueError, TimeoutError):
                session.revoked = True
                self._stop(session)
                raise

    def disconnect(self, owner: object) -> None:
        with self._lock:
            sessions = [session for session in self._sessions.values() if session.owner is owner]
        for session in sessions:
            self._stop(session)
            if not session.stopped:
                raise RuntimeError("preview disconnect cleanup retained for retry")

    def recover(self) -> None:
        """Reclaim root-owned state after broker death before accepting callers."""
        for owned in self.policy.runtime_root.iterdir():
            if owned.name == ".recovery-key":
                continue
            info = owned.lstat()
            if not _HANDLE.fullmatch(owned.name) or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
                raise RuntimeError("unrecognized preview recovery state")
            if stat.S_ISLNK(info.st_mode) or (_PLATFORM == "linux" and info.st_mode & 0o022):
                raise RuntimeError("unsafe preview recovery directory")
            # A pending mount unit may not yet be visible in this namespace.
            # Reserve first and query/stop its exact unit through normal cleanup.
            session = _Session(owned.name, "recovered-" + owned.name, object(), owned, 0,
                               mounted=True, revoked=True)
            session.units = {f"agent-hub-preview-{owned.name}-{stage}.service" for stage in _STAGES}
            with self._lock:
                self._sessions[session.handle] = session
        self.reap(shutdown=True)

    def reap(self, *, shutdown: bool = False) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        failures: list[Exception] = []
        for session in sessions:
            crashed = session.ready and session.process is not None and session.process.poll() is not None
            if shutdown or session.revoked or crashed or time.monotonic() >= session.expires_at:
                try:
                    self._stop(session)
                    if not session.stopped:
                        raise RuntimeError("preview cleanup retained for retry")
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    failures.append(error)
        if failures:
            raise RuntimeError("preview cleanup retained for retry") from failures[0]


def load_broker_policy(config_path: Path) -> PreviewBrokerPolicy:
    info = config_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("preview broker config must be root-owned and not group/world writable")
    config = json_object(config_path.read_bytes())
    allowed = {"workspace_root", "allowed_uid", "runtime_root", "trusted_source_root", "node_root"}
    if not {"workspace_root", "allowed_uid"} <= set(config) or set(config) - allowed:
        raise ValueError("invalid administrator preview configuration")
    uid = config["allowed_uid"]
    if type(uid) is not int or uid <= 0:
        raise ValueError("console service UID must be non-root")
    paths: dict[str, Path] = {}
    for name in allowed - {"allowed_uid"}:
        value = config.get(name)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError("invalid administrator path")
            paths[name] = Path(value)
            _systemd_path(paths[name])
    return PreviewBrokerPolicy(
        workspace_root=paths["workspace_root"], allowed_uid=uid,
        runtime_root=paths.get("runtime_root", Path("/run/agent-hub-preview")),
        trusted_source_root=paths.get("trusted_source_root", Path("/opt/agent-hub/current/src/agent_hub")),
        node_root=paths.get("node_root", Path("/opt/agent-hub/node")),
    )


def _require_root_path(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("preview trusted runtime path must be immutable root-owned")


def serve_broker(listener: socket.socket, broker: PreviewBroker,
                 stopping: threading.Event) -> None:
    slots = threading.BoundedSemaphore(16)
    clients: set[socket.socket] = set()
    workers: list[threading.Thread] = []
    lock = threading.Lock()

    def serve(connection: socket.socket) -> None:
        owner = object()
        stream: FrameStream | None = None
        try:
            credentials = connection.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", -1), 12)
            _, uid, _ = struct.unpack("3i", credentials)
            if uid != broker.policy.allowed_uid:
                return
            connection.settimeout(15)
            stream = connection.makefile("rwb", buffering=0)
            while not stopping.is_set():
                payload = read_frame(stream)
                try:
                    result = broker.handle(payload, peer_uid=uid, owner=owner)
                except ProbeFailure as error:
                    print(str(error), file=sys.stderr, flush=True)
                    result = error.response()
                except PreviewStartupFailure as error:
                    print(str(error), file=sys.stderr, flush=True)
                    result = error.response()
                except (ValueError, OSError, RuntimeError, EOFError, subprocess.SubprocessError):
                    result = {"ok": False, "error": "preview broker operation failed"}
                write_frame(stream, result)
                if payload.get("action") == "start" and result.get("state") == "ready":
                    connection.settimeout(broker.policy.max_lifetime_seconds + 60)
        except (OSError, ValueError, EOFError):
            pass
        finally:
            with contextlib.suppress(OSError, RuntimeError, subprocess.SubprocessError):
                broker.disconnect(owner)
            if stream is not None:
                stream.close()
            connection.close()
            with lock:
                clients.discard(connection)
            slots.release()

    listener.settimeout(1)
    try:
        while not stopping.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                connection = None
            if connection is not None:
                if not slots.acquire(blocking=False):
                    connection.close()
                else:
                    with lock:
                        clients.add(connection)
                    worker = threading.Thread(target=serve, args=(connection,), daemon=True)
                    worker.start()
                    workers.append(worker)
            # One failed cleanup must not prevent other expired sessions' cleanup.
            with contextlib.suppress(OSError, RuntimeError, subprocess.SubprocessError):
                broker.reap()
            workers = [worker for worker in workers if worker.is_alive()]
    finally:
        with lock:
            for connection in clients:
                with contextlib.suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
        for worker in workers:
            worker.join(timeout=5)
        broker.reap(shutdown=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Dedicated privileged preview broker")
    parser.add_argument("--config", type=Path, default=Path("/etc/agent-hub/preview-broker.json"))
    args = parser.parse_args()
    if sys.platform != "linux" or getattr(os, "geteuid", lambda: -1)() != 0:
        raise SystemExit("preview broker requires root Linux systemd activation")
    policy = load_broker_policy(args.config)
    # Freeze administrator-owned release/node aliases once, before accepting API
    # callers. Generated staging paths still reject all aliases and links.
    policy = replace(policy, trusted_source_root=policy.trusted_source_root.resolve(strict=True),
                     node_root=policy.node_root.resolve(strict=True))
    for path in (policy.trusted_source_root / "previews/dynamic_runner.py",
                 policy.trusted_source_root / "harness/project_validation_sandbox.py", policy.node_root):
        _require_root_path(path)
    broker = PreviewBroker(policy)
    broker._initialize_runtime_root()
    for executable in ("systemd-run", "systemctl", "systemd-mount", "systemd-escape"):
        if shutil.which(executable, path="/usr/bin:/bin") != f"/usr/bin/{executable}":
            raise SystemExit("fixed system preview executables required")
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise SystemExit("dedicated systemd socket activation required")
    listener = socket.socket(fileno=3)
    if listener.family != socket.AF_UNIX or listener.getsockname() != str(BROKER_SOCKET_PATH):
        raise SystemExit("unexpected preview broker activation socket")
    broker.recover()
    stopping = threading.Event()
    import signal
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stopping.set())
    with listener:
        serve_broker(listener, broker, stopping)


if __name__ == "__main__":
    main()
