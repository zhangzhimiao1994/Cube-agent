"""Nominal IO contracts and explicit hostile inputs for authenticated-input tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from types import ModuleType
from typing import Never, Protocol, TypedDict, runtime_checkable
from uuid import UUID

from alembic.operations import Operations
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.db.base import Base
from agent_hub.db.models import RunAuthenticatedInputRow, RunInputAuthorizationRow, RunRow
from agent_hub.runs import authorization
from agent_hub.runs.authorization import _AuthenticatedInput

type SessionFactory = async_sessionmaker[AsyncSession]


class EvidenceChanges(TypedDict, total=False):
    tenant_id: UUID
    actor_id: UUID
    actor_role: Role
    message: str
    message_sha256: str
    scope_sha256: str
    input_scope: str
    _seal: object


def evidence_changes(field: str, value: object) -> EvidenceChanges:
    """Retain each parameterized forged value without changing its runtime type."""
    if field == "tenant_id":
        assert isinstance(value, UUID)
        return {"tenant_id": value}
    if field == "actor_id":
        assert isinstance(value, UUID)
        return {"actor_id": value}
    if field == "actor_role":
        assert isinstance(value, Role)
        return {"actor_role": value}
    if field == "message":
        assert isinstance(value, str)
        return {"message": value}
    if field == "message_sha256":
        assert isinstance(value, str)
        return {"message_sha256": value}
    if field == "scope_sha256":
        assert isinstance(value, str)
        return {"scope_sha256": value}
    if field == "input_scope":
        assert isinstance(value, str)
        return {"input_scope": value}
    assert field == "_seal"
    return {"_seal": value}


class PersistInput(Protocol):
    def __call__(
        self,
        session: AsyncSession,
        run: RunRow,
        evidence: _AuthenticatedInput,
        *,
        submitted_message: str | None = None,
    ) -> Awaitable[None]: ...


class AuthorizationModule(Protocol):
    @property
    def _capture_authenticated_input(
        self,
    ) -> Callable[[AuthenticatedPrincipal, str, dict[str, object]], _AuthenticatedInput]: ...

    @property
    def _validate_authenticated_input(
        self,
    ) -> Callable[[_AuthenticatedInput, UUID, UUID | None, Role | None, str], None]: ...

    @property
    def _persist_authenticated_input(self) -> PersistInput: ...

    @property
    def _validated_persisted_input(
        self,
    ) -> Callable[
        [RunRow, RunInputAuthorizationRow | None, RunAuthenticatedInputRow | None],
        _AuthenticatedInput | None,
    ]: ...


def capture_untrusted_principal(
    principal: object, message: str, scope: dict[str, object]
) -> _AuthenticatedInput:
    """Invoke the real runtime boundary with intentionally invalid caller data.

    The production signature stays precise. Only this hostile-input seam resolves
    its callable dynamically, and any successful return must be the actual DTO.
    """
    capture: object = authorization._capture_authenticated_input
    assert callable(capture)
    result: object = capture(principal, message, scope)
    assert isinstance(result, _AuthenticatedInput)
    return result


def assign_attribute(target: object, name: str, value: object) -> None:
    """Exercise ordinary setattr, including the actual frozen dataclass guard."""
    setattr(target, name, value)


def present[T](value: T | None) -> T:
    assert value is not None
    return value


class RecordingSession(AsyncSession):
    """Only the test's SQL transport seam; preserve actual ORM row payloads."""

    def __init__(self, added: list[Base]) -> None:
        super().__init__()
        self.added = added

    def add(self, instance: object, _warn: bool = True) -> None:
        assert isinstance(instance, Base)
        self.added.append(instance)

    async def flush(self, objects: Sequence[object] | None = None) -> None:
        pass

    async def get(self, entity: object, ident: object, **kwargs: object) -> None:
        assert entity is RunInputAuthorizationRow


class ForbiddenSessions(async_sessionmaker[AsyncSession]):
    def __init__(self, forbidden: Callable[[], Never]) -> None:
        super().__init__()
        self.forbidden = forbidden

    def __call__(self, **local_kw: object) -> AsyncSession:
        self.forbidden()


class UnexpectedTaskQueue:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        raise AssertionError("authenticated submission must use the transactional outbox")


@runtime_checkable
class DowngradeModule(Protocol):
    op: Operations | ModuleType

    def downgrade(self) -> None: ...
