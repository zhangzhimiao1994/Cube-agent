from pathlib import Path

from agent_hub.runtime.crew.adapter import CrewAIObjectFactory, CrewDispatchRuntime


def test_crewai_factory_defaults_to_state_directory(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("AGENT_HUB_CREWAI_STORAGE_DIR", raising=False)

    factory = CrewAIObjectFactory()

    assert factory._storage_dir == Path("/var/lib/agent-hub/crewai").resolve()


def test_crewai_factory_storage_directory_can_be_overridden(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    storage = Path.cwd() / ".tmp-test-crewai-storage"
    monkeypatch.setenv("AGENT_HUB_CREWAI_STORAGE_DIR", str(storage))

    factory = CrewAIObjectFactory()

    assert factory._storage_dir == storage.resolve()


def test_dispatch_runtime_uses_crewai_state_directory_by_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("AGENT_HUB_CREWAI_STORAGE_DIR", raising=False)
    runtime = CrewDispatchRuntime.__new__(CrewDispatchRuntime)

    assert runtime._default_crewai_storage_dir() == Path("/var/lib/agent-hub/crewai").resolve()
