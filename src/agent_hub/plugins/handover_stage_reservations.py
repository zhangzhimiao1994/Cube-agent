"""Opt-in cooperative, immutable admission for one trusted store domain.

These logical budgets are not filesystem allocations or isolation from another
writer. Every record, including uncertain persistence, retains its charge. There
is no release, recovery, TTL or deletion authority. Call after canonical reuse
and registration/plugin locks, before journal or stage creation, in a worker.
Existing payload trees must be bound to this ledger; legacy stores fail closed.
Only fully validated token-bound journals are accounted; legacy journals refuse admission.
Windows exercises cooperative file semantics only, with no directory durability.
Independent roots on a volume do not form a shared physical capacity reservation.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import get_ident
from typing import Any
from uuid import UUID

from agent_hub.plugins.handover_snapshots import _Directory, _lock, _native_path, _ownership
from agent_hub.plugins.handover_sources import _reject_constant, _unique_object

_NAMESPACE = ".stage-reservations"
_RECORD_BYTES = 16_384
_NODE_BYTES = 4096
_BASE_BYTES = _RECORD_BYTES + 4 * _NODE_BYTES
_BASE_MEMBERS = 4
_OPERATION_NODES = 9
_SCOPE_MEMBERS = 5
_SCOPE_BYTES = 2 * _RECORD_BYTES + _SCOPE_MEMBERS * _NODE_BYTES
# Versioned accounting constants; journal helpers are imported lazily so the
# journal can import the public reservation interfaces without an import cycle.
_MAX_EVENTS = 4096
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_EVENT_BYTES = 1024 * 1024
_PLUGIN = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_STAGE = re.compile(r"tmp-gh-[a-f0-9]{32}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_PUBLICATION: ContextVar[StagePublicationContext | None] = ContextVar(
    "stage_publication", default=None
)


def _available_bytes(directory: _Directory) -> int:
    from agent_hub.plugins.handover_stage_journal import _available_bytes as available

    return available(directory)


def _clock_time() -> datetime:
    from agent_hub.plugins.handover_stage_journal import _clock_time as clock

    return clock()


def _timestamp(now: datetime) -> str:
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _metadata(value: object) -> None:
    from agent_hub.plugins.handover_stage_journal import _metadata as metadata

    metadata(value)


@dataclass(frozen=True, slots=True)
class ReservationPolicy:
    max_bytes: int
    max_members: int
    max_operations: int
    min_available_bytes: int = 128 * 1024 * 1024
    max_ledger_records: int = 4096
    max_ledger_bytes: int = 64 * 1024 * 1024
    max_store_entries: int = 16_384

    def __post_init__(self) -> None:
        for value, minimum, maximum in (
            (self.max_bytes, 1, 2**63 - 1),
            (self.max_members, 1, 2**31 - 1),
            (self.max_operations, 1, 4096),
            (self.min_available_bytes, 0, 2**63 - 1),
            (self.max_ledger_records, 1, 4096),
            (self.max_ledger_bytes, _RECORD_BYTES, 64 * 1024 * 1024),
            (self.max_store_entries, 1, 1_000_000),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("invalid reservation policy")


@dataclass(frozen=True, slots=True)
class ReservationToken:
    operation_id: UUID
    tenant: UUID
    plugin_id: str
    stage_name: str
    target_digest: str
    source_bytes: int
    source_members: int
    charged_bytes: int
    charged_members: int
    created_at: datetime
    policy: ReservationPolicy
    store_root: Path
    root_identity: tuple[int, ...]
    directory_durability: bool
    scope_lock_identity: tuple[int, ...] | None = None
    scope_lock_accounted: bool = False


@dataclass(frozen=True, slots=True)
class ScopeLockReservation:
    """Durable infrastructure facts, not a held lock or payload authority.

    The caller must bind its acquired descriptor and both parents to
    scope_lock_identity before entering publication(). No domain lock survives
    prepare_scope_lock(), so scope -> domain remains the publication lock order.
    """

    tenant: UUID
    plugin_id: str
    lock_path: Path
    scope_lock_identity: tuple[int, ...]
    charged_bytes: int
    charged_members: int
    created_at: datetime
    policy: ReservationPolicy
    store_root: Path
    root_identity: tuple[int, ...]
    directory_durability: bool


@dataclass(frozen=True, slots=True)
class ReservationUsage:
    bytes: int
    members: int
    operations: int


def _identity(info: os.stat_result) -> tuple[int, ...]:
    # Directory nlink changes when trusted publishers add child directories.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (*_identity(info), info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _canonical(value: dict[str, object]) -> bytes:
    _metadata(value)
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _decode(data: bytes) -> dict[str, Any]:
    try:
        result = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        _metadata(result)
    except (ValueError, RecursionError) as error:
        raise RuntimeError("corrupt reservation metadata") from error
    if type(result) is not dict:
        raise RuntimeError("unknown reservation metadata")
    return result


def _sealed(payload: dict[str, object], identity: tuple[int, ...]) -> dict[str, object]:
    body = {**payload, "file_identity": list(identity)}
    if "record_sha256" in body:
        del body["record_sha256"]
        return {**body, "record_sha256": hashlib.sha256(_canonical(body)).hexdigest()}
    return body


def _plugin(value: object) -> bool:
    return (
        type(value) is str
        and bool(_PLUGIN.fullmatch(value))
        and not value.endswith(".")
        and value.split(".")[0]
        not in {
            "con",
            "prn",
            "aux",
            "nul",
            *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10)),
        }
    )


def _uuid(value: object) -> UUID:
    if type(value) is not str:
        raise RuntimeError("invalid reservation UUID")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise RuntimeError("invalid reservation UUID") from error
    if str(parsed) != value:
        raise RuntimeError("reservation UUID alias")
    return parsed


def _utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("reservation deadline must be aware UTC")
    return value


def _charge(source_bytes: int, source_members: int, *, scope_lock: bool = False) -> tuple[int, int]:
    if (
        type(source_bytes) is not int
        or not 0 <= source_bytes <= 2_000_000
        or type(source_members) is not int
        or not 0 < source_members <= 2**31 - 1
    ):
        raise ValueError("invalid reservation source budget")
    # Nine nodes cover payload parents/stage, journal parents/op, record and one
    # shared namespace. A publication lock adds its tenant directory and file.
    members = source_members + _MAX_EVENTS + _OPERATION_NODES + (2 if scope_lock else 0)
    return source_bytes + _MAX_TOTAL_BYTES + _RECORD_BYTES + members * _NODE_BYTES, members


def reservation_reference(token: ReservationToken) -> dict[str, object]:
    """A fresh canonical-JSON-compatible reference, with no storage access.

    This is not token authentication; callers must use validate_token before
    journal creation. The fingerprint hashes canonical JSON of asdict(policy).
    """
    if (
        type(token) is not ReservationToken
        or type(token.policy) is not ReservationPolicy
        or type(token.operation_id) is not UUID
        or type(token.tenant) is not UUID
        or not _plugin(token.plugin_id)
        or type(token.stage_name) is not str
        or not _STAGE.fullmatch(token.stage_name)
        or type(token.target_digest) is not str
        or not _DIGEST.fullmatch(token.target_digest)
        or not isinstance(token.store_root, Path)
        or not token.store_root.is_absolute()
        or ".." in token.store_root.parts
        or type(token.root_identity) is not tuple
        or len(token.root_identity) != 5
        or any(type(value) is not int for value in token.root_identity)
        or type(token.directory_durability) is not bool
    ):
        raise ValueError("invalid reservation token reference")
    if token.scope_lock_identity is not None and (
        type(token.scope_lock_identity) is not tuple
        or len(token.scope_lock_identity) != 15
        or any(type(value) is not int for value in token.scope_lock_identity)
    ):
        raise ValueError("invalid reservation scope lock identity")
    if type(token.scope_lock_accounted) is not bool or (
        token.scope_lock_accounted and token.scope_lock_identity is None
    ):
        raise ValueError("invalid reservation scope accounting")
    charged = _charge(
        token.source_bytes,
        token.source_members,
        scope_lock=token.scope_lock_identity is not None and not token.scope_lock_accounted,
    )
    if (
        type(token.charged_bytes) is not int
        or type(token.charged_members) is not int
        or charged != (token.charged_bytes, token.charged_members)
    ):
        raise ValueError("reservation token charge differs")
    _utc(token.created_at)
    reference: dict[str, object] = {
        "schema_version": 1,
        "operation_id": str(token.operation_id),
        "tenant": str(token.tenant),
        "plugin_id": token.plugin_id,
        "stage_name": token.stage_name,
        "target_digest": token.target_digest,
        "source_bytes": token.source_bytes,
        "source_members": token.source_members,
        "charged_bytes": token.charged_bytes,
        "charged_members": token.charged_members,
        "created_at": _timestamp(token.created_at),
        "store_root": str(token.store_root),
        "root_identity": list(token.root_identity),
        "policy_fingerprint": hashlib.sha256(
            json.dumps(
                asdict(token.policy),
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest(),
        "directory_durability": token.directory_durability,
        "scope_lock_identity": (
            list(token.scope_lock_identity) if token.scope_lock_identity is not None else None
        ),
    }
    if token.scope_lock_accounted:
        reference["scope_lock_accounted"] = True
    return reference


def _file(directory: _Directory, name: str, fd: int) -> os.stat_result:
    directory.check()
    opened, entry = os.fstat(fd), directory.info(name)
    if (
        _identity(opened) != _identity(entry)
        or opened.st_size != entry.st_size
        or entry.st_nlink != 1
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or getattr(opened, "st_file_attributes", 0) & 0x400
    ):
        raise RuntimeError("reservation file identity changed or has an alias")
    _ownership(directory.path / name, opened, private=True, directory=False)
    return opened


class _LockedDirectory(_Directory):
    """Add entry/descriptor binding checks around the existing native lock helper."""

    def __init__(self, path: Path, *, create_lock: bool = True) -> None:
        self.lock_fd: int | None = None
        self.lock_identity: tuple[int, ...] | None = None
        self.create_lock = create_lock
        super().__init__(path, private=True)

    def open(self, name: str, flags: int) -> int:
        if name == "lock" and not self.create_lock:
            flags &= ~os.O_CREAT
        fd = super().open(name, flags)
        if name == "lock":
            try:
                opened = _file(self, name, fd)
                if opened.st_size not in (0, 1):
                    raise RuntimeError("reservation lock is corrupt")
                self.lock_identity = _identity(opened)
                self.lock_fd = fd
            except BaseException:
                os.close(fd)
                raise
        return fd

    def check(self) -> None:
        super().check()
        if self.lock_fd is not None:
            opened = os.fstat(self.lock_fd)
            entry = os.stat(
                _native_path(self.path / "lock") if self.fd is None else "lock",
                dir_fd=self.fd,
                follow_symlinks=False,
            )
            if (
                _stamp(opened) != _stamp(entry)
                or opened.st_nlink != 1
                or _identity(opened) != self.lock_identity
                or opened.st_size not in (0, 1)
            ):
                raise RuntimeError("reservation lock identity changed or has an alias")
            _ownership(self.path / "lock", opened, private=True, directory=False)


class StageReservationStore:
    def __init__(self, store_root: Path, policy: ReservationPolicy) -> None:
        if (
            not isinstance(store_root, Path)
            or not store_root.is_absolute()
            or ".." in store_root.parts
            or type(policy) is not ReservationPolicy
            or store_root == Path(store_root.anchor)
        ):
            raise ValueError("invalid reservation store binding")
        with contextlib.ExitStack() as stack:
            directory = _Directory(store_root, private=False)
            stack.callback(directory.close)
            canonical = Path(os.path.normcase(os.path.abspath(store_root)))
            if canonical != Path(os.path.normcase(str(store_root.resolve(strict=True)))):
                raise ValueError("reservation store root alias")
            self._root = canonical
            self._policy = policy
            self._root_identity = _identity(_native_path(canonical).lstat())
            self._root_chain = directory.chain

    @property
    def store_root(self) -> Path:
        return self._root

    @contextlib.contextmanager
    def publication(
        self, *, tenant: UUID, plugin_id: str, initialize: bool = True
    ) -> Iterator[StagePublicationContext]:
        """Hold the domain after plugin locks, through reserve, publication and close.

        Use the yielded methods and pass it to StagePublicationJournal. Ordinary
        store methods and nested publication contexts must not reacquire this lock.
        Payload parents must be created only after admission, inside this context.
        """
        if type(tenant) is not UUID or not _plugin(plugin_id) or type(initialize) is not bool:
            raise ValueError("invalid publication context scope")
        with self._domain((tenant, plugin_id), initialize=initialize) as (root, directory):
            publication = StagePublicationContext(self, root, directory, tenant, plugin_id)
            marker = _PUBLICATION.set(publication)
            try:
                yield publication
                publication._check()
            finally:
                publication._active = False
                _PUBLICATION.reset(marker)

    @contextlib.contextmanager
    def _access(
        self,
        publication: StagePublicationContext | None,
        incoming_scope: tuple[UUID, str] | None = None,
        *,
        initialize: bool = True,
    ) -> Iterator[tuple[_Directory, _LockedDirectory]]:
        if publication is None:
            with self._domain(incoming_scope, initialize=initialize) as directories:
                yield directories
        else:
            publication._check()
            if publication._store is not self or (
                incoming_scope is not None and incoming_scope != publication._scope
            ):
                raise RuntimeError("publication context store/scope differs")
            yield publication._root, publication._directory
            publication._check()

    def _root_check(self, directory: _Directory) -> None:
        directory.check()
        if (
            directory.chain != self._root_chain
            or _identity(_native_path(self._root).lstat()) != self._root_identity
        ):
            raise RuntimeError("reservation store root identity changed")

    def _binding(self, directory: _LockedDirectory) -> dict[str, object]:
        return {
            "schema_version": 1,
            "store_root": str(self._root),
            "root_identity": list(self._root_identity),
            "root_chain": [list(node) for node in self._root_chain],
            "ledger_identity": list(_identity(_native_path(directory.path).lstat())),
            "lock_identity": list(directory.lock_identity or ()),
            "policy": asdict(self._policy),
            "directory_durability": sys.platform != "win32",
        }

    @contextlib.contextmanager
    def _domain(
        self, incoming_scope: tuple[UUID, str] | None = None, *, initialize: bool = True
    ) -> Iterator[tuple[_Directory, _LockedDirectory]]:
        if _PUBLICATION.get() is not None:
            raise RuntimeError("nested domain lock; use the active publication context methods")
        with contextlib.ExitStack() as stack:
            root = _Directory(self._root, private=False)
            stack.callback(root.close)
            self._root_check(root)
            # Unknown legacy stages/journals must not acquire an initialized domain.
            if initialize:
                if not _native_path(self._root / _NAMESPACE).exists():
                    self._store_scan(root, [], incoming_scope)
                child = root.child(_NAMESPACE)
            else:
                child = _Directory(self._root / _NAMESPACE, private=True)
            stack.callback(child.close)
            root.sync()
            directory = _LockedDirectory(child.path, create_lock=initialize)
            stack.callback(directory.close)
            try:
                with _lock(directory):
                    self._root_check(root)
                    if _identity(directory.path.lstat())[0] != self._root_identity[0]:
                        raise RuntimeError("reservation volume binding changed")
                    yield root, directory
                    directory.check()
                    self._root_check(root)
            finally:
                # The shared helper owns and closes the descriptor.
                directory.lock_fd = None

    def _names(self, directory: _Directory, limit: int) -> tuple[str, ...]:
        directory.check()
        names: list[str] = []
        with os.scandir(
            _native_path(directory.path) if directory.fd is None else directory.fd
        ) as it:
            for entry in it:
                names.append(entry.name)
                if len(names) > limit:
                    raise RuntimeError("reservation scan limit exceeded")
        directory.check()
        return tuple(sorted(names))

    def _write(self, directory: _LockedDirectory, name: str, payload: dict[str, object]) -> None:
        fd = directory.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            before = _file(directory, name, fd)
            if before.st_size != 0:
                raise RuntimeError("reservation exclusive record is not empty")
            data = _canonical(_sealed(payload, _identity(before)))
            if len(data) > _RECORD_BYTES:
                raise RuntimeError("reservation record byte limit exceeded")
            if os.write(fd, data) != len(data):
                raise RuntimeError("reservation short write; record retained")
            written = _file(directory, name, fd)
            if written.st_size != len(data) or _identity(written) != _identity(before):
                raise RuntimeError("reservation record identity changed during write")
            os.fsync(fd)
            if _stamp(_file(directory, name, fd)) != _stamp(written):
                raise RuntimeError("reservation record changed during fsync")
            directory.sync()
            if _stamp(_file(directory, name, fd)) != _stamp(written):
                raise RuntimeError("reservation record changed during parent sync")
            if directory.read(name, _RECORD_BYTES) != data:
                raise RuntimeError("reservation record changed during persistence")
        finally:
            os.close(fd)

    def _payload(self, token: ReservationToken, binding: dict[str, object]) -> dict[str, object]:
        payload = {
            **binding,
            "operation_id": str(token.operation_id),
            "tenant": str(token.tenant),
            "plugin_id": token.plugin_id,
            "stage_name": token.stage_name,
            "target_digest": token.target_digest,
            "source_bytes": token.source_bytes,
            "source_members": token.source_members,
            "charged_bytes": token.charged_bytes,
            "charged_members": token.charged_members,
            "created_at": _timestamp(token.created_at),
        }
        if token.scope_lock_identity is not None:
            payload["scope_lock_identity"] = list(token.scope_lock_identity)
        if token.scope_lock_accounted:
            payload["scope_lock_accounted"] = True
        return {**payload, "record_sha256": hashlib.sha256(_canonical(payload)).hexdigest()}

    def _token(
        self, data: bytes, name: str, binding: dict[str, object], identity: tuple[int, ...]
    ) -> ReservationToken:
        payload = _decode(data)
        try:
            operation, tenant = _uuid(payload["operation_id"]), _uuid(payload["tenant"])
            created = _utc(datetime.fromisoformat(payload["created_at"]))
            lock_binding = payload.get("scope_lock_identity")
            accounted = payload.get("scope_lock_accounted", False)
            if type(accounted) is not bool or (accounted and lock_binding is None):
                raise ValueError("invalid record scope accounting")
            if lock_binding is not None and (
                type(lock_binding) is not list
                or len(lock_binding) != 15
                or any(type(value) is not int for value in lock_binding)
            ):
                raise ValueError("invalid record scope lock identity")
            charged_bytes, charged_members = _charge(
                payload["source_bytes"],
                payload["source_members"],
                scope_lock=lock_binding is not None and not accounted,
            )
            if (
                name != str(operation) + ".json"
                or not _plugin(payload["plugin_id"])
                or type(payload["stage_name"]) is not str
                or not _STAGE.fullmatch(payload["stage_name"])
                or type(payload["target_digest"]) is not str
                or not _DIGEST.fullmatch(payload["target_digest"])
            ):
                raise ValueError("invalid record scope")
            token = ReservationToken(
                operation,
                tenant,
                payload["plugin_id"],
                payload["stage_name"],
                payload["target_digest"],
                payload["source_bytes"],
                payload["source_members"],
                charged_bytes,
                charged_members,
                created,
                self._policy,
                self._root,
                self._root_identity,
                sys.platform != "win32",
                tuple(lock_binding) if lock_binding is not None else None,
                accounted,
            )
            if data != _canonical(_sealed(self._payload(token, binding), identity)):
                raise RuntimeError("reservation record scope/policy binding or checksum differs")
            return token
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("corrupt reservation record binding") from error

    def _ledger(
        self,
        root: _Directory,
        directory: _LockedDirectory,
        incoming_scope: tuple[UUID, str] | None = None,
        *,
        initialize: bool = True,
    ) -> tuple[list[ReservationToken], list[ScopeLockReservation]]:
        names = self._names(directory, self._policy.max_ledger_records + 2)
        record_names = [name for name in names if name not in {"lock", "domain.json"}]
        if len(record_names) > self._policy.max_ledger_records:
            raise RuntimeError("reservation ledger scan limit exceeded")
        binding = self._binding(directory)
        if "domain.json" not in names:
            if record_names or not initialize:
                raise RuntimeError("unknown partial reservation domain")
            self._store_scan(root, [], incoming_scope)
            self._write(directory, "domain.json", binding)
        if directory.read("domain.json", _RECORD_BYTES) != _canonical(
            _sealed(binding, _identity(directory.info("domain.json")))
        ):
            raise RuntimeError("reservation domain policy/root/lock identity binding differs")
        # A prior failed fsync can leave a complete manifest. Establish its durability
        # again without recovering or returning any reservation token from disk.
        fd = directory.open("domain.json", os.O_RDWR)
        try:
            before = _stamp(_file(directory, "domain.json", fd))
            os.fsync(fd)
            directory.sync()
            if _stamp(_file(directory, "domain.json", fd)) != before:
                raise RuntimeError("reservation domain changed during sync")
        finally:
            os.close(fd)
        tokens: list[ReservationToken] = []
        scope_records: dict[str, tuple[bytes, tuple[int, ...]]] = {}
        stages: set[tuple[UUID, str, str]] = set()
        seen: set[tuple[int, ...]] = set()
        stamps: dict[str, tuple[int, ...]] = {}
        total = directory.info("domain.json").st_size
        for name in record_names:
            if not name.endswith(".json"):
                raise RuntimeError("unknown reservation ledger object")
            info = directory.info(name)
            total += info.st_size
            if total > self._policy.max_ledger_bytes:
                raise RuntimeError("reservation ledger byte scan limit exceeded")
            node = _identity(info)
            stamps[name] = _stamp(info)
            if node in seen:
                raise RuntimeError("duplicate reservation file identity")
            seen.add(node)
            data = directory.read(name, _RECORD_BYTES)
            if name.startswith("scope-"):
                scope_records[name] = (data, node)
                continue
            _uuid(name[:-5])
            token = self._token(data, name, binding, node)
            stage = (token.tenant, token.plugin_id, token.stage_name)
            if stage in stages:
                raise RuntimeError("duplicate reservation stage")
            stages.add(stage)
            tokens.append(token)
        scopes: list[ScopeLockReservation] = []
        while scope_records:
            name = next(iter(scope_records))
            if name.startswith("scope-binding-"):
                name = "scope-" + name.removeprefix("scope-binding-")
            bound_name = "scope-binding-" + name.removeprefix("scope-")
            if name not in scope_records or bound_name not in scope_records:
                raise RuntimeError("unknown partial scope infrastructure reservation retained")
            data, node = scope_records.pop(name)
            bound_data, bound_node = scope_records.pop(bound_name)
            scope = self._scope_record(data, node, bound_data, bound_node, binding, name)
            if any((s.tenant, s.plugin_id) == (scope.tenant, scope.plugin_id) for s in scopes):
                raise RuntimeError("duplicate scope infrastructure reservation")
            scopes.append(scope)
        self._store_scan(root, tokens, incoming_scope, infrastructure=scopes)
        if set(self._names(directory, self._policy.max_ledger_records + 2)) != set(names) | {
            "domain.json"
        } or any(_stamp(directory.info(name)) != stamp for name, stamp in stamps.items()):
            raise RuntimeError("reservation ledger changed during full scan")
        self._root_check(root)
        directory.check()
        return tokens, scopes

    def _scope_name(self, tenant: UUID, plugin_id: str) -> str:
        return f"scope-{tenant}-{plugin_id}.json"

    def _scope_payload(
        self,
        scope: ScopeLockReservation,
        binding: dict[str, object],
        *,
        intent_identity: tuple[int, ...] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            **binding,
            "kind": "scope_intent" if intent_identity is None else "scope_binding",
            "tenant": str(scope.tenant),
            "plugin_id": scope.plugin_id,
            "charged_bytes": _SCOPE_BYTES,
            "charged_members": _SCOPE_MEMBERS,
            "created_at": _timestamp(scope.created_at),
        }
        if intent_identity is not None:
            payload["intent_identity"] = list(intent_identity)
            payload["scope_lock_identity"] = list(scope.scope_lock_identity)
        return {**payload, "record_sha256": hashlib.sha256(_canonical(payload)).hexdigest()}

    def _scope_record(
        self,
        data: bytes,
        node: tuple[int, ...],
        bound_data: bytes,
        bound_node: tuple[int, ...],
        binding: dict[str, object],
        name: str,
    ) -> ScopeLockReservation:
        intent, bound = _decode(data), _decode(bound_data)
        try:
            tenant, plugin = _uuid(intent["tenant"]), intent["plugin_id"]
            identities = bound["scope_lock_identity"]
            if (
                not _plugin(plugin)
                or name != self._scope_name(tenant, plugin)
                or type(identities) is not list
                or len(identities) != 15
                or any(type(value) is not int for value in identities)
            ):
                raise ValueError("invalid scope infrastructure binding")
            scope = ScopeLockReservation(
                tenant,
                plugin,
                self._root / ".uninstall-locks" / str(tenant) / (plugin + ".lock"),
                tuple(identities),
                _SCOPE_BYTES,
                _SCOPE_MEMBERS,
                _utc(datetime.fromisoformat(intent["created_at"])),
                self._policy,
                self._root,
                self._root_identity,
                sys.platform != "win32",
            )
            if data != _canonical(_sealed(self._scope_payload(scope, binding), node)) or (
                bound_data
                != _canonical(
                    _sealed(
                        self._scope_payload(scope, binding, intent_identity=node),
                        bound_node,
                    )
                )
            ):
                raise RuntimeError("scope infrastructure metadata/policy/identity binding differs")
            return scope
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("corrupt scope infrastructure reservation binding") from error

    def _scope_lock(
        self,
        root: _Directory,
        tenant: UUID,
        plugin: str,
        *,
        enrolled: bool = False,
    ) -> tuple[int, ...] | None:
        """Validate, but do not acquire or initialize, the caller's publication lock."""
        namespace = self._root / ".uninstall-locks"
        path = namespace / str(tenant)
        if not _native_path(path / (plugin + ".lock")).exists():
            return None
        with contextlib.ExitStack() as stack:
            identities: list[int] = []
            for parent in (namespace, path):
                directory = _Directory(parent, private=False)
                stack.callback(directory.close)
                info = _native_path(parent).lstat()
                if info.st_dev != self._root_identity[0] or info.st_nlink < 1:
                    raise RuntimeError("publication lock directory volume/identity differs")
                identities.extend(_identity(info))
            name = plugin + ".lock"
            before = directory.info(name)
            if before.st_dev != self._root_identity[0] or (enrolled and before.st_size != 1):
                raise RuntimeError("publication lock volume differs")
            if sys.platform == "win32":
                # The caller holds byte zero. Windows rejects reads of that byte,
                # even through another descriptor in the owning process.
                fd = directory.open(name, os.O_RDONLY)
                try:
                    opened = _file(directory, name, fd)
                    if opened.st_size not in (0, 1) or opened.st_size != before.st_size:
                        raise RuntimeError("publication lock is unsafe or changed")
                    # Windows path/descriptor timestamps can differ while the
                    # writer's handle is open; compare stamps within each API.
                    if _stamp(_file(directory, name, fd)) != _stamp(opened):
                        raise RuntimeError("publication lock is unsafe or changed")
                finally:
                    os.close(fd)
            else:
                data = directory.read(name, 1)
                if data not in (b"", b"\0"):
                    raise RuntimeError("publication lock is unsafe or changed")
            if _stamp(directory.info(name)) != _stamp(before):
                raise RuntimeError("publication lock is unsafe or changed")
            identities.extend(_identity(before))
            self._root_check(root)
        return tuple(identities)

    def _journal_operation(
        self,
        directory: _Directory,
        token: ReservationToken,
        bindings: dict[str, _Directory],
        event_names: tuple[str, ...],
    ) -> None:
        if not event_names or len(event_names) > _MAX_EVENTS:
            raise RuntimeError("unknown partial publication journal or event limit")
        dynamic = {"ordinal", "state", "details", "recorded_at"}
        fixed = {
            "schema_version",
            "created_at",
            "operation_id",
            "tenant",
            "plugin_id",
            "stage_name",
            "target_digest",
            "reserved_bytes",
            "reserved_members",
            "member_budget",
            "directory_durability",
            "bindings",
            "reservation",
        }
        expected: dict[str, object] = {
            "schema_version": 1,
            "operation_id": str(token.operation_id),
            "tenant": str(token.tenant),
            "plugin_id": token.plugin_id,
            "stage_name": token.stage_name,
            "target_digest": token.target_digest,
            "reserved_bytes": token.source_bytes,
            "reserved_members": token.source_members,
            "directory_durability": token.directory_durability,
        }
        prefix: bytes | None = None
        total = 0
        phase = ""
        previous: datetime | None = None
        stamps: dict[str, tuple[int, ...]] = {}

        def utc_text(value: object) -> datetime:
            if type(value) is not str:
                raise RuntimeError("invalid journal UTC timestamp")
            try:
                parsed = _utc(datetime.fromisoformat(value))
            except (ValueError, TypeError) as error:
                raise RuntimeError("invalid journal UTC timestamp") from error
            if _timestamp(parsed) != value:
                raise RuntimeError("noncanonical journal UTC timestamp")
            return parsed

        for ordinal, name in enumerate(event_names, 1):
            if name != f"{ordinal:06d}.json":
                raise RuntimeError("unknown journal entry or discontinuous ordinal")
            info = directory.info(name)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_dev != self._root_identity[0]
                or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise RuntimeError("journal event alias or volume differs")
            total += info.st_size
            if total > _MAX_TOTAL_BYTES:
                raise RuntimeError("journal aggregate byte limit exceeded")
            data = directory.read(name, _MAX_EVENT_BYTES)
            stamps[name] = _stamp(info)
            event = _decode(data)
            if data != _canonical(event) or event.keys() != fixed | dynamic:
                raise RuntimeError("unknown or noncanonical journal metadata")
            if any(type(event[k]) is not type(v) or event[k] != v for k, v in expected.items()):
                raise RuntimeError("journal reservation scope/count binding differs")
            if _canonical({"reservation": event["reservation"]}) != _canonical(
                {"reservation": reservation_reference(token)}
            ):
                raise RuntimeError("journal reservation reference differs")
            member_budget = event["member_budget"]
            if (
                type(member_budget) is not int
                or not token.source_members <= member_budget <= 2**31 - 1
            ):
                raise RuntimeError("journal member budget differs")
            recorded_bindings = event["bindings"]
            if type(recorded_bindings) is not dict or recorded_bindings.keys() != bindings.keys():
                raise RuntimeError("journal directory bindings differ")
            for label, parent in bindings.items():
                parent.check()
                facts = recorded_bindings[label]
                if type(facts) is not dict or facts.keys() != {
                    "path",
                    "dev",
                    "ino",
                    "mode",
                    "uid",
                    "gid",
                    "nlink",
                }:
                    raise RuntimeError("unknown journal directory binding")
                # Compare text only; never open a path supplied by metadata.
                if type(facts["path"]) is not str or os.path.normcase(
                    facts["path"]
                ) != os.path.normcase(str(parent.path)):
                    raise RuntimeError("journal directory path binding differs")
                current = _native_path(parent.path).lstat()
                expected_facts = dict(
                    zip(("dev", "ino", "mode", "uid", "gid"), _identity(current), strict=True)
                )
                if any(
                    type(facts[k]) is not int or facts[k] != v for k, v in expected_facts.items()
                ):
                    raise RuntimeError("journal directory node binding differs")
                if (
                    type(facts["nlink"]) is not int
                    or facts["nlink"] < 1
                    or current.st_nlink < 1
                    or (label == "operation" and facts["nlink"] != current.st_nlink)
                ):
                    raise RuntimeError("journal directory link binding differs")
            created = utc_text(event["created_at"])
            recorded = utc_text(event["recorded_at"])
            if (
                created < token.created_at
                or recorded < created
                or (previous is not None and recorded < previous)
            ):
                raise RuntimeError("journal clock/order differs")
            previous = recorded
            current_prefix = _canonical({k: event[k] for k in fixed})
            if prefix is not None and prefix != current_prefix:
                raise RuntimeError("journal fixed prefix differs")
            prefix = current_prefix
            if type(event["ordinal"]) is not int or event["ordinal"] != ordinal:
                raise RuntimeError("journal ordinal differs")
            if type(event["details"]) is not dict or type(event["state"]) is not str:
                raise RuntimeError("unknown journal state/details")
            state = event["state"]
            allowed = {
                "": {"creating"},
                "creating": {"recording", "ready"},
                "ready": {"rename_attempted"},
                "rename_attempted": {"published"},
                "published": set(),
            }
            if state not in allowed[phase]:
                raise RuntimeError("invalid or unknown journal state transition")
            if state != "recording":
                phase = state
        if self._names(directory, _MAX_EVENTS) != event_names or any(
            _stamp(directory.info(name)) != stamp for name, stamp in stamps.items()
        ):
            raise RuntimeError("publication journal changed during scan")

    def _store_scan(
        self,
        root: _Directory,
        tokens: list[ReservationToken],
        incoming_scope: tuple[UUID, str] | None = None,
        *,
        infrastructure: list[ScopeLockReservation] | None = None,
    ) -> None:
        """Validate all accounted metadata/payloads; allow only empty incoming parents."""
        remaining = self._policy.max_store_entries
        seen: set[tuple[int, ...]] = set()
        directory_paths: dict[tuple[int, ...], Path] = {}

        @contextlib.contextmanager
        def child(parent: _Directory, name: str, *, private: bool = False) -> Iterator[_Directory]:
            info = parent.info(name)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_dev != self._root_identity[0]
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise RuntimeError("reservation store directory alias/volume differs")
            node = _identity(info)
            path = parent.path / name
            if node in seen and directory_paths.get(node) != path:
                raise RuntimeError("reservation store directory alias")
            seen.add(node)
            directory_paths[node] = path
            directory = _Directory(path, private=private)
            try:
                if _identity(directory.path.lstat()) != node:
                    raise RuntimeError("reservation store directory rebound")
                yield directory
                directory.check()
                if _identity(parent.info(name)) != node:
                    raise RuntimeError("reservation store directory rebound")
            finally:
                directory.close()

        def names(directory: _Directory) -> tuple[str, ...]:
            nonlocal remaining
            result = self._names(directory, remaining)
            remaining -= len(result)
            return result

        def payload_tree(
            directory: _Directory, budgets: list[ReservationToken], depth: int = 0
        ) -> tuple[int, int]:
            if depth > 32:
                raise RuntimeError("reservation store depth scan limit exceeded")
            count, size = 0, 0
            for name in names(directory):
                info = directory.info(name)
                count += 1
                if stat.S_ISDIR(info.st_mode):
                    with child(directory, name) as nested:
                        subcount, subsize = payload_tree(nested, budgets, depth + 1)
                    count += subcount
                    size += subsize
                else:
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or info.st_dev != self._root_identity[0]
                        or getattr(info, "st_file_attributes", 0) & 0x400
                    ):
                        raise RuntimeError(
                            "reservation payload contains an alias or unknown object"
                        )
                    _ownership(directory.path / name, info, private=False, directory=False)
                    node = _identity(info)
                    if node in seen:
                        raise RuntimeError("reservation payload file alias")
                    seen.add(node)
                    size += info.st_size
            # Source counts include the payload root. Nested roots have already
            # been counted as entries by their parent, so add only the outer root.
            accounted_count = count + (1 if depth == 0 else 0)
            if not any(
                accounted_count <= t.source_members and size <= t.source_bytes for t in budgets
            ):
                raise RuntimeError("reservation payload exceeds accounted source budget")
            return count, size

        registered = {(s.tenant, s.plugin_id): s for s in (infrastructure or [])}
        scopes = {(token.tenant, token.plugin_id) for token in tokens} | registered.keys()
        if incoming_scope is not None:
            scopes.add(incoming_scope)
        by_operation = {token.operation_id: token for token in tokens}

        def journal_tree(namespace: _Directory) -> None:
            tenants = names(namespace)
            if not tenants:
                raise RuntimeError("unknown partial journal namespace")
            for tenant_name in tenants:
                tenant = _uuid(tenant_name)
                with child(namespace, tenant_name, private=True) as tenant_dir:
                    plugins = names(tenant_dir)
                    if not plugins:
                        raise RuntimeError("unknown partial journal tenant")
                    for plugin in plugins:
                        if (tenant, plugin) not in {(t.tenant, t.plugin_id) for t in tokens}:
                            raise RuntimeError("unaccounted journal tenant/plugin scope")
                        with child(tenant_dir, plugin, private=True) as plugin_dir:
                            operations = names(plugin_dir)
                            if not operations:
                                raise RuntimeError("unknown partial journal plugin")
                            for operation_name in operations:
                                token = by_operation.get(_uuid(operation_name))
                                if token is None or (token.tenant, token.plugin_id) != (
                                    tenant,
                                    plugin,
                                ):
                                    raise RuntimeError("unaccounted journal operation scope")
                                with (
                                    child(plugin_dir, operation_name, private=True) as operation,
                                    child(root, str(tenant)) as payload_tenant,
                                    child(payload_tenant, plugin) as payload_plugin,
                                ):
                                    directories = {
                                        "store": root,
                                        "namespace": namespace,
                                        "tenant_parent": payload_tenant,
                                        "plugin_parent": payload_plugin,
                                        "ledger_tenant": tenant_dir,
                                        "ledger_plugin": plugin_dir,
                                        "operation": operation,
                                    }
                                    self._journal_operation(
                                        operation, token, directories, names(operation)
                                    )

        def lock_tree(namespace: _Directory) -> None:
            tenants = names(namespace)
            if not tenants:
                raise RuntimeError("unknown partial publication lock namespace")
            for tenant_name in tenants:
                tenant = _uuid(tenant_name)
                if not any(scope[0] == tenant for scope in scopes):
                    raise RuntimeError("unaccounted publication lock tenant")
                with child(namespace, tenant_name) as tenant_dir:
                    leaves = names(tenant_dir)
                    if not leaves:
                        raise RuntimeError("unknown partial publication lock tenant")
                    for name in leaves:
                        plugin = name[:-5] if name.endswith(".lock") else ""
                        scope = (tenant, plugin)
                        if not _plugin(plugin) or scope not in scopes:
                            raise RuntimeError("unaccounted publication lock scope")
                        identity = self._scope_lock(
                            root, tenant, plugin, enrolled=scope in registered
                        )
                        if identity is None:
                            raise RuntimeError("publication lock disappeared")
                        issued = [t for t in tokens if (t.tenant, t.plugin_id) == scope]
                        if scope in registered:
                            if registered[scope].scope_lock_identity != identity:
                                raise RuntimeError("scope infrastructure publication lock rebound")
                        elif scope != incoming_scope and not any(
                            t.scope_lock_identity == identity for t in issued
                        ):
                            raise RuntimeError("unaccounted or rebound historical publication lock")

        for tenant_name in names(root):
            if tenant_name == _NAMESPACE:
                continue
            if tenant_name == ".stage-ownership":
                try:
                    with child(root, tenant_name, private=True) as namespace:
                        journal_tree(namespace)
                except FileNotFoundError:
                    raise RuntimeError(
                        "unaccounted partial journal: trusted parent/event is missing"
                    ) from None
                continue
            if tenant_name == ".uninstall-locks":
                with child(root, tenant_name) as namespace:
                    lock_tree(namespace)
                continue
            try:
                tenant = _uuid(tenant_name)
            except RuntimeError:
                raise RuntimeError("unaccounted unknown store object") from None
            tenant_tokens = [t for t in tokens if t.tenant == tenant]
            if not tenant_tokens and (incoming_scope is None or tenant != incoming_scope[0]):
                raise RuntimeError("unaccounted tenant store directory")
            with child(root, tenant_name) as tenant_dir:
                for plugin in names(tenant_dir):
                    plugin_tokens = [t for t in tenant_tokens if t.plugin_id == plugin]
                    if not plugin_tokens and incoming_scope != (tenant, plugin):
                        raise RuntimeError("unaccounted plugin store directory")
                    with child(tenant_dir, plugin) as plugin_dir:
                        leaves = names(plugin_dir)
                        if any(
                            t.stage_name in leaves and t.target_digest in leaves
                            for t in plugin_tokens
                        ):
                            raise RuntimeError("duplicate stage/canonical source charge")
                        for leaf in leaves:
                            budgets = [
                                t for t in plugin_tokens if leaf in (t.stage_name, t.target_digest)
                            ]
                            if not budgets:
                                raise RuntimeError("unaccounted stage or canonical object")
                            with child(plugin_dir, leaf) as tree:
                                payload_tree(tree, budgets)
        for token in tokens:
            if token.scope_lock_accounted and (
                (token.tenant, token.plugin_id) not in registered
                or registered[(token.tenant, token.plugin_id)].scope_lock_identity
                != token.scope_lock_identity
            ):
                raise RuntimeError("reservation scope infrastructure binding differs")
            if (
                token.scope_lock_identity is not None
                and self._scope_lock(root, token.tenant, token.plugin_id)
                != token.scope_lock_identity
            ):
                raise RuntimeError("historical publication lock identity changed")
        for scope in registered.values():
            if (
                self._scope_lock(root, scope.tenant, scope.plugin_id, enrolled=True)
                != scope.scope_lock_identity
            ):
                raise RuntimeError("scope infrastructure lock or parents changed")

    def _usage(
        self,
        tokens: list[ReservationToken],
        scopes: list[ScopeLockReservation],
    ) -> ReservationUsage:
        return ReservationUsage(
            _BASE_BYTES
            + sum(t.charged_bytes for t in tokens)
            + sum(s.charged_bytes for s in scopes),
            _BASE_MEMBERS
            + sum(t.charged_members for t in tokens)
            + sum(s.charged_members for s in scopes),
            len(tokens),
        )

    def usage(self, *, _publication: StagePublicationContext | None = None) -> ReservationUsage:
        """Read and validate the entire retained ledger; never recover an issued token."""
        with self._access(_publication) as (root, directory):
            result = self._usage(*self._ledger(root, directory))
        return result

    def validate_token(
        self, token: ReservationToken, *, _publication: StagePublicationContext | None = None
    ) -> None:
        """Validate only a caller-held token against the complete durable ledger.

        Never initialize a missing domain or recover/return another token.
        This requires no spare quota or current free-space watermark.
        """
        reference = reservation_reference(token)
        if (
            str(token.store_root) != str(self._root)
            or token.root_identity != self._root_identity
            or token.policy != self._policy
            or token.directory_durability is not (sys.platform != "win32")
        ):
            raise RuntimeError("reservation held token root/policy/identity binding differs")
        with self._access(_publication, initialize=False) as (root, directory):
            tokens, _ = self._ledger(root, directory, initialize=False)
            matched = [saved for saved in tokens if saved.operation_id == token.operation_id]
            if (
                len(matched) != 1
                or matched[0] != token
                or _canonical(reservation_reference(matched[0])) != _canonical(reference)
            ):
                raise RuntimeError("reservation held token differs from durable record")

    def reserve(
        self,
        *,
        operation_id: UUID,
        tenant: UUID,
        plugin_id: str,
        stage_name: str,
        target_digest: str,
        source_bytes: int,
        source_members: int,
        deadline: datetime,
        revalidate: Callable[[], bool],
        _publication: StagePublicationContext | None = None,
    ) -> ReservationToken:
        if (
            type(operation_id) is not UUID
            or type(tenant) is not UUID
            or not _plugin(plugin_id)
            or type(stage_name) is not str
            or not _STAGE.fullmatch(stage_name)
            or type(target_digest) is not str
            or not _DIGEST.fullmatch(target_digest)
            or not callable(revalidate)
        ):
            raise ValueError("invalid reservation request binding")
        _utc(deadline)
        charged_bytes, charged_members = _charge(source_bytes, source_members)

        def live() -> datetime:
            now = _clock_time()
            if now >= deadline:
                raise RuntimeError("reservation request deadline expired")
            if revalidate() is not True:
                raise RuntimeError("reservation request is no longer valid")
            now = _clock_time()
            if now >= deadline:
                raise RuntimeError("reservation request deadline expired")
            return now

        live()
        incoming_scope = (tenant, plugin_id)
        with self._access(_publication, incoming_scope) as (root, directory):
            live()
            tokens, scopes = self._ledger(root, directory, incoming_scope)
            scope_lock_identity = self._scope_lock(root, tenant, plugin_id)
            scope_lock_accounted = any((s.tenant, s.plugin_id) == incoming_scope for s in scopes)
            charged_bytes, charged_members = _charge(
                source_bytes,
                source_members,
                scope_lock=scope_lock_identity is not None and not scope_lock_accounted,
            )
            if any(
                t.operation_id == operation_id
                or (t.tenant, t.plugin_id, t.stage_name) == (tenant, plugin_id, stage_name)
                for t in tokens
            ):
                raise RuntimeError("duplicate reservation operation or stage")
            if len(tokens) + 2 * len(scopes) >= self._policy.max_ledger_records:
                raise RuntimeError("reservation ledger record scan limit reached")
            usage = self._usage(tokens, scopes)
            required = usage.bytes + charged_bytes
            if (
                required > self._policy.max_bytes
                or usage.members + charged_members > self._policy.max_members
                or usage.operations + 1 > self._policy.max_operations
            ):
                raise RuntimeError("reservation aggregate budget exceeded")
            self._root_check(root)
            available = _available_bytes(root)
            self._root_check(root)
            if (
                type(available) is not int
                or available < self._policy.min_available_bytes + required
            ):
                raise RuntimeError("reservation available capacity is insufficient or unknown")
            token = ReservationToken(
                operation_id,
                tenant,
                plugin_id,
                stage_name,
                target_digest,
                source_bytes,
                source_members,
                charged_bytes,
                charged_members,
                live(),
                self._policy,
                self._root,
                self._root_identity,
                sys.platform != "win32",
                scope_lock_identity,
                scope_lock_accounted,
            )
            payload = self._payload(token, self._binding(directory))
            current_bytes = sum(
                directory.info(name).st_size
                for name in self._names(directory, self._policy.max_ledger_records + 2)
                if name != "lock"
            )
            if current_bytes + _RECORD_BYTES > self._policy.max_ledger_bytes:
                raise RuntimeError("reservation ledger byte scan limit reached")
            self._write(directory, str(operation_id) + ".json", payload)
            self._root_check(root)
        # Unlock and all descriptor closes must also succeed before delivering authority.
        return token

    def prepare_scope_lock(
        self,
        *,
        tenant: UUID,
        plugin_id: str,
        deadline: datetime,
        revalidate: Callable[[], bool],
    ) -> ScopeLockReservation:
        """Enroll infrastructure before the caller acquires its shared scope lock.

        This method never acquires a scope lock. It returns immutable facts after
        releasing the domain lock; bind the caller's descriptor and parents to
        those facts, then acquire publication(). Do not call inside publication.
        Known complete scopes are reused even at quota; legacy/partial metadata
        is retained and refused. Charges cover two records and three nodes,
        including shared parents conservatively per scope, with no operation or
        payload reservation, release, recovery or cleanup authority.
        """
        if type(tenant) is not UUID or not _plugin(plugin_id) or not callable(revalidate):
            raise ValueError("invalid scope infrastructure request binding")
        _utc(deadline)

        def live() -> datetime:
            now = _clock_time()
            if now >= deadline:
                raise RuntimeError("scope infrastructure request deadline expired")
            if revalidate() is not True:
                raise RuntimeError("scope infrastructure request is no longer valid")
            now = _clock_time()
            if now >= deadline:
                raise RuntimeError("scope infrastructure request deadline expired")
            return now

        live()
        # No incoming exception: existing unaccounted scope locks are never adopted.
        with self._domain() as (root, directory):
            live()
            tokens, scopes = self._ledger(root, directory)
            known = next(
                (s for s in scopes if (s.tenant, s.plugin_id) == (tenant, plugin_id)), None
            )
            if known is not None:
                self._sync_scope(root, known)
                self._sync_scope_records(directory, known)
                self._ledger(root, directory)
                live()
                result = known
            else:
                if self._scope_lock(root, tenant, plugin_id) is not None:
                    raise RuntimeError("unaccounted legacy scope infrastructure; retained")
                usage = self._usage(tokens, scopes)
                required = usage.bytes + _SCOPE_BYTES
                if (
                    required > self._policy.max_bytes
                    or usage.members + _SCOPE_MEMBERS > self._policy.max_members
                ):
                    raise RuntimeError("scope infrastructure aggregate budget exceeded")
                names = self._names(directory, self._policy.max_ledger_records + 2)
                if len(tokens) + 2 * len(scopes) + 2 > self._policy.max_ledger_records:
                    raise RuntimeError("scope infrastructure ledger record scan limit reached")
                if (
                    sum(directory.info(n).st_size for n in names if n != "lock") + 2 * _RECORD_BYTES
                    > self._policy.max_ledger_bytes
                ):
                    raise RuntimeError("scope infrastructure ledger byte scan limit reached")
                available = _available_bytes(root)
                self._root_check(root)
                if (
                    type(available) is not int
                    or available < self._policy.min_available_bytes + required
                ):
                    raise RuntimeError(
                        "scope infrastructure available capacity is insufficient or unknown"
                    )
                result = ScopeLockReservation(
                    tenant,
                    plugin_id,
                    self._root / ".uninstall-locks" / str(tenant) / (plugin_id + ".lock"),
                    (),
                    _SCOPE_BYTES,
                    _SCOPE_MEMBERS,
                    live(),
                    self._policy,
                    self._root,
                    self._root_identity,
                    sys.platform != "win32",
                )
                name = self._scope_name(tenant, plugin_id)
                binding = self._binding(directory)
                self._write(directory, name, self._scope_payload(result, binding))
                intent_identity = _identity(directory.info(name))
                with contextlib.ExitStack() as stack:
                    namespace = root.child(".uninstall-locks")
                    stack.callback(namespace.close)
                    parent = namespace.child(str(tenant))
                    stack.callback(parent.close)
                    fd = parent.open(plugin_id + ".lock", os.O_RDWR | os.O_CREAT | os.O_EXCL)
                    stack.callback(os.close, fd)
                    before = _file(parent, plugin_id + ".lock", fd)
                    if before.st_size != 0 or os.write(fd, b"\0") != 1:
                        raise RuntimeError("scope infrastructure lock short write; retained")
                    written = _file(parent, plugin_id + ".lock", fd)
                    if written.st_size != 1 or _identity(written) != _identity(before):
                        raise RuntimeError("scope infrastructure lock changed during creation")
                    os.fsync(fd)
                    parent.sync()
                    namespace.sync()
                    root.sync()
                    if _stamp(_file(parent, plugin_id + ".lock", fd)) != _stamp(written):
                        raise RuntimeError("scope infrastructure lock changed during persistence")
                    identity = (
                        *_identity(root.info(".uninstall-locks")),
                        *_identity(namespace.info(str(tenant))),
                        *_identity(written),
                    )
                    result = ScopeLockReservation(
                        tenant,
                        plugin_id,
                        result.lock_path,
                        identity,
                        _SCOPE_BYTES,
                        _SCOPE_MEMBERS,
                        result.created_at,
                        self._policy,
                        self._root,
                        self._root_identity,
                        sys.platform != "win32",
                    )
                    if self._scope_lock(root, tenant, plugin_id, enrolled=True) != identity:
                        raise RuntimeError(
                            "scope infrastructure lock or parents changed during creation"
                        )
                    self._write(
                        directory,
                        "scope-binding-" + name.removeprefix("scope-"),
                        self._scope_payload(result, binding, intent_identity=intent_identity),
                    )
                    self._ledger(root, directory)
                    live()
        return result

    def _sync_scope_records(self, directory: _LockedDirectory, scope: ScopeLockReservation) -> None:
        name = self._scope_name(scope.tenant, scope.plugin_id)
        for record in (name, "scope-binding-" + name.removeprefix("scope-")):
            fd = directory.open(record, os.O_RDWR)
            try:
                stamp = _stamp(_file(directory, record, fd))
                os.fsync(fd)
                directory.sync()
                if _stamp(_file(directory, record, fd)) != stamp:
                    raise RuntimeError("scope infrastructure record changed during persistence")
            finally:
                os.close(fd)

    def _sync_scope(self, root: _Directory, scope: ScopeLockReservation) -> None:
        with contextlib.ExitStack() as stack:
            namespace = _Directory(scope.lock_path.parent.parent, private=False)
            stack.callback(namespace.close)
            parent = _Directory(scope.lock_path.parent, private=False)
            stack.callback(parent.close)
            fd = parent.open(scope.lock_path.name, os.O_RDWR)
            stack.callback(os.close, fd)
            stamp = _stamp(_file(parent, scope.lock_path.name, fd))
            os.fsync(fd)
            parent.sync()
            namespace.sync()
            root.sync()
            if _stamp(_file(parent, scope.lock_path.name, fd)) != stamp or (
                self._scope_lock(root, scope.tenant, scope.plugin_id, enrolled=True)
                != scope.scope_lock_identity
            ):
                raise RuntimeError(
                    "scope infrastructure lock or parents changed during persistence"
                )


class StagePublicationContext:
    """One live, thread/process-bound domain lock; never a recoverable lease."""

    def __init__(
        self,
        store: StageReservationStore,
        root: _Directory,
        directory: _LockedDirectory,
        tenant: UUID,
        plugin_id: str,
    ) -> None:
        self._store, self._root, self._directory = store, root, directory
        self._scope = (tenant, plugin_id)
        self._owner = (os.getpid(), get_ident())
        self._active = True

    def _check_owner(self) -> None:
        if self._owner != (os.getpid(), get_ident()):
            raise RuntimeError("publication context belongs to another execution")

    def _check(self) -> None:
        self._check_owner()
        if not self._active or _PUBLICATION.get() is not self:
            raise RuntimeError("publication context is inactive or belongs to another execution")
        self._store._root_check(self._root)
        self._directory.check()

    def usage(self) -> ReservationUsage:
        return self._store.usage(_publication=self)

    def validate_token(self, token: ReservationToken) -> None:
        self._check()
        if (token.tenant, token.plugin_id) != self._scope:
            raise RuntimeError("publication context token scope differs")
        self._store.validate_token(token, _publication=self)

    def reserve(
        self,
        *,
        operation_id: UUID,
        tenant: UUID,
        plugin_id: str,
        stage_name: str,
        target_digest: str,
        source_bytes: int,
        source_members: int,
        deadline: datetime,
        revalidate: Callable[[], bool],
    ) -> ReservationToken:
        return self._store.reserve(
            operation_id=operation_id,
            tenant=tenant,
            plugin_id=plugin_id,
            stage_name=stage_name,
            target_digest=target_digest,
            source_bytes=source_bytes,
            source_members=source_members,
            deadline=deadline,
            revalidate=revalidate,
            _publication=self,
        )
