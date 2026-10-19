"""Trusted, stdlib-only project runner launched by the systemd broker with -I -B.

The broker owns the read-only input, quota tmpfs and outer cgroup lifetime.
Only the existing trusted harness constructs commands with network authority.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import queue
import re
import runpy
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import Any, NamedTuple, NoReturn

_STREAM_LIMIT = 8192
_REPORT_LIMIT = 65536
_NODE_HOME = Path("/opt/agent-hub/node")
_BWRAP = Path("/usr/bin/bwrap")
_INSTALL = ("npm", "install", "--ignore-scripts", "--no-audit", "--no-fund")
_PHASES = ("install", "build", "test")
_LOCKS = ("npm-shrinkwrap.json", "package-lock.json")
_CONTRACT_LIMIT = 4 * 1024 * 1024
_CONTRACT_OUTPUTS = ("node_modules", "dist", "build")


class Blocked(RuntimeError):
    """A constant, public reason for refusing project execution."""


class InfrastructureError(RuntimeError):
    """A runner failure; its underlying exception must never reach stderr."""


class ProcessResult(NamedTuple):
    exit_code: int | None
    timed_out: bool
    output_limited: bool
    stdout: str
    stderr: str


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Blocked("deadline_exceeded")
    return remaining


def _child_environment() -> dict[str, str]:
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    # Allows the process helper's offline Python tests to run on Windows too.
    if sys.platform == "win32" and "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    if sys.platform == "linux":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _run_bounded(
    command: Sequence[str], *, cwd: Path, deadline: float, pass_fds: tuple[int, ...] = (),
) -> ProcessResult:
    """Drain both pipes concurrently, killing on deadline or excess output.

    The queue and saved prefixes are bounded independently. Killing the bwrap
    supervisor destroys its PID namespace, including detached project daemons.
    Reserve part of the caller's remaining budget for kill/reap and pipe readers.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return ProcessResult(None, True, False, "", "")
    stop_at = deadline - min(0.2, remaining / 5)
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=_child_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=sys.platform != "win32",
        pass_fds=pass_fds,
    )
    events: queue.Queue[tuple[int, bytes | None]] = queue.Queue(maxsize=8)
    stopped = threading.Event()

    def read_pipe(index: int, descriptor: int) -> None:
        try:
            while not stopped.is_set():
                chunk = os.read(descriptor, 4096)
                event = (index, chunk if chunk else None)
                while not stopped.is_set():
                    try:
                        events.put(event, timeout=0.02)
                        break
                    except queue.Full:
                        continue
                if not chunk:
                    return
        except OSError:
            if not stopped.is_set():
                try:
                    events.put((index, None), timeout=0.02)
                except queue.Full:
                    pass

    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(target=read_pipe, args=(index, pipe.fileno()), daemon=True)
        for index, pipe in enumerate((process.stdout, process.stderr))
    ]
    saved = [bytearray(), bytearray()]
    eof: set[int] = set()
    timed_out = False
    output_limited = False
    try:
        for reader in readers:
            reader.start()
        while True:
            remaining = stop_at - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if len(eof) == 2 and process.poll() is not None:
                break
            try:
                index, chunk = events.get(timeout=min(0.02, remaining))
            except queue.Empty:
                continue
            if chunk is None:
                eof.add(index)
                continue
            available = _STREAM_LIMIT - len(saved[index])
            saved[index].extend(chunk[:available])
            if len(chunk) > available:
                output_limited = True
                break
    finally:
        _kill_process(process)
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise InfrastructureError("process_reap_failed") from None
        finally:
            stopped.set()
            process.stdout.close()
            process.stderr.close()
            for reader in readers:
                if reader.ident is not None:
                    reader.join(timeout=max(0, min(0.05, deadline - time.monotonic())))
        if any(reader.is_alive() for reader in readers):
            raise InfrastructureError("pipe_cleanup_failed")
    return ProcessResult(
        process.returncode,
        timed_out,
        output_limited,
        saved[0].decode("utf-8", errors="ignore"),
        saved[1].decode("utf-8", errors="ignore"),
    )


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _safe_directory(path: Path) -> None:
    for current in (*reversed(path.parents), path):
        info = current.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise Blocked("unsafe_input_entry")


def _read_regular(path: Path, deadline: float, target: Path | None = None) -> bytes:
    """Hash a no-follow regular file while optionally copying it in small chunks."""
    _remaining(deadline)
    info = path.lstat()
    if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise Blocked("unsafe_input_entry")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    output = None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise Blocked("unsafe_input_entry")
        digest = hashlib.sha256(b"x\0" if opened.st_mode & 0o111 else b"f\0")
        if target is not None:
            output = target.open("xb")
        while True:
            _remaining(deadline)
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            digest.update(chunk)
            if output is not None:
                output.write(chunk)
        # npm scripts can legitimately execute checked-in helper scripts.
        if target is not None:
            target.chmod(0o700 if opened.st_mode & 0o111 else 0o600)
        return digest.digest()
    finally:
        os.close(descriptor)
        if output is not None:
            output.close()


def _prepare_work(input_root: Path, work_root: Path) -> None:
    if not input_root.is_absolute() or not work_root.is_absolute():
        raise Blocked("absolute_paths_required")
    if ".." in input_root.parts or ".." in work_root.parts:
        raise Blocked("unsafe_path")
    try:
        _safe_directory(input_root)
    except FileNotFoundError:
        raise Blocked("input_unavailable") from None
    _safe_directory(work_root.parent)
    if (
        input_root == work_root
        or input_root in work_root.parents
        or work_root in input_root.parents
    ):
        raise Blocked("overlapping_paths")
    if work_root.exists() or work_root.is_symlink():
        _safe_directory(work_root)
        if next(work_root.iterdir(), None) is not None:
            raise Blocked("work_not_empty")
    else:
        work_root.mkdir(mode=0o700)


def _copy_snapshot(input_root: Path, work_root: Path, deadline: float) -> str:
    _remaining(deadline)
    _prepare_work(input_root, work_root)
    files: dict[str, str] = {}

    def copy_directory(source: Path, destination: Path) -> None:
        _remaining(deadline)
        with os.scandir(source) as entries:
            for entry in entries:
                _remaining(deadline)
                src = source / entry.name
                # DirEntry's cached Windows metadata omits the hardlink count.
                info = src.lstat()
                if _is_link(info):
                    raise Blocked("unsafe_input_entry")
                dst = destination / entry.name
                if stat.S_ISDIR(info.st_mode):
                    dst.mkdir(mode=0o700)
                    copy_directory(src, dst)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    digest = _read_regular(src, deadline, dst).hex()
                    relative = src.relative_to(input_root).as_posix()
                    files[relative] = digest
                else:
                    raise Blocked("unsafe_input_entry")

    copy_directory(input_root, work_root)
    _remaining(deadline)
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _cleanup_work(path: Path, deadline: float) -> None:
    _remaining(deadline)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        path.unlink()
        return
    path.chmod(0o700)
    with os.scandir(path) as entries:
        for entry in entries:
            _cleanup_work(path / entry.name, deadline)
    _remaining(deadline)
    path.rmdir()


def _load_sandbox() -> dict[str, Any]:
    source = Path(__file__).resolve().parents[2] / "harness/project_validation_sandbox.py"
    return runpy.run_path(str(source))


@cache
def _load_contract_mounts() -> dict[str, Any]:
    return runpy.run_path(str(Path(__file__).with_name("project_contract_mounts.py")))


class _ContractExecution(NamedTuple):
    payload: dict[str, Any]
    contract_sha256: str
    protected_sha256: str
    original_input: Path
    source_sha256: str
    output_budget_bytes: int
    output_file_limit: int

    def arguments(self, work_root: Path) -> dict[str, Any]:
        return {
            "contract_payload": self.payload,
            "expected_contract_sha256": self.protected_sha256,
            "original_root": self.original_input,
            "work_root": work_root,
            "expected_source_sha256": self.source_sha256,
            "ephemeral_dirs": _CONTRACT_OUTPUTS,
            "ephemeral_budget_bytes": self.output_budget_bytes,
            "ephemeral_file_limit": self.output_file_limit,
        }


def _file_identity(info: os.stat_result) -> tuple[int, ...]:
    base = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns)
    # Windows lstat/fstat creation-time metadata can differ. It never executes
    # contract mounts; Linux retains the full change-time race check.
    return base if sys.platform == "win32" else (*base, info.st_ctime_ns)


def _read_check_contract(path: Path, expected_sha256: str, deadline: float) -> dict[str, Any]:
    """Bind broker-owned JSON to the shared canonical contract digest."""
    try:
        if (not path.is_absolute() or ".." in path.parts
                or str(path.resolve(strict=True)) != str(path)
                or re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is None):
            raise ValueError
        _safe_directory(path.parent)
        ancestors = {parent: _file_identity(parent.lstat())[:3] for parent in path.parents}
        before = path.lstat()
        if (_is_link(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_mode & 0o222 or before.st_size > _CONTRACT_LIMIT):
            raise ValueError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        try:
            opened = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(before):
                raise ValueError
            chunks: list[bytes] = []
            count = 0
            while True:
                _remaining(deadline)
                data = os.read(descriptor, min(65536, before.st_size - count + 1))
                if not data:
                    break
                count += len(data)
                if count > before.st_size:
                    raise ValueError
                chunks.append(data)
            if (count != before.st_size
                    or _file_identity(os.fstat(descriptor)) != _file_identity(opened)
                    or _file_identity(path.lstat()) != _file_identity(before)):
                raise ValueError
        finally:
            os.close(descriptor)
        _safe_directory(path.parent)
        if any(_file_identity(parent.lstat())[:3] != identity for parent, identity in ancestors.items()):
            raise ValueError
        raw = b"".join(chunks)

        def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        def reject_constant(value: str) -> NoReturn:
            raise ValueError

        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=strict_object,
                             parse_constant=reject_constant)
        source = Path(__file__).resolve().parents[2] / "capabilities/project_contracts.py"
        contract = runpy.run_path(str(source))["ProjectCheckContract"].from_payload(payload)
        if contract.protected_sha256 != expected_sha256:
            raise ValueError
        result: dict[str, Any] = contract.to_payload()
        return result
    except (OSError, ValueError, TypeError, RecursionError):
        raise Blocked("invalid_check_contract") from None


def _contract_command(
    command: Sequence[str], *, work_root: Path, deadline: float, contract: _ContractExecution,
) -> ProcessResult:
    support = _load_contract_mounts()
    try:
        with support["build_contract_mounts"](command, **contract.arguments(work_root)) as plan:
            _remaining(deadline)
            return _run_bounded(plan.argv, cwd=work_root, deadline=deadline, pass_fds=plan.pass_fds)
    except support["ContractMountError"]:
        raise Blocked("project_contract_mounts_rejected") from None


def _prepare_contract_work(contract: _ContractExecution, work_root: Path) -> None:
    support = _load_contract_mounts()
    try:
        arguments = contract.arguments(work_root)
        support["validate_contract_work"](**{**arguments, "ephemeral_dirs": ()})
        for name in _CONTRACT_OUTPUTS:
            # Even an empty input directory is source, not an owned output.
            if (work_root / name).exists() or (work_root / name).is_symlink():
                raise Blocked("project_contract_mounts_rejected")
        for name in _CONTRACT_OUTPUTS:
            (work_root / name).mkdir(mode=0o700)
        support["validate_contract_work"](**arguments)
    except support["ContractMountError"]:
        raise Blocked("project_contract_mounts_rejected") from None


def _backend_reason(sandbox: Mapping[str, Any]) -> str | None:
    if sys.platform != "linux":
        return "linux_required"
    if os.environ.get("AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX") != "systemd":
        return "systemd_authorization_required"
    if not _BWRAP.is_file() or not os.access(_BWRAP, os.X_OK):
        return "bwrap_unavailable"
    if not _NODE_HOME.is_dir():
        return "node_runtime_missing"
    for name in ("node", "npm"):
        binary = _NODE_HOME / "bin" / name
        try:
            resolved = binary.resolve(strict=True)
        except (OSError, RuntimeError):
            return name + "_runtime_missing"
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            return name + "_runtime_missing"
        if not resolved.is_relative_to(_NODE_HOME.resolve(strict=True)):
            return name + "_runtime_untrusted"
    if not sandbox["sandbox_available"]():
        return "bwrap_unavailable"
    return None


def _phase_command(
    sandbox: Mapping[str, Any], phase: str, work_root: Path, *, original_contract: bool = False,
) -> list[str]:
    if phase not in _PHASES:
        raise InfrastructureError("invalid_phase")
    command = _INSTALL if phase == "install" else ("npm", "run", phase)
    result: list[str]
    if original_contract and phase == 'install':
        result = sandbox['generated_contract_install_command'](cwd=work_root, config={},
            has_lock=any((work_root / filename).is_file() for filename in _LOCKS))
    else:
        result = sandbox["generated_command"](command, cwd=work_root, config={})
    if phase == "install":
        separator = result.index("--")
        result[separator:separator] = ["--setenv", "NPM_CONFIG_ENGINE_STRICT", "true"]
    return result


def _empty_report() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "blocked",
        "source_sha256": "",
        "checks": [
            {
                "phase": phase,
                "status": "not_run",
                "exit_code": None,
                "timed_out": False,
                "stdout": "",
                "stderr": "",
            }
            for phase in _PHASES
        ],
        "runtime": {"node": "", "npm": ""},
    }


def _manifest(work_root: Path, deadline: float) -> dict[str, Any]:
    _remaining(deadline)
    if (work_root / "pnpm-lock.yaml").exists() or (work_root / "pnpm-workspace.yaml").exists():
        raise Blocked("unsupported_pnpm")
    path = work_root / "package.json"
    if not path.is_file():
        if any(
            (work_root / name).exists()
            for name in ("pyproject.toml", "requirements.txt", "setup.py")
        ):
            raise Blocked("unsupported_python")
        raise Blocked("unsupported_project")
    if path.stat().st_size > 1024 * 1024:
        raise Blocked("invalid_package_manifest")
    try:
        manifest = json.loads(path.read_bytes())
    except (ValueError, UnicodeError, RecursionError):
        raise Blocked("invalid_package_manifest") from None
    if not isinstance(manifest, dict):
        raise Blocked("invalid_package_manifest")
    # These resolution mechanisms are outside the existing install guard's
    # supported contract. Reject even empty declarations rather than guessing.
    for field in ("workspaces", "overrides", "resolutions", "pnpm"):
        if field in manifest:
            raise Blocked("unsupported_" + field)
    manager = manifest.get("packageManager", "npm")
    if not isinstance(manager, str):
        raise Blocked("invalid_package_manifest")
    if manager.split("@", 1)[0] == "pnpm":
        raise Blocked("unsupported_pnpm")
    if manager.split("@", 1)[0] != "npm" or (work_root / "yarn.lock").exists():
        raise Blocked("unsupported_package_manager")
    if "devEngines" in manifest:
        engines = manifest["devEngines"]
        if isinstance(engines, dict) and "packageManager" in engines:
            managers = engines["packageManager"]
            if not isinstance(managers, list):
                managers = [managers]
            if any(not isinstance(item, dict) or item.get("name") != "npm" for item in managers):
                raise Blocked("unsupported_package_manager")
        # npm versions differ in devEngines support; only engines with the
        # fixed ENGINE_STRICT policy are currently supported.
        raise Blocked("unsupported_dev_engines")
    return manifest


def _probe_runtime(
    sandbox: Mapping[str, Any],
    work_root: Path,
    deadline: float,
    runtime: dict[str, str],
    contract: _ContractExecution | None = None,
) -> None:
    for name in ("node", "npm"):
        _remaining(deadline)
        command = sandbox["generated_command"]((name, "--version"), cwd=work_root, config={})
        try:
            if contract is None:
                result = _run_bounded(command, cwd=work_root, deadline=deadline)
            else:
                result = _contract_command(command, work_root=work_root, deadline=deadline,
                                           contract=contract)
        except OSError:
            raise Blocked(name + "_probe_unavailable") from None
        pattern = (
            r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?"
            if name == "node"
            else (r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?")
        )
        version = result.stdout.strip()
        if (
            result.exit_code != 0
            or result.timed_out
            or result.output_limited
            or len(version) > 128
            or not re.fullmatch(pattern, version)
        ):
            details = result.stderr.casefold()
            if 'namespace' in details or 'uid map' in details or 'gid map' in details:
                raise Blocked('user_namespace_unavailable')
            if 'mount' in details:
                for marker, reason in (
                    ('/workspace', 'workspace_mount_failed'),
                    ('/opt/validator/src', 'trusted_source_mount_failed'),
                    ('/opt/validator/node', 'node_mount_failed'),
                    ('/etc/resolv.conf', 'resolver_mount_failed'),
                    ('/etc/hosts', 'hosts_mount_failed'),
                    ('/etc/ssl/certs', 'certificates_mount_failed'),
                    ('/lib64', 'lib64_mount_failed'),
                    ('/lib', 'lib_mount_failed'),
                    ('/usr', 'usr_mount_failed'),
                    ('/proc', 'proc_mount_failed'),
                    ('/dev', 'devices_mount_failed'),
                    ('slave', 'mount_propagation_failed'),
                    ('tmpfs', 'tmpfs_mount_failed'),
                ):
                    if marker in details:
                        raise Blocked(reason)
                raise Blocked('sandbox_mount_failed')
            if 'permission denied' in details:
                raise Blocked('runtime_access_denied')
            raise Blocked(name + "_probe_failed")
        runtime[name] = version


def _locks(work_root: Path, deadline: float) -> dict[str, bytes]:
    return {
        name: _read_regular(work_root / name, deadline)
        for name in _LOCKS
        if (work_root / name).exists() or (work_root / name).is_symlink()
    }


def run_project(
    input_root: Path,
    work_root: Path,
    timeout: float,
    *,
    install_allowed: bool = False,
    check_contract: Path | None = None,
    original_input: Path | None = None,
    expected_contract_sha256: str | None = None,
    workspace_limit_bytes: int = 512 * 1024**2,
    file_limit: int = 10000,
) -> dict[str, Any]:
    report = _empty_report()
    if not math.isfinite(timeout) or timeout <= 0:
        raise InfrastructureError("invalid_timeout")
    deadline = time.monotonic() + timeout
    command_deadline = deadline - min(1.0, timeout / 10)
    owned_work = False
    try:
        contract_payload = None
        contract: _ContractExecution | None = None
        opt_in = (check_contract is not None, original_input is not None,
                  expected_contract_sha256 is not None)
        if any(opt_in):
            if (not all(opt_in) or not isinstance(check_contract, Path)
                    or not isinstance(original_input, Path) or type(expected_contract_sha256) is not str):
                raise Blocked("invalid_check_contract")
            if (type(workspace_limit_bytes) is not int or not 1 <= workspace_limit_bytes <= 2 * 1024**3
                    or type(file_limit) is not int or not 1 <= file_limit <= 100000):
                raise Blocked('invalid_contract_resource_allocation')
            contract_payload = _read_check_contract(check_contract, expected_contract_sha256,
                                                   command_deadline)
        # Never clean a caller's pre-existing nonempty directory on rejection.
        _prepare_work(input_root, work_root)
        owned_work = True
        report["source_sha256"] = _copy_snapshot(input_root, work_root, command_deadline)
        if contract_payload is not None:
            assert original_input is not None and expected_contract_sha256 is not None
            source_bytes = sum((Path(directory) / name).stat().st_size
                for directory, _, names in os.walk(work_root) for name in names)
            output_budget = workspace_limit_bytes - source_bytes
            if output_budget <= 0:
                raise Blocked('work_quota_exceeded')
            contract = _ContractExecution(contract_payload, expected_contract_sha256,
                                          contract_payload["protected_sha256"], original_input,
                                          report["source_sha256"], output_budget,
                                          min(100000, file_limit * 10))
            _prepare_contract_work(contract, work_root)
            report["original_check_contract"] = {
                "schema_version": 1, "contract_sha256": contract.contract_sha256,
                "protected_sha256": contract.protected_sha256,
            }
        _manifest(work_root, command_deadline)
        sandbox = _load_sandbox()
        reason = _backend_reason(sandbox)
        if reason:
            raise Blocked(reason)
        if contract is None:
            _probe_runtime(sandbox, work_root, command_deadline, report["runtime"])
        else:
            _probe_runtime(sandbox, work_root, command_deadline, report["runtime"], contract)
        if not install_allowed:
            raise Blocked("install_authorization_required")
        original_locks = _locks(work_root, command_deadline)
        for check in report["checks"]:
            _remaining(command_deadline)
            phase = check["phase"]
            try:
                command = _phase_command(sandbox, phase, work_root, original_contract=True) if contract is not None else (
                    _phase_command(sandbox, phase, work_root)
                )
                if contract is None:
                    result = _run_bounded(command, cwd=work_root, deadline=command_deadline)
                else:
                    result = _contract_command(command, work_root=work_root,
                                               deadline=command_deadline, contract=contract)
            except OSError:
                raise Blocked("phase_launch_unavailable") from None
            check.update(
                status="passed"
                if result.exit_code == 0 and not result.timed_out and not result.output_limited
                else "failed",
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                stdout=result.stdout,
                stderr=result.stderr,
            )
            if check["status"] == "failed":
                report["status"] = "failed"
                if result.output_limited:
                    report["reason"] = "phase_output_limit_exceeded"
                elif result.timed_out:
                    report["reason"] = "phase_deadline_exceeded"
                break
            if phase == "install":
                for name, digest in original_locks.items():
                    try:
                        current = _read_regular(work_root / name, command_deadline)
                    except FileNotFoundError:
                        raise Blocked("input_lock_changed") from None
                    if current != digest:
                        raise Blocked("input_lock_changed")
        else:
            report["status"] = "passed"
    except Blocked as exc:
        report["status"] = "blocked"
        report["reason"] = str(exc)
    except OSError as exc:
        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            report.update(status="blocked", reason="work_quota_exceeded")
        elif exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            report.update(status="blocked", reason="unsafe_input_entry")
        else:
            raise InfrastructureError("workspace_io_failed") from None
    finally:
        if owned_work:
            try:
                _cleanup_work(work_root, deadline)
            except Blocked:
                # The outer systemd cgroup and quota tmpfs remain broker-owned.
                if report["status"] != "failed":
                    report.update(status="blocked", reason="cleanup_deadline_exceeded")
            except OSError:
                raise InfrastructureError("workspace_cleanup_failed") from None
    return report


def _encode_report(report: dict[str, Any]) -> bytes:
    limit = _STREAM_LIMIT
    while True:
        for check in report["checks"]:
            for stream in ("stdout", "stderr"):
                check[stream] = (
                    check[stream].encode("utf-8")[:limit].decode("utf-8", errors="ignore")
                )
        encoded = json.dumps(
            report, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(encoded) + 1 < _REPORT_LIMIT:
            return encoded
        limit //= 2
        if limit == 0:
            raise InfrastructureError("report_size_exceeded")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise InfrastructureError("invalid_arguments")


def main(argv: Sequence[str] | None = None) -> int:
    started = time.monotonic()
    try:
        parser = _Parser(add_help=False, allow_abbrev=False)
        parser.add_argument("--input", type=Path, required=True)
        parser.add_argument("--work", type=Path, required=True)
        parser.add_argument("--timeout", type=float, required=True)
        parser.add_argument("--install-allowed", action="store_true")
        parser.add_argument("--check-contract", type=Path)
        parser.add_argument("--original-input", type=Path)
        parser.add_argument("--expected-contract-sha256")
        parser.add_argument('--workspace-limit-bytes', type=int, default=512 * 1024**2)
        parser.add_argument('--file-limit', type=int, default=10000)
        args = parser.parse_args(argv)
        flags = (args.check_contract is not None, args.original_input is not None,
                 args.expected_contract_sha256 is not None)
        if any(flags) and not all(flags):
            raise InfrastructureError("invalid_arguments")
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise InfrastructureError("invalid_timeout")
        remaining = args.timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise InfrastructureError("invalid_timeout")
        options = ({"check_contract": args.check_contract, "original_input": args.original_input,
                    "expected_contract_sha256": args.expected_contract_sha256,
                    'workspace_limit_bytes': args.workspace_limit_bytes,
                    'file_limit': args.file_limit} if all(flags) else {})
        report = run_project(args.input, args.work, remaining, install_allowed=args.install_allowed,
                             **options)
        sys.stdout.buffer.write(_encode_report(report) + b"\n")
        return 0
    except Exception:  # noqa: BLE001 - CLI boundary must not print exception values/tracebacks.
        sys.stderr.write("project_runner_infrastructure_error\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
