from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from agent_hub.skills.package import InvalidSkillPackage, SkillPackageInspector

_DEFAULT_PACKAGE_PATH = Path("/package/skill.zip")
_DEFAULT_WORKDIR = Path("/workspace")
_DEPENDENCY_FILES = frozenset({"requirements.txt", "requirements.lock", "dependencies.lock"})


class SkillRunnerError(RuntimeError):
    pass


def run_skill_from_environment() -> int:
    package_path = Path(os.environ.get("AGENT_HUB_PACKAGE_PATH", str(_DEFAULT_PACKAGE_PATH)))
    expected_sha256 = os.environ.get("AGENT_HUB_PACKAGE_SHA256", "")
    workdir = Path(os.environ.get("AGENT_HUB_WORKDIR", str(_DEFAULT_WORKDIR)))
    timeout_seconds = _int_env("AGENT_HUB_TIMEOUT_SECONDS", default=300)

    stdin_bytes = sys.stdin.buffer.read()
    try:
        _validate_json_stdin(stdin_bytes)
        archive_bytes = package_path.read_bytes()
        actual_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        if expected_sha256 and actual_sha256 != expected_sha256:
            raise SkillRunnerError("skill package sha256 does not match invocation")
        inspection = SkillPackageInspector().inspect(archive_bytes)
        if inspection.content_sha256 != actual_sha256:
            raise SkillRunnerError("skill package inspection hash mismatch")
        _reject_runtime_dependency_install(archive_bytes)
        return _execute_archive(
            archive_bytes,
            entry_point=inspection.manifest.entry_point,
            workdir=workdir,
            stdin_bytes=stdin_bytes,
            timeout_seconds=timeout_seconds,
        )
    except (OSError, InvalidSkillPackage, SkillRunnerError, ValueError) as error:
        print(f"skill runner failed: {error}", file=sys.stderr)
        return 78


def _execute_archive(
    archive_bytes: bytes,
    *,
    entry_point: str,
    workdir: Path,
    stdin_bytes: bytes,
    timeout_seconds: int,
) -> int:
    workdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="skill-", dir=workdir) as temp_dir:
        package_dir = Path(temp_dir)
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            archive.extractall(package_dir)
        process = subprocess.run(
            (sys.executable, str(package_dir / entry_point)),
            input=stdin_bytes,
            cwd=package_dir,
            timeout=timeout_seconds,
            check=False,
            capture_output=True,
        )
        sys.stdout.buffer.write(process.stdout)
        sys.stderr.buffer.write(process.stderr)
        return int(process.returncode)


def _reject_runtime_dependency_install(archive_bytes: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        for name in archive.namelist():
            normalized = name.replace("\\", "/").rsplit("/", 1)[-1]
            if normalized in _DEPENDENCY_FILES and archive.read(name).strip():
                raise SkillRunnerError(
                    "skill dependencies are not installed by the base runner; "
                    "publish a self-contained skill or use a configured dependency sandbox"
                )


def _validate_json_stdin(value: bytes) -> None:
    if not value:
        return
    try:
        json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SkillRunnerError("skill input must be valid UTF-8 JSON") from error


def _int_env(name: str, *, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise SkillRunnerError(f"{name} must be an integer") from error
    if value < 1 or value > 3600:
        raise SkillRunnerError(f"{name} must be between 1 and 3600")
    return value


async def _run_service(skill: str | None) -> None:
    del skill
    raise SkillRunnerError(
        "standalone skill service is not configured; invoke skills through a sandbox invocation"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Hub isolated skill runner.")
    parser.add_argument("--skill", default=None)
    parser.parse_args()
    if os.environ.get("AGENT_HUB_PACKAGE_PATH") or _DEFAULT_PACKAGE_PATH.exists():
        raise SystemExit(run_skill_from_environment())
    raise SystemExit(str(SkillRunnerError("skill package path is not configured")))


if __name__ == "__main__":
    main()
