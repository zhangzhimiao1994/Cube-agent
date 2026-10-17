"""Independent Task2 boundary review. Doubles only, never PostgreSQL evidence."""
from __future__ import annotations

import asyncio
import traceback
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runtime.artifacts import ArtifactRepositoryError
from agent_hub.runtime.contracts import Artifact
from agent_hub.runtime.provider_continuation import ProviderContinuationRepositoryError
from tests.unit.runs.test_provider_continuation import repository, session_factory
from tests.unit.runtime.test_provider_continuation import snapshot


async def test_private_snapshot_cannot_enter_public_artifact_repository() -> None:
    value = snapshot()
    factory = MagicMock()
    with pytest.raises(ArtifactRepositoryError):
        await PostgresArtifactRepository(factory).put(value.tenant_id, value.run_id, value)
    factory.assert_not_called()
    with pytest.raises(ValueError):
        Artifact.from_payload(value.to_payload())


@pytest.mark.parametrize("point", [
    "session_enter", "transaction_enter", "id_get", "ledger", "quota", "insert", "commit", "close",
])
async def test_private_driver_failure_at_all_transaction_stages_is_context_free(point: str) -> None:
    value = snapshot()
    factory, session, transaction = session_factory()
    session.scalar.side_effect = [value.run_id, None]
    session.get.return_value = None
    usage = MagicMock()
    usage.one.return_value = (0, 0)
    session.execute.return_value = usage
    marker = "REVIEW_PRIVATE_PARAMETER_SENTINEL"
    failure = IntegrityError("private insert", {"payload": marker}, RuntimeError(marker))
    if point == "session_enter":
        session.__aenter__.side_effect = failure
    elif point == "transaction_enter":
        transaction.__aenter__.side_effect = failure
    elif point == "id_get":
        session.get.side_effect = failure
    elif point == "ledger":
        session.scalar.side_effect = [value.run_id, failure]
    elif point == "quota":
        session.execute.side_effect = failure
    elif point == "insert":
        session.add.side_effect = failure
    elif point == "commit":
        transaction.__aexit__.side_effect = failure
    else:
        session.__aexit__.side_effect = failure
    with pytest.raises(ProviderContinuationRepositoryError) as caught:
        await repository(factory).put(value)
    error = caught.value
    assert error.__context__ is error.__cause__ is None
    assert marker not in str(error) + repr(error) + "".join(traceback.format_exception(error))


async def test_cancellation_is_not_reclassified_if_transaction_cleanup_fails() -> None:
    value = snapshot()
    factory, session, transaction = session_factory()
    session.scalar.side_effect = [value.run_id, None]
    session.get.side_effect = asyncio.CancelledError()
    transaction.__aexit__.side_effect = IntegrityError("cleanup failed", {}, RuntimeError("private"))
    with pytest.raises(asyncio.CancelledError):
        await repository(factory).put(value)
    session.add.assert_not_called()
