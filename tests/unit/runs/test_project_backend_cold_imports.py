"""Production helper modules must load without pytest's warmed import graph."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", [
    "agent_hub.skills.sandbox.python_project_backend",
    "agent_hub.skills.sandbox.python_project_client",
    "agent_hub.skills.sandbox.python_project_broker",
    "agent_hub.skills.sandbox.broker",
    "agent_hub.skills.sandbox.remote_runner",
    "agent_hub.capabilities.project_baselines",
    "agent_hub.plugins.handover_sources",
    "agent_hub.skills.sandbox.project_runner",
    "agent_hub.runtime.worker",
])
def test_project_entry_cold_import_is_isolated_and_side_effect_free(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c",
         "import importlib,sys; importlib.import_module(sys.argv[1]); print('IMPORTED')", module],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "IMPORTED\n" and result.stderr == ""
