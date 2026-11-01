"""Explicit memory IO and hostile-data seams for the conversation input tests."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import Never, Self, TypedDict, TypeGuard
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction, async_sessionmaker

from agent_hub.auth.models import Role
from agent_hub.db.models import RunRow
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.authorization import _AuthenticatedInput
from agent_hub.runs.conversation_inputs import ConversationInputs, conversation_inputs
from agent_hub.runs.conversation_queue import ConversationQueueRepository
from agent_hub.runs.repository import RunRecord, RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.artifacts import ArtifactReference, InMemoryArtifactRepository
from agent_hub.runtime.contracts import (
    Artifact,
    JsonValue,
    RuntimeCheckpoint,
    _freeze_object,
)


def present[T](value: T | None) -> T:
    assert value is not None
    return value


def string_keys(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def mutable(value: object) -> dict[str, object]:
    assert string_keys(value)
    return value


def json_map(value: object) -> Mapping[str, JsonValue]:
    return _freeze_object(value, name='conversation fixture')


def evidence(value: object) -> _AuthenticatedInput:
    assert isinstance(value, _AuthenticatedInput)
    return value


def run[T](coroutine: Coroutine[object, None, T]) -> T:
    # Capture the typed result inside the coroutine, rather than casting the
    # untyped StopIteration.value. No event loop, socket or background task.
    result: list[T] = []
    async def capture() -> None:
        result.append(await coroutine)
    wrapper = capture()
    try:
        for _ in range(10000):
            suspended = wrapper.send(None)
            assert suspended is None, 'fixture must not schedule external IO'
    except StopIteration:
        assert len(result) == 1
        return result[0]
    finally:
        wrapper.close()
        coroutine.close()
    raise AssertionError('unbounded fixture coroutine')


class InputArgs(TypedDict):
    tenant_id: UUID
    run_id: UUID
    actor_id: UUID | None
    actor_role: Role | None
    mode: TaskMode
    worker_id: str
    execution_token: UUID | None
    current_request: str
    routing_decision: dict[str, object]
    checkpoint: RuntimeCheckpoint | None


class InputChanges(TypedDict, total=False):
    routing_decision: dict[str, object]
    checkpoint: RuntimeCheckpoint | None


class LockTransaction(AsyncSessionTransaction):
    def __init__(self, session: AsyncSession, lock: asyncio.Lock) -> None:
        super().__init__(session)
        self.lock = lock

    async def __aenter__(self) -> Self:
        await self.lock.acquire()
        return self

    async def __aexit__(self, *args: object) -> None:
        self.lock.release()


class LockSession(AsyncSession):
    def __init__(self, lock: asyncio.Lock) -> None:
        super().__init__()
        self.lock = lock

    def begin(self) -> LockTransaction:
        return LockTransaction(self, self.lock)

    async def __aexit__(self, *args: object) -> None:
        # This memory-only session owns no engine/connection. Close directly;
        # the synchronous coroutine fixture must not spawn background tasks.
        await self.close()


class MemoryRowRepository(RunRepository):
    def __init__(self, row: RunRow) -> None:
        super().__init__(async_sessionmaker())
        self.row, self.lock = row, asyncio.Lock()

    async def run_transaction(self) -> AsyncSession:
        return LockSession(self.lock)

    async def get_for_update(self, session: AsyncSession, run_id: UUID) -> RunRow:
        return self.row


class NoDatabaseRepository(RunRepository):
    def __init__(self) -> None:
        pass

    def __getattribute__(self, name: str) -> Never:
        raise AssertionError('legacy history must not access repository: ' + name)


class RevocableArtifacts(InMemoryArtifactRepository):
    def __init__(self) -> None:
        super().__init__()
        self.after_get: Callable[[], None] | None = None
        self.after_put: Callable[[], None] | None = None

    async def get_many(self, tenant_id: UUID, run_id: UUID, references: tuple[ArtifactReference, ...]) -> tuple[Artifact, ...]:
        result = await super().get_many(tenant_id, run_id, references)
        if self.after_get is not None:
            self.after_get()
        return result

    async def put(self, tenant_id: UUID, run_id: UUID, artifact: Artifact, *, write_id: UUID | None = None) -> None:
        await super().put(tenant_id, run_id, artifact, write_id=write_id)
        if self.after_put is not None:
            self.after_put()


type Case = tuple[MemoryRowRepository, RevocableArtifacts, InputArgs]


async def invoke_builder(
    repository: RunRepository, artifacts: RevocableArtifacts, args: InputArgs,
    builder: Callable[[], Awaitable[object]],
) -> ConversationInputs:
    # Only this hostile-result seam accepts an object-valued builder. Pass its
    # value unmodified to the real runtime validator (list/wrong item/hash cases).
    entry: object = conversation_inputs
    assert callable(entry)
    result: object = await entry(repository, artifacts, **args, builder=builder)
    assert isinstance(result, ConversationInputs)
    return result


class UnexpectedQueue:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        raise AssertionError('authenticated input test must not enqueue outside outbox')


class PersistenceSpy(RunRepository):
    def __init__(self, callback: Callable[..., Awaitable[Never]]) -> None:
        super().__init__(async_sessionmaker())
        self.callback = callback

    async def create_run(self, **kwargs: object) -> Never:
        await self.callback(**kwargs)
        raise AssertionError('persistence callback must stop')


class ActiveConversationRepository(RunRepository):
    def __init__(self, record: RunRecord) -> None:
        super().__init__(async_sessionmaker())
        self.record = record

    async def list_conversation(self, tenant_id: UUID, conversation_id: str) -> tuple[RunRecord, ...]:
        return (self.record,)


class EditSpy(ConversationQueueRepository):
    def __init__(self, callback: Callable[..., Awaitable[Never]]) -> None:
        super().__init__(async_sessionmaker())
        self.callback = callback

    async def edit(self, *args: object, **kwargs: object) -> Never:
        await self.callback(*args, **kwargs)
        raise AssertionError('edit callback must stop')


class SubmitSpyService(RunService):
    callback: Callable[..., Awaitable[Never]]

    async def submit(self, **kwargs: object) -> Never:
        await self.callback(**kwargs)
        raise AssertionError('submit callback must stop')


class RouterCaptureService(RunService):
    """Router IO spy: capture genuine evidence; never manufacture a submission."""
    def __init__(self, callback: Callable[..., Awaitable[Never]],
                 original: _AuthenticatedInput | None = None) -> None:
        self.callback, self.original = callback, original

    async def submit(self, **kwargs: object) -> Never:
        await self.callback(**kwargs)
        raise AssertionError('router callback must stop')

    async def queue_message(self, **kwargs: object) -> Never:
        await self.callback(**kwargs)
        raise AssertionError('router callback must stop')

    async def edit_queued_message(self, *args: object, **kwargs: object) -> Never:
        await self.callback(*args, **kwargs)
        raise AssertionError('router callback must stop')

    async def queued_authenticated_input(self, tenant_id: UUID, item_id: UUID) -> _AuthenticatedInput | None:
        return self.original


def spy_method[**P, R](original: Callable[P, Awaitable[R]],
                     received: list[_AuthenticatedInput]) -> Callable[P, Awaitable[R]]:
    async def capture(*args: P.args, **kwargs: P.kwargs) -> R:
        received.append(evidence(kwargs.pop('authenticated_input')))
        return await original(*args, **kwargs)
    return capture


def fresh_row(**values: object) -> RunRow:
    return RunRow(created_at=datetime.now(UTC), **values)
