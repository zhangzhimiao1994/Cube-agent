from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_hub import app as app_module
from agent_hub.capability_installer.environment import CapabilityEnvironmentManager
from tests.unit.test_app_execution_scope_policy import _application


def test_configured_policy_binds_installer_cli_probe_in_lifespan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected: list[tuple[Path, tuple[Path, ...]]] = []
    captured: list[dict[str, Any]] = []
    original = CapabilityEnvironmentManager

    def probe(executable: Path, staging: Path) -> None:
        raise AssertionError("binding fixture must not execute a probe")

    def build(path: Path, protected_paths: tuple[Path, ...]) -> Any:
        selected.append((path, protected_paths))
        return probe

    def manager(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(app_module, "build_scoped_cli_probe", build, raising=False)
    monkeypatch.setattr(app_module, "CapabilityEnvironmentManager", manager)
    application, _ = _application(tmp_path, monkeypatch)
    with TestClient(application):
        assert captured[0].get("cli_probe") is probe
    assert selected[0][0] == tmp_path / "scope.json"
    assert application.state.settings.capability_environment_root_dir in selected[0][1]
    assert application.state.settings.plugin_package_store_dir in selected[0][1]
