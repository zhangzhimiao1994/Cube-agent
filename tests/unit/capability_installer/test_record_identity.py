"""Stored environment records must remain bound to their selected identity."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_hub.capability_installer.environment import (
    CapabilityEnvironmentManager,
    CliArtifactSpec,
    EnvironmentBuildError,
)


def _installed(tmp_path: Path) -> tuple[CapabilityEnvironmentManager, Path]:
    source = tmp_path / "artifact"
    source.write_bytes(b"controlled-cli")
    manager = CapabilityEnvironmentManager(tmp_path / "envs", cli_probe=lambda *_: None)
    record = manager.deploy_cli(
        CliArtifactSpec(
            "tool", "1.0.0", source, hashlib.sha256(source.read_bytes()).hexdigest(), "tool"
        )
    )
    return manager, record.path


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capability_id", "other"),
        ("version", "2.0.0"),
        ("kind", "unknown"),
        ("executable", "../outside"),
        ("executable", "/outside"),
        ("executable", "C:\\outside.exe"),
        ("executable", "nested/tool"),
        ("content_sha256", "not-a-hash"),
        ("capability_id", ["tool"]),
    ],
)
def test_active_rejects_record_identity_or_executable_corruption(
    tmp_path: Path, field: str, value: object
) -> None:
    manager, root = _installed(tmp_path)
    metadata = root / "environment.json"
    payload = json.loads(metadata.read_bytes())
    payload[field] = value
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    before = (tmp_path / "envs/tool/state.json").read_bytes()

    with pytest.raises(EnvironmentBuildError):
        manager.active("tool")

    assert (tmp_path / "envs/tool/state.json").read_bytes() == before


def test_active_rejects_nonobject_record_without_raw_attribute_error(tmp_path: Path) -> None:
    manager, root = _installed(tmp_path)
    (root / "environment.json").write_text("[]", encoding="ascii")
    with pytest.raises(EnvironmentBuildError):
        manager.active("tool")


def test_active_version_cannot_traverse_outside_version_directory(tmp_path: Path) -> None:
    manager, _ = _installed(tmp_path)
    outside = tmp_path / "envs/tool/outside"
    outside.mkdir()
    (outside / "environment.json").write_text(
        json.dumps(
            {
                "capability_id": "tool",
                "version": "1.0.0",
                "kind": "cli",
                "executable": "tool",
                "content_sha256": "a" * 64,
            }
        ),
        encoding="ascii",
    )
    state = tmp_path / "envs/tool/state.json"
    payload = json.loads(state.read_bytes())
    payload["active_version"] = "../outside"
    state.write_text(json.dumps(payload), encoding="ascii")
    with pytest.raises(EnvironmentBuildError):
        manager.active("tool")


@pytest.mark.parametrize("case", ["missing", "tampered", "hardlinked"])
def test_active_cli_rechecks_regular_single_link_and_locked_content(
    tmp_path: Path, case: str
) -> None:
    import os

    manager, root = _installed(tmp_path)
    executable = root / "tool"
    if case == "missing":
        executable.unlink()
    elif case == "tampered":
        executable.write_bytes(b"changed")
    else:
        os.link(executable, tmp_path / "linked-fixture")
    with pytest.raises(EnvironmentBuildError):
        manager.active("tool")
