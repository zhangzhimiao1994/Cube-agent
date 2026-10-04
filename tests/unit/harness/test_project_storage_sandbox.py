from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from agent_hub.harness import project_validation_sandbox as sandbox
from agent_hub.harness.project_validation_result import STORAGE_PROFILE


@pytest.mark.parametrize("mutation", ["none", "legacy", "duplicate", "failed_exit", "timeout"])
def test_storage_ipc_requires_complete_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    source = Path(__file__).resolve().parents[2] / "fixtures/project_business/ultra_storage_result.json"
    payload: Any = json.loads(source.read_text(encoding="utf-8"))
    stdout = json.dumps(payload["checks"]["load"] if mutation == "legacy" else payload)
    if mutation == "duplicate":
        stdout = stdout.replace('"starts": 3', '"starts": 0, "starts": 3')
    commands: list[Any] = []

    def command(inner: Any, **kwargs: Any) -> list[str]:
        assert kwargs["shared_network"] is False and kwargs["cwd"] == tmp_path
        commands.append(inner)
        return ["bwrap", *inner]

    monkeypatch.setattr(sandbox, "sandbox_command", command)
    run = Mock(return_value=subprocess.CompletedProcess(
        [], 1 if mutation == "failed_exit" else 0, stdout, "",
    ))
    if mutation == "timeout":
        run.side_effect = subprocess.TimeoutExpired("bwrap", 10)
    monkeypatch.setattr(subprocess, "run", run)
    validate = getattr(sandbox, "validate_scale_storage", None)
    assert callable(validate), "public storage sandbox dispatcher is missing"
    result = validate(tmp_path, "ultra", 10)
    assert result["profile"] == STORAGE_PROFILE
    assert result["status"] == ("passed" if mutation == "none" else "unknown")
    assert commands[0][-3:] == ("portfolio-storage", "ultra", "10")


def test_storage_unavailable_never_runs_generated_code_on_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "sandbox_available", lambda: False)
    run = Mock(side_effect=AssertionError("host execution forbidden"))
    monkeypatch.setattr(subprocess, "run", run)
    validate = getattr(sandbox, "validate_scale_storage", None)
    assert callable(validate), "public storage sandbox dispatcher is missing"
    result = validate(tmp_path, "ultra", 10)
    assert result["profile"] == STORAGE_PROFILE and result["status"] == "unknown"
    run.assert_not_called()
