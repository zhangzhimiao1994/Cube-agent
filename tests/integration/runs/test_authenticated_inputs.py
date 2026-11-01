"""Requires the existing isolated PostgreSQL fixtures; never substitute mocked SQL."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.db.models import (
    RunAuthenticatedInputRow,
    RunInputAuthorizationRow,
    RunOutboxRow,
    RunRow,
)
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.authorization import _AuthenticatedInput, _capture_authenticated_input
from agent_hub.runs.conversation_queue import (
    ConversationQueueConflict,
    ConversationQueueItem,
    ConversationQueueRepository,
    EnqueueConversationMessage,
)
from agent_hub.runs.repository import RunRecord, RunRepository
from tests.unit.runs.authenticated_inputs_typed_fixture import (
    DowngradeModule,
    SessionFactory,
    UnexpectedTaskQueue,
    assign_attribute,
    present,
)

pytestmark = pytest.mark.integration


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)


async def create(
    repository: RunRepository,
    user: AuthenticatedPrincipal,
    message: str = "original",
    evidence: _AuthenticatedInput | None = None,
    key: str | None = None,
    blocked: UUID | None = None,
) -> RunRecord:
    return await repository.create_run(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_role=user.role,
        request=message,
        mode=TaskMode.DIRECT,
        status=RunStatus.QUEUED,
        idempotency_key=key,
        enqueue=True,
        authenticated_input=evidence,
        blocked_by_run_id=blocked,
        routing_decision={"conversation_id": "conv-proof"},
    )


async def counts(factory: SessionFactory) -> tuple[int, ...]:
    async with factory() as session:
        return tuple(
            [
                present(await session.scalar(select(func.count()).select_from(table)))
                for table in (
                    RunRow,
                    RunAuthenticatedInputRow,
                    RunInputAuthorizationRow,
                    RunOutboxRow,
                )
            ]
        )


async def queued(
    factory: SessionFactory,
) -> tuple[
    AuthenticatedPrincipal,
    RunRepository,
    ConversationQueueRepository,
    ConversationQueueItem,
    _AuthenticatedInput,
]:
    user = principal()
    repository = RunRepository(factory)
    predecessor = await create(repository, user, "predecessor")
    evidence = _capture_authenticated_input(user, "original", {"conversation_id": "conv-proof"})
    successor = await create(repository, user, evidence=evidence, blocked=predecessor.id)
    queue = ConversationQueueRepository(factory)
    item = await queue.enqueue(
        EnqueueConversationMessage(
            tenant_id=user.tenant_id,
            conversation_id="conv-proof",
            predecessor_run_id=predecessor.id,
            successor_run_id=successor.id,
            message="original",
            idempotency_key="queue-proof",
        )
    )
    return user, repository, queue, item, evidence


async def test_create_atomic_original_and_outbox_idempotency(
    auth_session_factory: SessionFactory,
) -> None:
    factory = auth_session_factory
    repository = RunRepository(factory)
    user = principal()
    evidence = _capture_authenticated_input(user, "original", {"mode": "direct"})
    run = await create(repository, user, evidence=evidence, key="same")
    assert await counts(factory) == (1, 1, 1, 1)
    loaded = await repository.load_authenticated_input(user.tenant_id, run.id)
    assert loaded == evidence
    duplicate = await create(
        repository,
        user,
        "duplicate",
        _capture_authenticated_input(user, "duplicate", {}),
        key="same",
    )
    assert duplicate.id == run.id
    assert await counts(factory) == (1, 1, 1, 1)
    assert await repository.load_authenticated_input(user.tenant_id, run.id) == evidence
    assert await repository.load_authenticated_input(uuid4(), run.id) is None
    async with factory() as session:
        turn = present(await session.get(RunAuthenticatedInputRow, evidence.turn_id))
        assert turn.created_at is not None


@pytest.mark.parametrize("change", ["tenant", "actor", "role", "message", "hash"])
async def test_invalid_input_has_no_partial_rows(
    auth_session_factory: SessionFactory, change: str
) -> None:
    repository = RunRepository(auth_session_factory)
    user = principal()
    evidence = _capture_authenticated_input(user, "original", {})
    if change == "tenant":
        evidence = replace(evidence, tenant_id=uuid4())
    elif change == "actor":
        evidence = replace(evidence, actor_id=uuid4())
    elif change == "role":
        evidence = replace(evidence, actor_role=Role.ADMIN)
    elif change == "message":
        evidence = replace(evidence, message="different")
    else:
        evidence = replace(evidence, message_sha256="0" * 64)
    with pytest.raises(ValueError):
        await create(repository, user, evidence=evidence)
    assert await counts(auth_session_factory) == (0, 0, 0, 0)


async def test_reuse_cannot_authorize_a_second_run(auth_session_factory: SessionFactory) -> None:
    repository = RunRepository(auth_session_factory)
    user = principal()
    evidence = _capture_authenticated_input(user, "original", {})
    await create(repository, user, evidence=evidence)
    with pytest.raises(IntegrityError):
        await create(repository, user, evidence=evidence)
    assert await counts(auth_session_factory) == (1, 1, 1, 1)


async def test_create_rollback_preserves_no_turn_or_outbox(
    auth_session_factory: SessionFactory,
) -> None:
    repository = RunRepository(auth_session_factory)
    user = principal()
    # A missing predecessor violates FK after insertion within the same transaction.
    with pytest.raises(IntegrityError):
        await create(
            repository,
            user,
            evidence=_capture_authenticated_input(user, "original", {}),
            blocked=uuid4(),
        )
    assert await counts(auth_session_factory) == (0, 0, 0, 0)


async def test_queue_edit_appends_turn_and_stale_edit_leaves_originals(
    auth_session_factory: SessionFactory,
) -> None:
    user, repository, queue, item, original = await queued(auth_session_factory)
    edited = _capture_authenticated_input(user, "  edited 文本  ", {"operation": "queue_edit"})
    result = await queue.edit(
        user.tenant_id,
        item.id,
        expected_version=1,
        message=edited.message,
        authenticated_input=edited,
    )
    assert result.version == 2
    assert result.message == edited.message.strip()
    assert (
        await repository.get(user.tenant_id, item.successor_run_id)
    ).request == edited.message.strip()
    assert (
        await repository.load_authenticated_input(user.tenant_id, item.successor_run_id) == edited
    )
    with pytest.raises(ConversationQueueConflict):
        await queue.edit(
            user.tenant_id,
            item.id,
            expected_version=1,
            message="stale",
            authenticated_input=_capture_authenticated_input(user, "stale", {}),
        )
    assert await counts(auth_session_factory) == (2, 2, 1, 1)
    async with auth_session_factory() as session:
        assert (
            present(await session.get(RunAuthenticatedInputRow, original.turn_id)).message
            == "original"
        )
    assert (
        await repository.load_authenticated_input(user.tenant_id, item.successor_run_id) == edited
    )


@pytest.mark.parametrize("change", ["actor", "role", "tenant"])
async def test_edit_cannot_upgrade_successor_identity(
    auth_session_factory: SessionFactory, change: str
) -> None:
    user, repository, queue, item, original = await queued(auth_session_factory)
    other = AuthenticatedPrincipal(
        uuid4() if change == "actor" else user.user_id,
        uuid4() if change == "tenant" else user.tenant_id,
        Role.ADMIN if change == "role" else user.role,
    )
    with pytest.raises(ConversationQueueConflict):
        await queue.edit(
            user.tenant_id,
            item.id,
            expected_version=1,
            message="new",
            authenticated_input=_capture_authenticated_input(other, "new", {}),
        )
    assert await counts(auth_session_factory) == (2, 1, 1, 1)
    assert (await queue.get(user.tenant_id, item.id)).version == 1
    assert (
        await repository.load_authenticated_input(user.tenant_id, item.successor_run_id) == original
    )


async def test_internal_edit_invalidates_current_link_without_promoting_text(
    auth_session_factory: SessionFactory,
) -> None:
    user, repository, queue, item, original = await queued(auth_session_factory)
    await queue.edit(user.tenant_id, item.id, expected_version=1, message="internal new text")
    assert await repository.load_authenticated_input(user.tenant_id, item.successor_run_id) is None
    assert await counts(auth_session_factory) == (2, 1, 0, 1)
    async with auth_session_factory() as session:
        assert await session.get(RunAuthenticatedInputRow, original.turn_id) is not None


async def test_edit_duplicate_turn_rolls_back_request_version_and_link(
    auth_session_factory: SessionFactory,
) -> None:
    user, repository, queue, item, original = await queued(auth_session_factory)
    # Reusing the same turn PK fails after the edit mutations have been staged.
    with pytest.raises(IntegrityError):
        await queue.edit(
            user.tenant_id,
            item.id,
            expected_version=1,
            message="original",
            authenticated_input=original,
        )
    assert (await queue.get(user.tenant_id, item.id)).version == 1
    assert (await repository.get(user.tenant_id, item.successor_run_id)).request == "original"
    assert (
        await repository.load_authenticated_input(user.tenant_id, item.successor_run_id) == original
    )
    assert await counts(auth_session_factory) == (2, 1, 1, 1)


async def test_racing_edits_have_one_winner_and_no_losing_turn(
    auth_session_factory: SessionFactory,
) -> None:
    user, repository, queue, item, _original = await queued(auth_session_factory)
    inputs = [_capture_authenticated_input(user, message, {}) for message in ("first", "second")]

    async def edit(evidence: _AuthenticatedInput) -> ConversationQueueItem:
        return await queue.edit(
            user.tenant_id,
            item.id,
            expected_version=1,
            message=evidence.message,
            authenticated_input=evidence,
        )

    results = await asyncio.gather(*(edit(e) for e in inputs), return_exceptions=True)
    assert sum(isinstance(x, ConversationQueueConflict) for x in results) == 1
    assert sum(not isinstance(x, BaseException) for x in results) == 1
    loaded = await repository.load_authenticated_input(user.tenant_id, item.successor_run_id)
    assert loaded in inputs
    assert (
        present(loaded).message
        == (await repository.get(user.tenant_id, item.successor_run_id)).request
    )
    assert await counts(auth_session_factory) == (2, 2, 1, 1)


async def test_legacy_and_cross_run_have_no_provenance_and_same_run_repair_retains_original(
    auth_session_factory: SessionFactory,
) -> None:
    user = principal()
    repository = RunRepository(auth_session_factory)
    original = _capture_authenticated_input(user, "original", {})
    run = await create(repository, user, evidence=original)
    legacy = await create(repository, user)
    assert await repository.load_authenticated_input(user.tenant_id, legacy.id) is None
    async with auth_session_factory() as session, session.begin():
        await session.execute(
            update(RunRow)
            .where(RunRow.id == run.id)
            .values(
                request="internal derived repair text", routing_decision={"source": "self_repair"}
            )
        )
    assert await repository.load_authenticated_input(user.tenant_id, run.id) == original
    async with auth_session_factory() as session, session.begin():
        await session.execute(update(RunRow).where(RunRow.id == run.id).values(actor_id=uuid4()))
    assert await repository.load_authenticated_input(user.tenant_id, run.id) is None


async def test_missing_link_and_missing_turn_fail_closed(
    auth_session_factory: SessionFactory,
) -> None:
    user = principal()
    repository = RunRepository(auth_session_factory)
    original = _capture_authenticated_input(user, "original", {})
    run = await create(repository, user, evidence=original)
    async with auth_session_factory() as session, session.begin():
        await session.execute(
            delete(RunAuthenticatedInputRow).where(
                RunAuthenticatedInputRow.turn_id == original.turn_id
            )
        )
    assert await repository.load_authenticated_input(user.tenant_id, run.id) is None


async def test_constraint_blocks_cross_run_link(auth_session_factory: SessionFactory) -> None:
    user = principal()
    repository = RunRepository(auth_session_factory)
    original = _capture_authenticated_input(user, "original", {})
    first = await create(repository, user, evidence=original)
    second = await create(repository, user)
    with pytest.raises(IntegrityError):
        async with auth_session_factory() as session, session.begin():
            session.add(
                RunInputAuthorizationRow(
                    run_id=second.id, tenant_id=user.tenant_id, turn_id=original.turn_id
                )
            )
    assert await repository.load_authenticated_input(user.tenant_id, first.id) == original
    assert await repository.load_authenticated_input(user.tenant_id, second.id) is None


async def test_racing_idempotent_submissions_preserve_only_winning_original(
    auth_session_factory: SessionFactory,
) -> None:
    user = principal()
    repository = RunRepository(auth_session_factory)
    inputs = [_capture_authenticated_input(user, message, {}) for message in ("first", "second")]
    results = await asyncio.gather(
        *(create(repository, user, e.message, e, key="race") for e in inputs)
    )
    assert results[0].id == results[1].id
    assert await counts(auth_session_factory) == (1, 1, 1, 1)
    loaded = await repository.load_authenticated_input(user.tenant_id, results[0].id)
    assert loaded in inputs
    assert present(loaded).message == results[0].request == results[1].request


async def test_populated_downgrade_refuses_original_loss(
    auth_session_factory: SessionFactory,
) -> None:
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.exc import DBAPIError

    user = principal()
    repository = RunRepository(auth_session_factory)
    await create(repository, user, evidence=_capture_authenticated_input(user, "original", {}))
    path = Path(__file__).resolve().parents[3] / "alembic/versions/0034_authenticated_inputs.py"
    spec = spec_from_file_location("authenticated_input_downgrade", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    assert isinstance(module, DowngradeModule)
    with pytest.raises(DBAPIError):
        async with auth_session_factory() as session, session.begin():
            connection = await session.connection()

            def downgrade(sync_connection: Connection) -> None:
                assign_attribute(
                    module, "op", Operations(MigrationContext.configure(sync_connection))
                )
                module.downgrade()

            await connection.run_sync(downgrade)
    assert await counts(auth_session_factory) == (1, 1, 1, 1)


async def test_authenticated_queue_api_edit_keeps_raw_utf8_and_original_selection(
    auth_session_factory: SessionFactory,
) -> None:
    import json

    from agent_hub.api.routers import runs
    from agent_hub.runs.service import RunService
    from agent_hub.runtime.defaults import UnavailableRuntime
    from agent_hub.runtime.registry import RuntimeRegistry

    user = principal()
    repository = RunRepository(auth_session_factory)
    await create(repository, user, "predecessor")
    service = RunService(
        repository,
        runtime_registry=RuntimeRegistry((UnavailableRuntime(TaskMode.DIRECT),)),
        router=None,
        task_queue=UnexpectedTaskQueue(),
        conversation_queue_repository=ConversationQueueRepository(auth_session_factory),
    )
    request = Request({"type": "http", "app": FastAPI()})
    body = runs.CreateRunRequest(
        message="  queued 原文  ",
        mode=TaskMode.DIRECT,
        project_id="selected-project",
        workspace_session_id="selected-session",
        permission_mode="read_only",
        direct_model="model-a",
    )
    queued_item = await runs.queue_conversation_message(
        "conv-proof", body, request, service, user, "full-api-proof"
    )
    original = present(
        await repository.load_authenticated_input(user.tenant_id, queued_item.successor_run_id)
    )
    assert original.message == "  queued 原文  "
    edited = await runs.edit_conversation_queue_item(
        queued_item.id,
        runs.QueueItemEditRequest(version=1, message="  edited 文本\n"),
        request,
        service,
        user,
    )
    current = present(
        await repository.load_authenticated_input(user.tenant_id, edited.successor_run_id)
    )
    assert current.turn_id != original.turn_id
    assert current.message == "  edited 文本\n"
    assert (await repository.get(user.tenant_id, edited.successor_run_id)).request == "edited 文本"
    scope = json.loads(current.input_scope)
    assert scope["selection"]["project_id"] == "selected-project"
    assert scope["selection"]["workspace_session_id"] == "selected-session"
    assert scope["selection"]["permission_mode"] == "read_only"
    assert scope["selection"]["direct_model"] == "model-a"
    assert scope["unchanged_selection_from_turn"] == str(original.turn_id)
    async with auth_session_factory() as session:
        assert (
            present(await session.get(RunAuthenticatedInputRow, original.turn_id)).message
            == original.message
        )


async def test_persisted_hash_corruption_is_not_review_authority(
    auth_session_factory: SessionFactory,
) -> None:
    user = principal()
    repository = RunRepository(auth_session_factory)
    original = _capture_authenticated_input(user, "original", {"project_id": "selected-project"})
    run = await create(repository, user, evidence=original)
    async with auth_session_factory() as session, session.begin():
        # Deliberate DB corruption for the fail-closed reader, never an application update path.
        await session.execute(
            update(RunAuthenticatedInputRow)
            .where(RunAuthenticatedInputRow.turn_id == original.turn_id)
            .values(message="corrupted")
        )
    assert await repository.load_authenticated_input(user.tenant_id, run.id) is None
