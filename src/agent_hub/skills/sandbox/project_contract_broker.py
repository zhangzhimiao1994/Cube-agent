"""Root broker staging of service-owned original project contracts."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from agent_hub.capabilities.project_baselines import ProjectBaselineReference, StoredProjectBaseline
from agent_hub.capabilities.project_contracts import (
    ManagedProjectInput,
    ProjectCheckContract,
    _path,
    build_project_check_contract,
)
from agent_hub.skills.sandbox.project import ProjectInvocation

_CONTROL_LIMIT = 32 * 1024**2
_ERROR = 'project original contract staging unavailable or unsafe'
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0)
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)


class ProjectContractStageError(RuntimeError):
    """A broker cannot issue a staged original from the supplied trusted reference."""


def _need(condition: object) -> None:
    if not condition:
        raise ValueError(_ERROR)


def _encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _need(key not in result)
        result[key] = value
    return result


def _decode_original(
    raw: bytes, intent_raw: bytes, invocation: ProjectInvocation, reference: ProjectBaselineReference,
) -> tuple[ProjectCheckContract, dict[str, str]]:
    _need(len(raw) <= _CONTROL_LIMIT and len(intent_raw) <= _CONTROL_LIMIT)
    _need(hashlib.sha256(raw).hexdigest() == reference.contract_sha256)
    value: object = json.loads(raw, object_pairs_hook=_object)
    _need(type(value) is dict)
    envelope = cast(dict[str, object], value)
    _need(envelope.keys() == {'schema_version', 'contract', 'source_files'}
          and type(envelope['schema_version']) is int and envelope['schema_version'] == 1)
    _need(_encoded(envelope) == raw)
    contract = ProjectCheckContract.from_payload(envelope['contract'])
    _need((contract.tenant_id, contract.run_id, contract.project_id, contract.session_id)
          == (invocation.tenant_id, invocation.run_id, invocation.project_id, invocation.session_id))
    _need(type(envelope['source_files']) is dict)
    files = cast(dict[str, str], envelope['source_files'])
    rebuilt = build_project_check_contract(
        invocation.tenant_id, invocation.run_id, invocation.project_id, invocation.session_id,
        reference.input_source_sha256, contract.editable_paths, files,
    )
    _need(rebuilt.to_payload() == contract.to_payload()
          and len(files) == reference.file_count <= invocation.file_limit)
    intent: object = json.loads(intent_raw, object_pairs_hook=_object)
    expected_intent = {
        'schema_version': 1, 'state': 'reserved', 'tenant_id': str(invocation.tenant_id),
        'run_id': str(invocation.run_id), 'project_id': invocation.project_id,
        'session_id': invocation.session_id,
        'project_input': ManagedProjectInput(contract.editable_paths).to_payload(),
    }
    _need(intent == expected_intent and _encoded(expected_intent) == intent_raw)
    return contract, dict(files)


def _stamp(info: os.stat_result, *, full: bool) -> tuple[int, ...]:
    basic = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
    return (*basic, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns) if full else basic


@dataclass(frozen=True, slots=True)
class _Observed:
    parent: int
    name: str
    identity: tuple[int, ...]
    full: bool
    descriptor: int | None = None

    def check(self) -> None:
        info = os.stat(self.name, dir_fd=self.parent, follow_symlinks=False)
        _need(_stamp(info, full=self.full) == self.identity)
        if self.descriptor is not None:
            _need(_stamp(os.fstat(self.descriptor), full=self.full) == self.identity)


def _open_directory(
    parent: int, name: str, owner: int, stack: ExitStack, observed: list[_Observed],
    *, mode: int | None = None, full: bool = False,
) -> int:
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    stack.callback(os.close, descriptor)
    info = os.fstat(descriptor)
    _need(stat.S_ISDIR(info.st_mode) and info.st_uid == owner)
    _need(stat.S_IMODE(info.st_mode) == mode if mode is not None else not info.st_mode & 0o022)
    entry = _Observed(parent, name, _stamp(info, full=full), full, descriptor)
    entry.check()
    observed.append(entry)
    return descriptor


def _root(
    path: Path, owner: int, stack: ExitStack, observed: list[_Observed], *, stage: bool = False,
) -> int:
    _need(path.is_absolute() and path.anchor == '/' and '..' not in path.parts and '\\' not in str(path))
    descriptor = os.open('/', _DIRECTORY_FLAGS)
    stack.callback(os.close, descriptor)
    for name in path.parts[1:]:
        child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
        stack.callback(os.close, child)
        info = os.fstat(child)
        _need(stat.S_ISDIR(info.st_mode) and info.st_uid in ({0} if stage else {0, owner}))
        _need(not info.st_mode & 0o022 or info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
        entry = _Observed(descriptor, name, _stamp(info, full=False), False, child)
        entry.check()
        observed.append(entry)
        descriptor = child
    final = os.fstat(descriptor)
    _need(final.st_uid == owner and not final.st_mode & 0o022)
    if stage:
        _need(stat.S_IMODE(final.st_mode) == 0o700)
    return descriptor


def _read_control(parent: int, name: str, owner: int, observed: list[_Observed]) -> bytes:
    descriptor = os.open(name, _READ_FLAGS, dir_fd=parent)
    try:
        before = os.fstat(descriptor)
        _need(stat.S_ISREG(before.st_mode) and before.st_uid == owner and before.st_nlink == 1
              and stat.S_IMODE(before.st_mode) == 0o600 and 0 < before.st_size <= _CONTROL_LIMIT)
        entry = _Observed(parent, name, _stamp(before, full=True), True, descriptor)
        entry.check()
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(1024**2, _CONTROL_LIMIT + 1 - total)):
            total += len(chunk)
            _need(total <= _CONTROL_LIMIT)
            chunks.append(chunk)
        _need(total == before.st_size)
        entry.check()
        observed.append(_Observed(parent, name, entry.identity, True))
        return b''.join(chunks)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, raw: bytes) -> None:
    remaining = memoryview(raw)
    while remaining:
        count = os.write(descriptor, remaining)
        _need(count > 0)
        remaining = remaining[count:]


def _seal_readonly(descriptor: int, mode: int) -> None:
    if sys.platform != 'linux':
        raise ValueError(_ERROR)
    os.fchmod(descriptor, mode)
    os.fsync(descriptor)


def _copy_file(
    parent: int, name: str, destination: int, remaining: int, owner: int, observed: list[_Observed],
) -> tuple[str, int]:
    source = os.open(name, _READ_FLAGS, dir_fd=parent)
    try:
        before = os.fstat(source)
        mode = stat.S_IMODE(before.st_mode)
        _need(stat.S_ISREG(before.st_mode) and before.st_uid == owner and before.st_nlink == 1
              and mode in {0o444, 0o555} and before.st_size <= remaining)
        entry = _Observed(parent, name, _stamp(before, full=True), True, source)
        entry.check()
        target = os.open(name, _WRITE_FLAGS, 0o600, dir_fd=destination)
        try:
            digest = hashlib.sha256(b'x\0' if mode & 0o111 else b'f\0')
            total = 0
            while chunk := os.read(source, min(1024**2, remaining + 1 - total)):
                total += len(chunk)
                _need(total <= remaining)
                digest.update(chunk)
                _write_all(target, chunk)
            _need(total == before.st_size)
            entry.check()
            _seal_readonly(target, 0o555 if mode & 0o111 else 0o444)
        finally:
            os.close(target)
        observed.append(_Observed(parent, name, entry.identity, True))
        return digest.hexdigest(), total
    finally:
        os.close(source)


def _copy_snapshot(
    source: int, destination: int, invocation: ProjectInvocation, owner: int,
    stack: ExitStack, observed: list[_Observed],
) -> dict[str, str]:
    files: dict[str, str] = {}
    aliases: dict[str, str] = {}
    total = visited = 0

    def walk(source_fd: int, destination_fd: int, prefix: str) -> None:
        nonlocal total, visited
        names: list[str] = []
        with os.scandir(source_fd) as entries:
            for entry in entries:
                visited += 1
                _need(visited <= invocation.file_limit * 10)
                names.append(entry.name)
        for name in sorted(names):
            relative = _path(prefix + name)
            lower = relative.lower()
            _need(lower not in aliases)
            aliases[lower] = relative
            info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = _open_directory(source_fd, name, owner, stack, observed, mode=0o555, full=True)
                os.mkdir(name, 0o700, dir_fd=destination_fd)
                target = os.open(name, _DIRECTORY_FLAGS, dir_fd=destination_fd)
                try:
                    walk(child, target, relative + '/')
                    _seal_readonly(target, 0o555)
                finally:
                    os.close(target)
            else:
                _need(stat.S_ISREG(info.st_mode) and len(files) < invocation.file_limit)
                digest, count = _copy_file(source_fd, name, destination_fd,
                                           invocation.source_limit_bytes - total, owner, observed)
                files[relative] = digest
                total += count

    walk(source, destination, '')
    _seal_readonly(destination, 0o555)
    return files


def stage_original_contract(
    workspace_root: Path, invocation: ProjectInvocation, reference: ProjectBaselineReference,
    stage: Path, allowed_uid: int,
) -> StoredProjectBaseline:
    """Copy a service-owned original into a root-owned broker stage, never execute it.

    runner.expected_contract_sha256 is contract.protected_sha256, NOT the
    reference.contract_sha256 (the separate baseline envelope's byte digest).
    The caller authenticates the run/reference and owns stage cleanup on failure.
    """
    try:
        _need(type(allowed_uid) is int and 0 < allowed_uid < 2**32 - 1)
        if sys.platform != 'linux':
            raise ValueError(_ERROR)
        _need(os.geteuid() == 0 and os.getegid() == 0)
        _need(getattr(os, 'O_DIRECTORY', 0) and getattr(os, 'O_NOFOLLOW', 0)
              and os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd)
        invocation = ProjectInvocation.model_validate(invocation.model_dump(), strict=True)
        _need(type(reference) is ProjectBaselineReference)
        reference = ProjectBaselineReference.from_payload(reference.to_payload())
        with ExitStack() as stack:
            observed: list[_Observed] = []
            target = _root(stage, 0, stack, observed, stage=True)
            for name in ('original_snapshot', 'check_contract.json'):
                try:
                    os.stat(name, dir_fd=target, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise ValueError(_ERROR)
            controls = _root(workspace_root, allowed_uid, stack, observed)
            for name in ('.project-contracts', str(invocation.tenant_id), str(invocation.run_id)):
                controls = _open_directory(controls, name, allowed_uid, stack, observed, mode=0o700)
            raw = _read_control(controls, 'baseline.json', allowed_uid, observed)
            intent = _read_control(controls, 'capture-intent.json', allowed_uid, observed)
            contract, expected = _decode_original(raw, intent, invocation, reference)
            container = _open_directory(controls, 'snapshot', allowed_uid, stack, observed, mode=0o700)
            source = _open_directory(container, 'source', allowed_uid, stack, observed, mode=0o555, full=True)
            os.mkdir('original_snapshot', 0o700, dir_fd=target)
            copied = os.open('original_snapshot', _DIRECTORY_FLAGS, dir_fd=target)
            stack.callback(os.close, copied)
            files = _copy_snapshot(source, copied, invocation, allowed_uid, stack, observed)
            observed.append(_Observed(target, 'original_snapshot', _stamp(os.fstat(copied), full=True),
                                       True, copied))
            _need(files == expected and len(files) == reference.file_count
                  and hashlib.sha256(_encoded(files)).hexdigest() == reference.input_source_sha256)
            for entry in observed:
                entry.check()
            descriptor = os.open('check_contract.json', _WRITE_FLAGS, 0o600, dir_fd=target)
            try:
                _write_all(descriptor, _encoded(contract.to_payload()))
                _seal_readonly(descriptor, 0o444)
                observed.append(_Observed(target, 'check_contract.json',
                                           _stamp(os.fstat(descriptor), full=True), True))
            finally:
                os.close(descriptor)
            os.fsync(target)
            for entry in observed:
                entry.check()
            return StoredProjectBaseline(reference, contract, stage / 'original_snapshot', files)
    except Exception:  # noqa: BLE001 - no source, private input or host path crosses this boundary.
        raise ProjectContractStageError(_ERROR) from None
