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
from agent_hub.settings import Settings

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")


def test_worker_runtime_stack_uses_configured_generated_artifact_dir(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class FakeDatabase:
        session_factory = object()

    class FakeRedis:
        @classmethod
        def from_url(cls, url: str) -> FakeRedis:
            captured["redis_url"] = url
            return cls()

    class FakeSettings:
        bootstrap_tenant_id = TENANT_ID
        skill_store_dir = tmp_path / "skills"
        attachment_store_dir = tmp_path / "attachments"
        generated_artifact_dir = tmp_path / "generated"
        runtime_timeout_seconds = 10
        runtime_token_budget = 1000

        def database_url_value(self) -> str:
            return "postgresql+asyncpg://example"

        def redis_url_value(self) -> str:
            return "redis://example"

        def master_key_bytes(self) -> bytes:
            return b"0" * 32

    class FakeRuntimeStack:
        runtime_gateway = object()
        harness_tool_gateway = object()

    def fake_build_runtime_capability_stack(**kwargs: object) -> FakeRuntimeStack:
        captured["runtime_stack"] = kwargs
        return FakeRuntimeStack()

    monkeypatch.setattr(worker, "build_database", lambda url: FakeDatabase())
    monkeypatch.setattr(worker, "Redis", FakeRedis)
    monkeypatch.setattr(worker, "ConfigService", lambda session_factory: object())
    monkeypatch.setattr(worker, "RunRepository", lambda session_factory: object())
    monkeypatch.setattr(worker, "SecretCipher", lambda key: object())
    monkeypatch.setattr(worker, "SecretService", lambda session_factory, cipher: object())
    monkeypatch.setattr(worker, "PersistentHermesRunAdvisor", lambda session_factory: object())
    monkeypatch.setattr(worker, "_evolution_terminal_hooks", lambda **kwargs: ())
    monkeypatch.setattr(worker, "configured_runtime_registry", lambda **kwargs: object())
    monkeypatch.setattr(worker, "RunService", lambda *args, **kwargs: object())
    monkeypatch.setattr(worker, "build_runtime_capability_stack", fake_build_runtime_capability_stack)

    worker.build_worker_service(cast(Settings, FakeSettings()))

    runtime_stack = captured["runtime_stack"]
    assert isinstance(runtime_stack, dict)
    assert runtime_stack["generated_artifact_dir"] == tmp_path / "generated"


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
