"""Lease-fenced issuance index, not a validator of verification or Artifact bodies.

Only trusted verification code may call begin/complete after its own policy and
execution checks. Hashes bind that caller's data; matching hashes never prove
that checks ran. Recovery callers must independently validate referenced bodies,
current source/contracts/environments, and live approvals before delivery.

Supply/preparation can mutate generated_project_preparation, which is part of
the bound scope. Build authority and call begin only after that phase is stable,
immediately before actual verification execution, not at the outer verify entry.
An existing running record may represent an unknown execution outcome; neither
lease replacement nor a different action key authorizes restarting it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import wraps
from types import SimpleNamespace
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.models import Role
from agent_hub.capabilities.scoped_read import authorized_workspace_scope
from agent_hub.db.models import RunRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.repository import RunConflict, RunNotFound, RunRepository
from agent_hub.runtime.artifacts import ArtifactReference
from agent_hub.runtime.contracts import TaskContext, _freeze_object

RECEIPT_KEY = "project_verification_receipt"
_MAX_INDEX_BYTES = 16 * 1024
_MAX_REFERENCES = 16
_MAX_GENERATION = 2**63 - 1
_SHA = re.compile(r"[a-f0-9]{64}")
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_SCOPE_ERROR = "project_verification_receipt_scope_changed"
_STATE_ERROR = "project_verification_receipt_state_changed"
_UNAVAILABLE = "project_verification_receipt_unavailable"
_BOUND_KEYS = (
    "project_id", "workspace_session_id", "sandbox_profile", "permission_mode", "execution_backend",
    "requested_permissions", "runtime_timeout_seconds", "project_input_contract_version",
    "project_input", "project_input_preparation", "python_input_supply",
    "generated_project_contract_version", "generated_project_preparation", "toolchain_supply",
    "go_project_recipe",
)


def _encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _text(value: object, maximum: int) -> bool:
    return (type(value) is str and 0 < len(value) <= maximum and value == value.strip()
            and all(ord(character) >= 32 and ord(character) != 127 for character in value))


def _hash(value: object) -> bool:
    return type(value) is str and _SHA.fullmatch(value) is not None


def _uuid(value: object) -> UUID:
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError(_STATE_ERROR)
    return UUID(value)


def _scope_digest(route: object, tenant_id: UUID, run_id: UUID) -> str:
    if not isinstance(route, Mapping):
        raise TypeError(_SCOPE_ERROR)
    authorized_workspace_scope(
        SimpleNamespace(tenant_id=tenant_id, id=run_id, routing_decision=route),
        tenant_id=tenant_id, run_id=run_id,
    )
    if (route.get("sandbox_profile") not in {"restricted", "workspace_write"}
            or route.get("execution_backend", "systemd") != "systemd"
            or "command.run" not in route["requested_permissions"]):
        raise ValueError(_SCOPE_ERROR)
    if "permission_mode" in route and (
        type(route["permission_mode"]) is not str
        or route["permission_mode"] not in {"default", "auto_review"}
    ):
        raise ValueError(_SCOPE_ERROR)
    binding = _freeze_object({key: route[key] for key in _BOUND_KEYS if key in route},
                             name="verification receipt scope")
    return hashlib.sha256(_encoded(binding)).hexdigest()


def _storage_errors[**P, T](method: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    @wraps(method)
    async def guarded(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await method(*args, **kwargs)
        except SQLAlchemyError:
            pass
        # Sanitize after contextlib has unwound the transaction and its exception chain.
        raise RunConflict(_UNAVAILABLE)

    return guarded


@dataclass(frozen=True, slots=True)
class VerificationAuthority:
    """Trusted caller identity, compared with the locked RunRow on every access.

    Construct from the worker's internal context, never model/tool arguments.
    The constructor alone establishes no authority and bypasses no approvals.
    """

    tenant_id: UUID
    run_id: UUID
    actor_id: UUID
    actor_role: Role
    worker_id: str
    execution_token: UUID = field(repr=False)
    actor: str
    request_sha256: str
    mode: TaskMode
    scope_sha256: str

    def __post_init__(self) -> None:
        if (not all(type(value) is UUID for value in (
                self.tenant_id, self.run_id, self.actor_id, self.execution_token))
                or type(self.actor_role) is not Role or self.actor_role is Role.VIEWER
                or not _text(self.worker_id, 128) or type(self.actor) is not str
                or _IDENTIFIER.fullmatch(self.actor) is None
                or not _hash(self.request_sha256) or not _hash(self.scope_sha256)
                or type(self.mode) is not TaskMode or self.mode is TaskMode.AUTO):
            raise RunConflict(_SCOPE_ERROR)

    @classmethod
    def from_context(
        cls, context: TaskContext, *, worker_id: str, actor: str,
    ) -> VerificationAuthority:
        try:
            context = context.validated_internal_clone()
            if context.actor_id is None or context.actor_role is None or context.execution_token is None:
                raise ValueError(_SCOPE_ERROR)
            return cls(
                tenant_id=context.tenant_id, run_id=context.run_id, actor_id=context.actor_id,
                actor_role=context.actor_role, worker_id=worker_id,
                execution_token=context.execution_token, actor=actor,
                request_sha256=hashlib.sha256(context.request.encode("utf-8")).hexdigest(),
                mode=context.mode,
                scope_sha256=_scope_digest(context.routing_decision, context.tenant_id, context.run_id),
            )
        except (ValueError, TypeError, RuntimeError, KeyError, AttributeError):
            raise RunConflict(_SCOPE_ERROR) from None

    def to_payload(self) -> dict[str, object]:
        return {
            "tenant_id": str(self.tenant_id), "run_id": str(self.run_id),
            "actor_id": str(self.actor_id), "actor_role": self.actor_role.value,
            "worker_id": self.worker_id, "execution_token": str(self.execution_token),
            "actor": self.actor, "request_sha256": self.request_sha256,
            "mode": self.mode.value, "scope_sha256": self.scope_sha256,
        }

    @classmethod
    def from_payload(cls, value: object) -> VerificationAuthority:
        if (not isinstance(value, Mapping) or set(value) != {
                "tenant_id", "run_id", "actor_id", "actor_role", "worker_id", "execution_token",
                "actor", "request_sha256", "mode", "scope_sha256"}
                or any(type(item) is not str for item in value.values())):
            raise ValueError(_STATE_ERROR)
        return cls(
            tenant_id=_uuid(value["tenant_id"]), run_id=_uuid(value["run_id"]),
            actor_id=_uuid(value["actor_id"]), actor_role=Role(value["actor_role"]),
            worker_id=value["worker_id"], execution_token=_uuid(value["execution_token"]),
            actor=value["actor"], request_sha256=value["request_sha256"],
            mode=TaskMode(value["mode"]), scope_sha256=value["scope_sha256"],
        )

    def stable_scope(self) -> dict[str, object]:
        return {key: value for key, value in self.to_payload().items()
                if key not in {"worker_id", "execution_token"}}


@dataclass(frozen=True, slots=True)
class VerificationRecord:
    """Private issuance metadata; Artifact references are opaque caller-provided locators."""

    generation: int
    status: Literal["running", "passed", "failed"]
    authority: VerificationAuthority
    action_key: str
    execution_id: str
    source_sha256: str
    artifact_references: tuple[ArtifactReference, ...] = ()

    def __post_init__(self) -> None:
        if (type(self.generation) is not int or not 1 <= self.generation <= _MAX_GENERATION
                or type(self.status) is not str or self.status not in {"running", "passed", "failed"}
                or type(self.authority) is not VerificationAuthority
                or not _text(self.action_key, 160) or type(self.execution_id) is not str
                or _IDENTIFIER.fullmatch(self.execution_id) is None or not _hash(self.source_sha256)
                or type(self.artifact_references) is not tuple
                or len(self.artifact_references) > _MAX_REFERENCES):
            raise RunConflict(_STATE_ERROR)
        replace(self.authority)
        for reference in self.artifact_references:
            if type(reference) is not ArtifactReference:
                raise RunConflict(_STATE_ERROR)
            ArtifactReference(id=reference.id, sha256=reference.sha256)
        if (len({item.id for item in self.artifact_references}) != len(self.artifact_references)
                or self.status == "running" and self.artifact_references
                or self.status == "passed" and not self.artifact_references
                or len(_encoded(self.to_payload())) > _MAX_INDEX_BYTES):
            raise RunConflict(_STATE_ERROR)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1, "generation": self.generation, "status": self.status,
            "authority": self.authority.to_payload(), "action_key": self.action_key,
            "execution_id": self.execution_id, "source_sha256": self.source_sha256,
            "artifact_references": [item.to_payload() for item in self.artifact_references],
        }

    @classmethod
    def from_payload(cls, value: object) -> VerificationRecord:
        try:
            item = _freeze_object(value, name="verification issuance index")
            if (set(item) != {"schema_version", "generation", "status", "authority", "action_key",
                             "execution_id", "source_sha256", "artifact_references"}
                    or type(item["schema_version"]) is not int or item["schema_version"] != 1
                    or len(_encoded(item)) > _MAX_INDEX_BYTES
                    or not isinstance(item["artifact_references"], tuple)):
                raise ValueError(_STATE_ERROR)
            references = []
            for reference in item["artifact_references"]:
                if not isinstance(reference, Mapping) or set(reference) != {"id", "sha256"}:
                    raise ValueError(_STATE_ERROR)
                references.append(ArtifactReference(id=_uuid(reference["id"]),
                                                    sha256=cast(str, reference["sha256"])))
            return cls(
                generation=cast(int, item["generation"]),
                status=cast(Literal["running", "passed", "failed"], item["status"]),
                authority=VerificationAuthority.from_payload(item["authority"]),
                action_key=cast(str, item["action_key"]), execution_id=cast(str, item["execution_id"]),
                source_sha256=cast(str, item["source_sha256"]), artifact_references=tuple(references),
            )
        except (ValueError, TypeError, RuntimeError, KeyError, AttributeError, RecursionError):
            raise RunConflict(_STATE_ERROR) from None


class ProjectVerificationReceiptRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _require_current(row: RunRow, authority: VerificationAuthority) -> None:
        try:
            if type(authority) is not VerificationAuthority:
                raise ValueError(_SCOPE_ERROR)
            replace(authority)
            record = RunRepository._record(row)
            expiry = record.worker_lease_expires_at
            if (record.status is not RunStatus.RUNNING or expiry is None or expiry.tzinfo is None
                    or expiry <= datetime.now(UTC)
                    or (record.tenant_id, record.id, record.actor_id, record.actor_role,
                        record.worker_id, record.worker_lease_token, record.mode)
                    != (authority.tenant_id, authority.run_id, authority.actor_id, authority.actor_role,
                        authority.worker_id, authority.execution_token, authority.mode)
                    or hashlib.sha256(record.request.encode("utf-8")).hexdigest() != authority.request_sha256
                    or _scope_digest(record.routing_decision, record.tenant_id, record.id)
                        != authority.scope_sha256):
                raise ValueError(_SCOPE_ERROR)
        except (ValueError, TypeError, RuntimeError, KeyError, AttributeError):
            raise RunConflict(_SCOPE_ERROR) from None

    @asynccontextmanager
    async def _locked(
        self, authority: VerificationAuthority,
    ) -> AsyncIterator[tuple[AsyncSession, RunRow]]:
        if type(authority) is not VerificationAuthority:
            raise RunConflict(_SCOPE_ERROR)
        replace(authority)
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(select(RunRow).where(
                RunRow.tenant_id == authority.tenant_id, RunRow.id == authority.run_id,
            ).with_for_update())
            if row is None:
                raise RunNotFound("run was not found")
            self._require_current(row, authority)
            yield session, row

    @staticmethod
    def _read(row: RunRow) -> VerificationRecord | None:
        route = row.routing_decision
        if not isinstance(route, Mapping):
            raise RunConflict(_SCOPE_ERROR)
        return None if RECEIPT_KEY not in route else VerificationRecord.from_payload(route[RECEIPT_KEY])

    @_storage_errors
    async def begin(
        self, authority: VerificationAuthority, *, action_key: str, execution_id: str,
        source_sha256: str,
    ) -> VerificationRecord:
        """Reserve execution after preparation stabilizes, before any verification command.

        Only an absent index or a terminal record with a different attempt key
        permits begin. Call load for a completed action; unresolved running
        outcomes require explicit reconciliation outside this DAO.
        """
        async with self._locked(authority) as (session, row):
            previous = self._read(row)
            if previous is not None and (
                previous.status not in {"passed", "failed"} or previous.action_key == action_key
            ):
                raise RunConflict(_STATE_ERROR)
            result = VerificationRecord(
                generation=1 if previous is None else previous.generation + 1, status="running",
                authority=authority, action_key=action_key, execution_id=execution_id,
                source_sha256=source_sha256,
            )
            row.routing_decision = {**(row.routing_decision or {}), RECEIPT_KEY: result.to_payload()}
            row.version += 1
            await session.flush()
            self._require_current(row, authority)
        return result

    @_storage_errors
    async def complete(
        self, authority: VerificationAuthority, *, expected: VerificationRecord,
        status: Literal["passed", "failed"], artifact_references: tuple[ArtifactReference, ...] = (),
    ) -> VerificationRecord:
        """CAS the exact running attempt; another lease may never finish it."""
        if type(expected) is not VerificationRecord or status not in {"passed", "failed"}:
            raise RunConflict(_STATE_ERROR)
        expected = replace(expected)
        async with self._locked(authority) as (session, row):
            current = self._read(row)
            if (current != expected or expected.status != "running"
                    or expected.authority != authority):
                raise RunConflict(_STATE_ERROR)
            result = replace(expected, status=status, artifact_references=artifact_references)
            row.routing_decision = {**(row.routing_decision or {}), RECEIPT_KEY: result.to_payload()}
            row.version += 1
            await session.flush()
            self._require_current(row, authority)
        return result

    @_storage_errors
    async def load(
        self, authority: VerificationAuthority, *, generation: int | None = None,
    ) -> VerificationRecord | None:
        """Read only the current pass under a live lease, allowing stable-scope lease renewal."""
        if generation is not None and (
            type(generation) is not int or not 1 <= generation <= _MAX_GENERATION
        ):
            raise RunConflict(_STATE_ERROR)
        async with self._locked(authority) as (_, row):
            result = self._read(row)
            if result is not None and result.authority.stable_scope() != authority.stable_scope():
                raise RunConflict(_SCOPE_ERROR)
            if (result is None or result.status != "passed"
                    or generation is not None and result.generation != generation):
                return None
            self._require_current(row, authority)
        return result
