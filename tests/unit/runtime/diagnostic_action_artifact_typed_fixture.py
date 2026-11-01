"""Local diagnostic/action fixtures; retain original IO mocks and mutable state."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Mapping
from datetime import datetime
from types import TracebackType
from typing import Self, TypedDict
from uuid import UUID, uuid4

from openai import AsyncOpenAI
from sqlalchemy import event
from sqlalchemy.engine import Result
from sqlalchemy.engine.result import IteratorResult, SimpleResultMetaData
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction, async_sessionmaker
from sqlalchemy.orm import ORMExecuteState

from agent_hub.config.repository import ConfigRevision
from agent_hub.config.service import ConfigService
from agent_hub.db.models import RunApprovalRow, RunEventRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.litellm_client import _Chat, _Completions
from agent_hub.runs.repository import RunRecord
from agent_hub.runs.service import RunService
from agent_hub.runtime import defaults
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    ExecutionRuntime,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
    _freeze_object,
)
from agent_hub.runtime.registry import RuntimeRegistry
from agent_hub.security.secrets import SecretService
from tests.unit.runs.conversation_authenticated_input_typed_fixture import run
from tests.unit.runs.project_auto_repair_typed_fixture import (
    ledger,
    mapping,
    number,
    present,
    sequence,
    string,
)
from tests.unit.runs.reviewed_report_revision_typed_fixture import object_map as mutable
from tests.unit.runs.test_repository_approval_upserts import ApprovalProjectionSession
from tests.unit.runs.test_terminal_hooks import (
    ContextRecordingRuntime,
    ExecutableFakeRepository,
    FakeRunRow,
    FakeTransaction,
)
from tests.unit.runtime.test_configured_runtime import FakeConfigService, FakeSecretService

__all__ = ["ledger", "mapping", "mutable", "number", "present", "run", "sequence", "string"]

type ConfiguredRuntime = (
    defaults.ConfigBackedDispatchRuntime
    | defaults.ConfigBackedDiscussionRuntime
    | defaults.ConfigBackedHybridRuntime
)


def configured(value: ExecutionRuntime) -> ConfiguredRuntime:
    assert isinstance(
        value,
        (
            defaults.ConfigBackedDispatchRuntime,
            defaults.ConfigBackedDiscussionRuntime,
            defaults.ConfigBackedHybridRuntime,
        ),
    )
    return value


class ExecuteOptions(TypedDict):
    tenant_id: UUID
    run_id: UUID
    actor: str
    name: str
    arguments: Mapping[str, JsonValue]
    idempotency_key: str


def json_map(value: object) -> Mapping[str, JsonValue]:
    return _freeze_object(value, name="diagnostic action fixture", max_nodes=2_000_000)


def closeable_events(value: AsyncIterator[RunEvent]) -> AsyncGenerator[RunEvent, None]:
    assert isinstance(value, AsyncGenerator)
    return value


class _CheckpointRepository(ExecutableFakeRepository):
    async def claim_for_execution(
        self,
        session: FakeTransaction,
        run_id: UUID,
        *,
        allow_running_recovery: bool,
        worker_id: str | None = None,
        worker_lease_token: UUID | None = None,
        worker_lease_expires_at: datetime | None = None,
    ) -> tuple[FakeRunRow, RuntimeCheckpoint | None] | RunRecord:
        claimed = await super().claim_for_execution(
            session,
            run_id,
            allow_running_recovery=allow_running_recovery,
            worker_id=worker_id,
            worker_lease_token=worker_lease_token,
            worker_lease_expires_at=worker_lease_expires_at,
        )
        assert not isinstance(claimed, RunRecord)
        row, _ = claimed
        assert row.mode is not None
        return row, RuntimeCheckpoint(
            id=uuid4(),
            runtime_type="test",
            runtime_version="1",
            run_id=run_id,
            tenant_id=row.tenant_id,
            mode=TaskMode(row.mode),
            state={},
        )


async def _execute_policy(
    repository: ExecutableFakeRepository,
    *,
    allow_running_recovery: bool = False,
) -> TaskContext:
    assert repository.row.mode is not None
    runtime = ContextRecordingRuntime()
    runtime.mode = TaskMode(repository.row.mode)
    constructor: object = RunService
    assert callable(constructor)
    service: object = constructor(
        repository,
        runtime_registry=RuntimeRegistry((runtime,)),
        router=None,
        task_queue=object(),
    )
    assert isinstance(service, RunService)
    submitted = await service.execute(
        repository.run_id, allow_running_recovery=allow_running_recovery
    )
    assert submitted.status is RunStatus.COMPLETED
    assert len(runtime.contexts) == 1
    return runtime.contexts[0]


class FindQuery(TypedDict):
    type: str
    producer: str
    limit: int


async def find_untrusted(
    repository: InMemoryArtifactRepository,
    tenant: UUID,
    run_id: UUID,
    query: Mapping[str, object],
) -> tuple[ArtifactReference, ...]:
    target: object = repository.find_references
    assert callable(target)
    result: object = await target(tenant, run_id, **query)
    assert isinstance(result, tuple) and all(isinstance(item, ArtifactReference) for item in result)
    return tuple(item for item in result if isinstance(item, ArtifactReference))


class HarnessTrace(TypedDict):
    tenant: UUID
    request: HarnessToolCallRequest
    user: UUID | None
    role: object
    result: HarnessToolCallResult


class ActionConfigService(ConfigService):
    def __init__(self, document: dict[str, object]) -> None:
        self.original = FakeConfigService(document)

    async def get_current(self, tenant_id: UUID) -> ConfigRevision | None:
        return await self.original.get_current(tenant_id)


class ActionSecretService(SecretService):
    def __init__(self) -> None:
        self.original = FakeSecretService()

    async def resolve(self, tenant_id: UUID, reference: object) -> str:
        return await self.original.resolve(tenant_id, reference)

    async def fingerprint(self, tenant_id: UUID, reference: object) -> str:
        return await self.original.fingerprint(tenant_id, reference)


def rows(values: list[object]) -> Result[tuple[object, ...]]:
    return IteratorResult(SimpleResultMetaData(["value"]), iter((value,) for value in values))


class ProjectionTransaction(AsyncSessionTransaction):
    async def __aenter__(self) -> Self:
        assert isinstance(self.session, ProjectionSession)
        await self.session.original.begin().__aenter__()
        return self

    async def __aexit__(
        self,
        typ: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert isinstance(self.session, ProjectionSession)
        await self.session.original.__aexit__(typ, value, traceback)


class ProjectionSession(AsyncSession):
    def __init__(self, original: ApprovalProjectionSession) -> None:
        super().__init__()
        self.original = original
        event.listen(self.sync_session, "do_orm_execute", self.dispatch_projection, retval=True)

    async def __aenter__(self) -> Self:
        await self.original.__aenter__()
        return self

    async def __aexit__(
        self,
        typ: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.original.__aexit__(typ, value, traceback)
        await self.close()

    def begin(self) -> AsyncSessionTransaction:
        return ProjectionTransaction(self)

    def add(self, instance: object, _warn: bool = True) -> None:
        self.original.add(instance)

    async def flush(self, objects: object = None) -> None:
        await self.original.flush()

    def dispatch_projection(self, state: ORMExecuteState) -> Result[tuple[object, ...]]:
        if state.is_select:
            if any(
                name in str(state.statement)
                for name in ("agent_hub_run_approvals", "agent_hub_run_events")
            ) and not str(state.statement).startswith("SELECT max("):
                selected: object = run(self.original.scalars(state.statement))
                all_rows: object = vars(selected)["all"]
                assert callable(all_rows)
                result: object = all_rows()
                assert isinstance(result, list)
                assert all(isinstance(item, (RunApprovalRow, RunEventRow)) for item in result)
                return rows(list(result))
            scalar: object = run(self.original.scalar(state.statement))
        else:
            executed: object = run(self.original.execute(state.statement))
            scalar_one: object = vars(executed)["scalar_one_or_none"]
            assert callable(scalar_one)
            scalar = scalar_one()
        return rows([] if scalar is None else [scalar])


class ProjectionSessions(async_sessionmaker[AsyncSession]):
    def __init__(self, original: ApprovalProjectionSession) -> None:
        super().__init__()
        self.original = original

    def __call__(self, **local_kw: object) -> ProjectionSession:
        return ProjectionSession(self.original)


class SDKCompletion:
    def __init__(self, original: object) -> None:
        assert callable(original)
        self.original = original

    async def create(self, **kwargs: object) -> object:
        target = self.original
        assert callable(target)
        pending: object = target(**kwargs)
        assert isinstance(pending, Awaitable)
        result: object = await pending
        return result


class SDKChat:
    completions: _Completions

    def __init__(self, completions: _Completions) -> None:
        self.completions = completions


class SDKClient:
    chat: _Chat
    responses: _Completions

    def __init__(self, original: AsyncOpenAI) -> None:
        self.original = original
        self.chat = SDKChat(SDKCompletion(original.chat.completions.create))
        self.responses = SDKCompletion(original.responses.create)

    async def close(self) -> None:
        await self.original.close()
