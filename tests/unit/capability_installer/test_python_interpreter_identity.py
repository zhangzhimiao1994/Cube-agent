from __future__ import annotations

import sys
from pathlib import Path

from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    PythonEnvironmentSpec,
)
from tests.unit.capability_installer.test_environment import RecordingRunner, _hashed_lock


def test_venv_uses_canonical_interpreter_not_a_parent_environment_alias(tmp_path: Path) -> None:
    interpreter = Path(sys.executable)
    alias = interpreter.parent / ".." / interpreter.parent.name / interpreter.name
    assert alias.resolve(strict=True) == interpreter.resolve(strict=True)
    cache = tmp_path / "wheels"
    cache.mkdir()
    runner = RecordingRunner()
    manager = CapabilityEnvironmentManager(
        tmp_path / "envs", runner=runner, python_executable=str(alias)
    )

    manager.build_python(
        PythonEnvironmentSpec(
            "interpreter-fixture", "1.0.0", _hashed_lock(tmp_path / "lock"), cache, "demo"
        )
    )

    assert runner.calls[0][0][0] == str(interpreter.resolve(strict=True))
