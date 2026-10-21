"""Immutable original/generated captures; storage is not execution authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from collections.abc import Mapping
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from uuid import UUID

from agent_hub.capabilities.project_contracts import (
    ManagedProjectInput,
    ProjectCheckContract,
    _editables,
    build_project_check_contract,
)
from agent_hub.capability_installer.python_attestation import _identity, read_control
from agent_hub.plugins.handover_snapshots import _Directory, _ownership
from agent_hub.skills.sandbox.project import (
    ProjectInvocation,
    _directory,
    _scope,
    project_source_fingerprint,
    snapshot_project_source,
)

if TYPE_CHECKING:
    from agent_hub.runtime.contracts import JsonValue

_NAMESPACE = '.project-contracts'
_CONTROL_LIMIT = 32 * 1024**2
_SHA = re.compile('[a-f0-9]{64}')


class ProjectBaselineError(RuntimeError):
    """A baseline cannot be captured, re-created or trusted from this reference."""


def _encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, ensure_ascii=True, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode('ascii')


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate baseline key')
        result[key] = value
    return result


def _fingerprint(files: Mapping[str, str]) -> str:
    return hashlib.sha256(_encoded(files)).hexdigest()


def _generated_metadata(value: object) -> Mapping[str, JsonValue]:
    keys = {'schema_version', 'purpose', 'capture_phase', 'user_id', 'request_sha256', 'manifest_sha256'}
    if not isinstance(value, Mapping) or len(value) != len(keys) or set(value) != keys:
        raise ValueError('invalid generated capture metadata')
    item = dict(value)
    if (type(item['schema_version']) is not int or item['schema_version'] != 1
            or any(type(item[key]) is not str for key in keys - {'schema_version'})
            or item['purpose'] != 'generated_acceptance' or item['capture_phase'] != 'post_model'
            or any(_SHA.fullmatch(item[key]) is None for key in ('request_sha256', 'manifest_sha256'))
            or str(UUID(item['user_id'])) != item['user_id']):
        raise ValueError('invalid generated capture metadata')
    return MappingProxyType(cast('dict[str, JsonValue]', item))


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), info.st_uid, info.st_gid, getattr(info, 'st_file_attributes', 0))


def _snapshot_permissions(path: Path, info: os.stat_result) -> None:
    _ownership(path, info, private=os.name == 'nt', directory=stat.S_ISDIR(info.st_mode))
    if os.name != 'nt' and info.st_mode & 0o222:
        raise ValueError('baseline snapshot is writable')


def _seal_snapshot_acl(owned: _Directory, snapshot: Path) -> None:
    if sys.platform != 'win32':
        return
    import ctypes
    from ctypes import wintypes

    owned.check()
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
        wintypes.LPVOID, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
    )
    advapi.SetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID,
        wintypes.LPVOID, wintypes.LPVOID,
    )
    advapi.EqualSid.argtypes = (wintypes.LPVOID, wintypes.LPVOID)
    kernel.LocalFree.argtypes = (wintypes.LPVOID,)
    owner, dacl, descriptor = wintypes.LPVOID(), wintypes.LPVOID(), wintypes.LPVOID()
    if advapi.GetNamedSecurityInfoW(str(owned.path), 1, 5, ctypes.byref(owner), None,
                                   ctypes.byref(dacl), None, ctypes.byref(descriptor)) != 0:
        raise ValueError('baseline private ACL unavailable')
    try:
        # Windows mkdir(0700) adds administrative ACEs. Copy the already verified
        # private parent's DACL onto only our new no-link tree before publication.
        pending = [snapshot]
        while pending:
            path = pending.pop()
            info = path.lstat()
            if (path.resolve(strict=True) != path or not path.is_relative_to(owned.path)
                    or stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                    or not (stat.S_ISDIR(info.st_mode)
                            or stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
                raise ValueError('baseline ACL target alias')
            target_owner, target_descriptor = wintypes.LPVOID(), wintypes.LPVOID()
            if advapi.GetNamedSecurityInfoW(str(path), 1, 1, ctypes.byref(target_owner), None,
                                           None, None, ctypes.byref(target_descriptor)) != 0:
                raise ValueError('baseline ACL target owner unavailable')
            try:
                if not advapi.EqualSid(owner, target_owner):
                    raise ValueError('baseline ACL target owner changed')
            finally:
                kernel.LocalFree(target_descriptor)
            if advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, dacl, None) != 0:
                raise ValueError('baseline private ACL could not be sealed')
            _snapshot_permissions(path, path.lstat())
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    pending.extend(path / entry.name for entry in entries)
        owned.check()
    finally:
        kernel.LocalFree(descriptor)


@dataclass(frozen=True, slots=True)
class ProjectBaselineReference:
    contract_sha256: str
    input_source_sha256: str
    file_count: int

    def __post_init__(self) -> None:
        if (any(type(value) is not str or _SHA.fullmatch(value) is None
                for value in (self.contract_sha256, self.input_source_sha256))
                or type(self.file_count) is not int or not 1 <= self.file_count <= 100_000):
            raise ValueError('invalid project baseline reference')

    def to_payload(self) -> dict[str, object]:
        return {'schema_version': 1, 'contract_sha256': self.contract_sha256,
                'input_source_sha256': self.input_source_sha256, 'file_count': self.file_count}

    @classmethod
    def from_payload(cls, value: object) -> ProjectBaselineReference:
        if (not isinstance(value, Mapping)
                or set(value) != {'schema_version', 'contract_sha256', 'input_source_sha256', 'file_count'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1):
            raise ValueError('invalid project baseline reference')
        return cls(cast(str, value['contract_sha256']), cast(str, value['input_source_sha256']),
                   cast(int, value['file_count']))


@dataclass(frozen=True, slots=True)
class StoredProjectBaseline:
    reference: ProjectBaselineReference
    contract: ProjectCheckContract
    snapshot: Path
    source_files: Mapping[str, str]
    generated_metadata: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        files = dict(self.source_files)
        if len(files) != self.reference.file_count or _fingerprint(files) != self.reference.input_source_sha256:
            raise ValueError('invalid original source manifest')
        object.__setattr__(self, 'source_files', MappingProxyType(files))
        if self.generated_metadata is not None:
            object.__setattr__(self, 'generated_metadata', _generated_metadata(self.generated_metadata))


def _validate_generated_snapshot(
    snapshot: Path, invocation: ProjectInvocation, contract: ProjectCheckContract,
    files: Mapping[str, str], metadata: Mapping[str, JsonValue],
) -> None:
    from agent_hub.capabilities.project_acceptance import parse_acceptance_manifest

    path = snapshot / 'ACCEPTANCE.json'
    before = path.lstat()
    _snapshot_permissions(path, before)
    raw, receipt = read_control(path, max_bytes=min(_CONTROL_LIMIT, invocation.source_limit_bytes))
    tag = b'x\0' if before.st_mode & 0o111 else b'f\0'
    if (_identity(before) != receipt['identity'] or _stamp(path.lstat()) != _stamp(before)
            or files.get('ACCEPTANCE.json') != hashlib.sha256(tag + raw).hexdigest()
            or hashlib.sha256(raw).hexdigest() != metadata['manifest_sha256']):
        raise ValueError('generated manifest changed')
    manifest = parse_acceptance_manifest(raw)
    required = {'ACCEPTANCE.json', *manifest.test_files}
    if (manifest.editable_paths != contract.editable_paths or not required <= files.keys()
            or not required <= contract.protected_files.keys()):
        raise ValueError('generated acceptance files are unavailable or editable')


def _snapshot_files(snapshot: Path, invocation: ProjectInvocation) -> dict[str, str]:
    _directory(snapshot)
    result: dict[str, str] = {}
    pending = [snapshot]
    observed: dict[Path, tuple[int, ...]] = {}
    total = visited = 0
    while pending:
        directory = pending.pop()
        _directory(directory)
        before = directory.lstat()
        _snapshot_permissions(directory, before)
        observed[directory] = _stamp(before)
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > invocation.file_limit * 10:
                    raise ValueError('baseline entry limit')
                path = directory / entry.name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise ValueError('baseline link')
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                    continue
                if len(result) >= invocation.file_limit:
                    raise ValueError('baseline file limit')
                raw, receipt = read_control(path, max_bytes=invocation.source_limit_bytes - total)
                _snapshot_permissions(path, info)
                if _identity(info) != receipt['identity']:
                    raise ValueError('baseline file changed before read')
                observed[path] = _stamp(info)
                total += len(raw)
                if total > invocation.source_limit_bytes:
                    raise ValueError('baseline byte limit')
                mode_tag = b'x\0' if info.st_mode & 0o111 else b'f\0'
                result[path.relative_to(snapshot).as_posix()] = hashlib.sha256(mode_tag + raw).hexdigest()
    for path, identity in observed.items():
        info = path.lstat()
        _snapshot_permissions(path, info)
        if _stamp(info) != identity:
            raise ValueError('baseline member changed')
        if os.name == 'nt' and stat.S_ISREG(info.st_mode):
            # Windows creation-time clocks cannot prove a same-size write whose
            # mtime was restored. Recheck bytes as well as the complete identity.
            raw, receipt = read_control(path, max_bytes=info.st_size)
            mode_tag = b'x\0' if info.st_mode & 0o111 else b'f\0'
            if (_identity(info) != receipt['identity']
                    or hashlib.sha256(mode_tag + raw).hexdigest() != result[path.relative_to(snapshot).as_posix()]):
                raise ValueError('baseline file bytes changed')
    return result


async def _scan_snapshot(snapshot: Path, invocation: ProjectInvocation) -> dict[str, str]:
    worker = asyncio.create_task(asyncio.to_thread(_snapshot_files, snapshot, invocation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - drain the reader before owned cleanup.
                break
        with suppress(Exception, asyncio.CancelledError):
            worker.result()
        raise


class ProjectBaselineStore:
    def __init__(self, workspace_root: Path) -> None:
        self.root = workspace_root

    def _invocation(
        self, invocation: ProjectInvocation, *, require_workspace: bool = True,
    ) -> ProjectInvocation:
        result = ProjectInvocation.model_validate(invocation.model_dump(), strict=True)
        if not self.root.is_absolute() or '..' in self.root.parts:
            raise ValueError('baseline root must be canonical')
        _directory(self.root)
        if require_workspace:
            _scope(self.root, result)
        return result

    def _controls(self, invocation: ProjectInvocation, stack: ExitStack, *, create: bool) -> _Directory:
        parent = _Directory(self.root, private=False)
        stack.callback(parent.close)
        for name in (_NAMESPACE, str(invocation.tenant_id), str(invocation.run_id)):
            child = parent.child(name) if create else _Directory(parent.path / name, private=True)
            stack.callback(child.close)
            parent.check()
            parent = child
        return parent

    @staticmethod
    def _write_once(parent: _Directory, name: str, data: bytes) -> None:
        if len(data) > _CONTROL_LIMIT:
            raise ValueError('baseline control limit')
        temporary = 'baseline-partial.json' if name == 'baseline.json' else name
        descriptor = parent.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary != name:
            parent.check()
            if parent.fd is None:
                os.link(parent.path / temporary, parent.path / name, follow_symlinks=False)
                parent.sync()
                (parent.path / temporary).unlink()
            else:
                os.link(temporary, name, src_dir_fd=parent.fd, dst_dir_fd=parent.fd, follow_symlinks=False)
                parent.sync()
                os.unlink(temporary, dir_fd=parent.fd)
        parent.sync()

    @staticmethod
    def _intent(invocation: ProjectInvocation, project_input: ManagedProjectInput) -> dict[str, object]:
        return {
            'schema_version': 1, 'state': 'reserved', 'tenant_id': str(invocation.tenant_id),
            'run_id': str(invocation.run_id), 'project_id': invocation.project_id,
            'session_id': invocation.session_id, 'project_input': project_input.to_payload(),
        }

    @staticmethod
    def _generated_intent(
        invocation: ProjectInvocation, editable_paths: tuple[str, ...], metadata: Mapping[str, JsonValue],
    ) -> dict[str, object]:
        return {
            'schema_version': 2, 'purpose': 'generated_acceptance', 'state': 'reserved',
            'tenant_id': str(invocation.tenant_id), 'run_id': str(invocation.run_id),
            'project_id': invocation.project_id, 'session_id': invocation.session_id,
            'editable_paths': list(editable_paths), 'generated_metadata': metadata,
        }

    @staticmethod
    def _remove_partial(controls: _Directory, owned: _Directory) -> None:
        controls.check()
        owned.check()
        if owned.path != controls.path / 'snapshot':
            raise ValueError('baseline cleanup ownership')
        if os.name == 'nt':
            try:
                (owned.path / 'source').lstat()
            except FileNotFoundError:
                pass
            else:
                _seal_snapshot_acl(owned, owned.path / 'source')
        members: list[tuple[Path, tuple[int, ...]]] = []
        for directory_name, directories, files in os.walk(owned.path, followlinks=False):
            for path in (Path(directory_name), *(Path(directory_name) / name for name in (*directories, *files))):
                info = path.lstat()
                if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                        or path.resolve(strict=True) != path
                        or not (stat.S_ISDIR(info.st_mode)
                                or stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
                    raise ValueError('baseline cleanup alias')
                _ownership(path, info, private=False, directory=stat.S_ISDIR(info.st_mode))
                members.append((path, _stamp(info)))
        # Validate the whole owned tree before changing readonly flags; never chmod a hardlink.
        unique = dict(members)
        for path, identity in unique.items():
            if _stamp(path.lstat()) != identity:
                raise ValueError('baseline cleanup changed')
        for path in unique:
            if path != owned.path:
                info = path.lstat()
                path.chmod(0o700 if stat.S_ISDIR(info.st_mode) else 0o600)
        controls.check()
        owned.check()
        owned.close()
        if controls.fd is None:
            shutil.rmtree(owned.path)
        else:
            shutil.rmtree('snapshot', dir_fd=controls.fd)
        controls.sync()

    def _capture_failed(
        self, controls: _Directory, owned: _Directory | None, error: BaseException,
    ) -> None:
        retained = True
        try:
            controls.check()
            try:
                controls.info('baseline.json')
            except FileNotFoundError:
                if owned is not None:
                    self._remove_partial(controls, owned)
                try:
                    controls.info('snapshot')
                except FileNotFoundError:
                    retained = False
        except Exception:  # noqa: BLE001 - uncertainty retains the object and consumed intent.
            retained = True
        status = 'unknown' if retained else 'cancelled' if isinstance(error, asyncio.CancelledError) else 'failed'
        with suppress(Exception):
            self._write_once(controls, 'capture-failure.json', _encoded({
                'schema_version': 1, 'status': status, 'snapshot_retained': retained,
                'cleanup_confirmed': not retained,
            }))

    async def capture_once(
        self, invocation: ProjectInvocation, project_input: ManagedProjectInput,
    ) -> ProjectBaselineReference:
        try:
            invocation = self._invocation(invocation)
            if type(project_input) is not ManagedProjectInput:
                raise ValueError('baseline selection must be typed')
            project_input = ManagedProjectInput.from_payload(project_input.to_payload())
            return await self._capture(invocation, project_input.editable_paths,
                                       self._intent(invocation, project_input))
        except ProjectBaselineError:
            raise
        except Exception:  # noqa: BLE001 - never expose source/host paths or re-capture an uncertain original.
            raise ProjectBaselineError('project original baseline unavailable or unsafe') from None

    async def capture_generated_once(
        self, invocation: ProjectInvocation, *, editable_paths: tuple[str, ...], metadata: Mapping[str, JsonValue],
    ) -> ProjectBaselineReference:
        """Freeze post-model criteria; caller owns user/run/lease authorization."""
        try:
            invocation = self._invocation(invocation)
            paths = _editables(editable_paths)
            generated = _generated_metadata(metadata)
            return await self._capture(invocation, paths, self._generated_intent(invocation, paths, generated),
                                       generated_metadata=generated)
        except ProjectBaselineError:
            raise
        except Exception:  # noqa: BLE001 - no source/host paths or authority inferred from model edit paths.
            raise ProjectBaselineError('generated project baseline unavailable or unsafe') from None

    async def _capture(
        self, invocation: ProjectInvocation, editable_paths: tuple[str, ...], intent: Mapping[str, object],
        *, generated_metadata: Mapping[str, JsonValue] | None = None,
    ) -> ProjectBaselineReference:
        with ExitStack() as stack:
            controls = self._controls(invocation, stack, create=True)
            try:
                self._write_once(controls, 'capture-intent.json', _encoded(intent))
            except FileExistsError:
                raise ProjectBaselineError('project baseline already reserved; never recapture') from None
            owned = None
            try:
                # Reserve space before copying; only this exclusive container is cleanup-owned.
                if shutil.disk_usage(self.root).free <= invocation.source_limit_bytes + _CONTROL_LIMIT:
                    raise ProjectBaselineError('project baseline storage quota unavailable')
                controls.check()
                if controls.fd is None:
                    (controls.path / 'snapshot').mkdir()
                else:
                    os.mkdir('snapshot', mode=0o700, dir_fd=controls.fd)
                owned = _Directory(controls.path / 'snapshot', private=True)
                stack.callback(owned.close)
                controls.sync()
                snapshot = owned.path / 'source'
                digest = await snapshot_project_source(self.root, invocation, destination=snapshot)
                _seal_snapshot_acl(owned, snapshot)
                files = await _scan_snapshot(snapshot, invocation)
                if (not files or digest != _fingerprint(files)
                        or digest != await project_source_fingerprint(self.root, invocation)):
                    raise ProjectBaselineError('project input changed during capture')
                contract = build_project_check_contract(
                    tenant_id=invocation.tenant_id, run_id=invocation.run_id,
                    project_id=invocation.project_id, session_id=invocation.session_id,
                    input_source_sha256=digest, editable_paths=editable_paths, source_files=files,
                )
                payload: dict[str, object] = {'schema_version': 1, 'contract': contract.to_payload(),
                                              'source_files': files}
                if generated_metadata is not None:
                    _validate_generated_snapshot(snapshot, invocation, contract, files, generated_metadata)
                    if digest != await project_source_fingerprint(self.root, invocation):
                        raise ProjectBaselineError('project input changed during capture')
                    payload.update(schema_version=2, purpose='generated_acceptance',
                                   generated_metadata=generated_metadata)
                body = _encoded(payload)
                self._write_once(controls, 'baseline.json', body)
                controls.check()
                return ProjectBaselineReference(hashlib.sha256(body).hexdigest(), digest, len(files))
            except BaseException as error:
                self._capture_failed(controls, owned, error)
                raise

    def load(self, invocation: ProjectInvocation, reference: ProjectBaselineReference) -> StoredProjectBaseline:
        try:
            invocation = self._invocation(invocation, require_workspace=False)
            if type(reference) is not ProjectBaselineReference:
                raise ValueError
            reference = ProjectBaselineReference.from_payload(reference.to_payload())
            with ExitStack() as stack:
                controls = self._controls(invocation, stack, create=False)
                raw = controls.read('baseline.json', _CONTROL_LIMIT)
                if hashlib.sha256(raw).hexdigest() != reference.contract_sha256:
                    raise ValueError
                value = json.loads(raw, object_pairs_hook=_object)
                if type(value) is not dict or type(value.get('schema_version')) is not int:
                    raise ValueError
                metadata = None
                if value['schema_version'] == 1:
                    if set(value) != {'schema_version', 'contract', 'source_files'}:
                        raise ValueError
                elif value['schema_version'] == 2:
                    if (set(value) != {'schema_version', 'purpose', 'generated_metadata', 'contract', 'source_files'}
                            or type(value['purpose']) is not str or value['purpose'] != 'generated_acceptance'
                            or _encoded(value) != raw):
                        raise ValueError
                    metadata = _generated_metadata(value['generated_metadata'])
                else:
                    raise ValueError
                contract = ProjectCheckContract.from_payload(value['contract'])
                intent_raw = controls.read('capture-intent.json', _CONTROL_LIMIT)
                intent = json.loads(intent_raw, object_pairs_hook=_object)
                expected_intent = (self._intent(invocation, ManagedProjectInput(contract.editable_paths))
                                   if metadata is None else self._generated_intent(invocation, contract.editable_paths, metadata))
                if intent != expected_intent or _encoded(expected_intent) != intent_raw:
                    raise ValueError
                if ((contract.tenant_id, contract.run_id, contract.project_id, contract.session_id)
                        != (invocation.tenant_id, invocation.run_id, invocation.project_id, invocation.session_id)
                        or contract.input_source_sha256 != reference.input_source_sha256):
                    raise ValueError
                snapshot = controls.path / 'snapshot' / 'source'
                files = _snapshot_files(snapshot, invocation)
                if (files != value['source_files'] or len(files) != reference.file_count
                        or _fingerprint(files) != reference.input_source_sha256):
                    raise ValueError
                # The baseline includes editable original bytes too; preserve them for diagnosis.
                rebuilt = build_project_check_contract(
                    tenant_id=invocation.tenant_id, run_id=invocation.run_id,
                    project_id=invocation.project_id, session_id=invocation.session_id,
                    input_source_sha256=reference.input_source_sha256,
                    editable_paths=contract.editable_paths, source_files=files,
                )
                if rebuilt.to_payload() != contract.to_payload():
                    raise ValueError
                if metadata is not None:
                    _validate_generated_snapshot(snapshot, invocation, contract, files, metadata)
                controls.check()
                return StoredProjectBaseline(reference, contract, snapshot, files, metadata)
        except Exception:  # noqa: BLE001 - a missing/tampered reference never authorizes a replacement baseline.
            raise ProjectBaselineError('project original baseline unavailable or unsafe') from None
