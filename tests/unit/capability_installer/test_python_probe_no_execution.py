from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    PythonEnvironmentSpec,
)


def _wheel(cache: Path, sentinel: Path, *, executable: bool = False) -> Path:
    wheel = cache / "scope_probe_fixture-1.0.0-py3-none-any.whl"
    script = "from pathlib import Path; Path(" + repr(str(sentinel)) + ").write_text('executed')\n"
    files = {
        "scope_probe_fixture/__init__.py": script,
        "scope_probe_fixture/child.py": "VALUE = 1\n",
        "scope_probe_fixture.pth": "import pathlib; pathlib.Path("
        + repr(str(sentinel))
        + ").write_text('executed')\n",
        "scope_probe_fixture-1.0.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: scope-probe-fixture\nVersion: 1.0.0\n",
        "scope_probe_fixture-1.0.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: owned-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    if executable:
        files["scope_probe_fixture/__init__.py"] = ""
        files.pop("scope_probe_fixture.pth")
        files["scope_probe_fixture/cli.py"] = "def main():\n    print('scope-probe 1.0.0')\n"
        files["scope_probe_fixture-1.0.0.dist-info/entry_points.txt"] = (
            "[console_scripts]\nscope-probe = scope_probe_fixture.cli:main\n"
        )
    files["scope_probe_fixture-1.0.0.dist-info/RECORD"] = (
        "".join(name + ",,\n" for name in files) + "scope_probe_fixture-1.0.0.dist-info/RECORD,,\n"
    )
    with ZipFile(wheel, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return wheel


def test_real_venv_module_probe_never_runs_pth_or_parent_package(tmp_path: Path) -> None:
    cache = tmp_path / "wheels"
    cache.mkdir()
    sentinel = tmp_path / "probe-executed"
    wheel = _wheel(cache, sentinel)
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "scope-probe-fixture==1.0.0 --hash=sha256:"
        + hashlib.sha256(wheel.read_bytes()).hexdigest()
        + "\n",
        encoding="ascii",
    )
    manager = CapabilityEnvironmentManager(
        tmp_path / "environments", python_executable=sys.executable
    )
    record = manager.build_python(
        PythonEnvironmentSpec("scope-probe", "1.0.0", lock, cache, "scope_probe_fixture.child")
    )
    assert record.kind == "python" and manager.active("scope-probe") == record
    assert not sentinel.exists(), (
        "installer smoke executed untrusted startup or parent package code"
    )


def test_real_published_python_console_command_survives_staging_rename(tmp_path: Path) -> None:
    cache = tmp_path / "wheels"
    cache.mkdir()
    wheel = _wheel(cache, tmp_path / "unused", executable=True)
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "scope-probe-fixture==1.0.0 --hash=sha256:"
        + hashlib.sha256(wheel.read_bytes()).hexdigest()
        + "\n",
        encoding="ascii",
    )
    manager = CapabilityEnvironmentManager(
        tmp_path / "environments", python_executable=sys.executable
    )
    record = manager.build_python(
        PythonEnvironmentSpec(
            "scope-probe",
            "1.0.0",
            lock,
            cache,
            "scope_probe_fixture.cli",
            "scope-probe",
        )
    )
    assert not list((tmp_path / "environments/.staging").iterdir())
    result = subprocess.run(
        [str(record.path / record.executable), "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "scope-probe 1.0.0"
