"""Conversation candidates normalize trusted database identities before runtime use."""
from collections.abc import Coroutine
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from asyncpg.pgproto.pgproto import UUID as DriverUUID  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.runs.repository import ConversationContextItem, RunRepository


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """The fake session must complete without scheduling network or event-loop IO."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return cast(T, completed.value)
    finally:
        coroutine.close()
    raise AssertionError("fake database unexpectedly suspended")


def _candidates(identifier: object | None, *, bounded: bool) -> tuple[ConversationContextItem, ...]:
    session = MagicMock()
    session.__aenter__.return_value = session
    now = datetime.now(UTC)
    rows = [] if identifier is None else [SimpleNamespace(
        id=identifier, created_at=now, request="Original delivery request")]
    session.scalar = AsyncMock(side_effect=[now, rows[0] if rows else None])
    artifact_rows = [] if identifier is None else [SimpleNamespace(
        run_id=identifier, payload={"type": "text", "content": {"text": "Prior report"}})]
    session.scalars = AsyncMock(side_effect=[
        SimpleNamespace(all=lambda: rows), SimpleNamespace(all=lambda: artifact_rows)])
    repository = RunRepository(cast(async_sessionmaker[AsyncSession], lambda: session))
    return _run(repository.conversation_context(
        uuid4(), "same-conversation", before_run_id=uuid4(),
        candidate_limit=8 if bounded else None, limit=32 if bounded else None))


@pytest.mark.parametrize("bounded", [True, False])
def test_driver_uuid_is_canonicalized_at_public_conversation_dao(bounded: bool) -> None:
    identifier = DriverUUID(str(uuid4()))
    assert isinstance(identifier, UUID) and type(identifier) is not UUID

    (candidate,) = _candidates(identifier, bounded=bounded)

    assert type(candidate.run_id) is UUID
    assert candidate.run_id == identifier
    assert candidate.request == "Original delivery request"
    assert candidate.artifacts == ({"type": "text", "content": {"text": "Prior report"}},)


@pytest.mark.parametrize("bounded", [True, False])
def test_standard_uuid_and_artifact_association_are_preserved(bounded: bool) -> None:
    identifier = uuid4()

    (candidate,) = _candidates(identifier, bounded=bounded)

    assert candidate.run_id is identifier
    assert candidate.artifacts[0]["content"] == {"text": "Prior report"}


@pytest.mark.parametrize("bounded", [True, False])
def test_database_string_identity_is_rejected_instead_of_coerced(bounded: bool) -> None:
    with pytest.raises(TypeError, match="database identity must be a UUID"):
        _candidates(str(uuid4()), bounded=bounded)


@pytest.mark.parametrize("bounded", [True, False])
def test_empty_conversation_still_returns_no_candidates(bounded: bool) -> None:
    assert _candidates(None, bounded=bounded) == ()
