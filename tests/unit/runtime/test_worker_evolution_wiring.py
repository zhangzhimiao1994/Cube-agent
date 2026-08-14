from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID

from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.config.service import ConfigService
from agent_hub.evolution_hooks import EvolutionExecutionIngestHook
from agent_hub.runs.repository import RunRepository
from agent_hub.runtime import worker
from agent_hub.security.secrets import SecretService

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")


def test_worker_builds_evolution_terminal_hook(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakePersistentAdminResourceService:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        worker,
        "admin",
        SimpleNamespace(PersistentAdminResourceService=FakePersistentAdminResourceService),
        raising=False,
    )

    hooks = worker._evolution_terminal_hooks(
        config_service=cast(ConfigService, object()),
        secret_service=cast(SecretService, object()),
        tenant_id=TENANT_ID,
        run_repository=cast(RunRepository, object()),
        session_factory=cast(async_sessionmaker[AsyncSession], object()),
        skill_store_dir=tmp_path,
    )

    assert len(hooks) == 1
    assert isinstance(hooks[0], EvolutionExecutionIngestHook)
    assert captured["tenant_id"] == TENANT_ID
    assert captured["actor_id"] == TENANT_ID
    assert captured["run_repository"] is not None
    assert captured["session_factory"] is not None
    assert captured["skill_store_dir"] == tmp_path
