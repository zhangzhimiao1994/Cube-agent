from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import textwrap
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def test_skill_runner_executes_valid_self_contained_package() -> None:
    with _workspace_tmpdir() as tmp_path:
        package = _skill_zip(
            tmp_path,
            entry_body=(
                "import json, sys\n"
                "payload = json.load(sys.stdin)\n"
                "print('hello ' + payload['name'])\n"
            ),
        )

        result = _run_runner(
            package,
            tmp_path,
            input_text='{"name":"agent"}',
            sha256=hashlib.sha256(package.read_bytes()).hexdigest(),
        )

    assert result.returncode == 0
    assert result.stdout == "hello agent\n"
    assert result.stderr == ""


def test_skill_runner_rejects_package_hash_mismatch() -> None:
    with _workspace_tmpdir() as tmp_path:
        package = _skill_zip(tmp_path)

        result = _run_runner(package, tmp_path, input_text="{}", sha256="0" * 64)

    assert result.returncode == 78
    assert "sha256 does not match" in result.stderr


def test_skill_runner_rejects_runtime_dependency_install() -> None:
    with _workspace_tmpdir() as tmp_path:
        package = _skill_zip(tmp_path, requirements="pydantic==2.10.0\n")

        result = _run_runner(
            package,
            tmp_path,
            input_text="{}",
            sha256=hashlib.sha256(package.read_bytes()).hexdigest(),
        )

    assert result.returncode == 78
    assert "dependencies are not installed" in result.stderr


def _run_runner(
    package: Path,
    tmp_path: Path,
    *,
    input_text: str,
    sha256: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        AGENT_HUB_PACKAGE_PATH=str(package),
        AGENT_HUB_PACKAGE_SHA256=sha256,
        AGENT_HUB_WORKDIR=str(tmp_path / "work"),
        AGENT_HUB_TIMEOUT_SECONDS="5",
        PYTHONPATH=str(Path.cwd() / "src"),
    )
    return subprocess.run(
        (sys.executable, "-m", "agent_hub.skills.runner"),
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _skill_zip(
    tmp_path: Path,
    *,
    entry_body: str = "print('ok')\n",
    requirements: str = "",
) -> Path:
    package = tmp_path / "skill.zip"
    dependency_hash = hashlib.sha256(requirements.encode()).hexdigest()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "skill.yaml",
            textwrap.dedent(
                f"""\
                name: demo_skill
                version: 1.0.0
                entry_point: main.py
                compatible_runtime: python3.12
                declared_tools:
                  []
                network_policy:
                  mode: none
                  allow_hosts:
                    []
                writable_paths:
                  []
                env_secret_refs:
                  []
                dependency_lock_hash: "{dependency_hash}"
                """
            ),
        )
        archive.writestr("main.py", entry_body)
        if requirements:
            archive.writestr("requirements.txt", requirements)
    return package


@contextmanager
def _workspace_tmpdir() -> Iterator[Path]:
    root = Path.cwd() / ".pytest-tmp"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        yield Path(directory)
