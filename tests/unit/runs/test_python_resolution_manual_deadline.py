"""The manual grant gate must respect the protected original's wall-clock cutoff."""

from datetime import UTC, datetime, timedelta, tzinfo
from types import SimpleNamespace
from typing import Self, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.capabilities import python_resolution_authorization
from agent_hub.domain.runs import TaskMode
from agent_hub.runs import repository as repository_module
from agent_hub.runs.repository import RunConflict, RunRepository
from tests.unit.runs.test_python_resolution_public_projection import object_map, pending


@pytest.mark.parametrize('seconds_left', [-1, 0, 1], ids=['expired', 'at-deadline', 'future'])
async def test_protected_original_publication_manual_grant_deadline(
    seconds_left: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 11, 9, 8, 2, 30, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(now.timestamp(), tz)

    monkeypatch.setattr(repository_module, 'datetime', FixedDatetime)
    monkeypatch.setattr(python_resolution_authorization, 'datetime', FixedDatetime)
    case = pending(native=False, mode=TaskMode.DISCUSS)
    binding = object_map(case.approval.payload['python_resolution_binding'])
    binding['schema_version'] = 2
    binding['arguments'] = {
        'purpose': 'python_dependency_publication',
        'original_deadline': (now + timedelta(seconds=seconds_left)).isoformat(),
    }
    # The binding matcher is exercised separately. Keep the locked manual gate real.
    monkeypatch.setattr(python_resolution_authorization, 'resolution_binding_matches', lambda *_: True)
    session = cast(AsyncSession, SimpleNamespace(scalar=AsyncMock(return_value=case.approval)))
    if seconds_left <= 0:
        with pytest.raises(RunConflict):
            await RunRepository._require_resolution_manual_approval(
                session, case.run, approval_id=case.approval.approval_id, fingerprint='b' * 64,
            )
    else:
        assert await RunRepository._require_resolution_manual_approval(
            session, case.run, approval_id=case.approval.approval_id, fingerprint='b' * 64,
        )


async def test_legacy_resolution_manual_grant_without_deadline() -> None:
    case = pending(native=False)
    session = cast(AsyncSession, SimpleNamespace(scalar=AsyncMock(return_value=case.approval)))
    assert await RunRepository._require_resolution_manual_approval(
        session, case.run, approval_id=case.approval.approval_id, fingerprint='b' * 64,
    )
