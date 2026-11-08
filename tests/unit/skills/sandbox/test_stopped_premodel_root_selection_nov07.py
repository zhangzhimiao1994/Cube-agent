"""A base broker's socket root must not be traversed as retained executions."""
from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_hub.skills.sandbox import project_contract_broker as controls
from agent_hub.skills.sandbox import python_premodel_retirement_backend as backend
from agent_hub.skills.sandbox.broker import BrokerPolicy
from tests.unit.skills.sandbox.test_stopped_premodel_stage_metadata_nov07 import owner


class ExecutionRootReached(Exception):
    pass


@pytest.mark.parametrize("refuse_execution_root,same_root", [(False, False), (True, False), (False, True)])
def test_base_policy_reads_project_execution_root_without_opening_socket_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refuse_execution_root: bool, same_root: bool,
) -> None:
    socket_root = tmp_path / "agent-hub"
    socket_root.mkdir()
    for name in ("preview-broker.sock", "skill-broker.sock"):
        (socket_root / name).touch()
    project_root = tmp_path / "agent-hub-project"
    project_root.mkdir()
    stage_root = tmp_path / "python-project-stages"
    stage_root.mkdir()
    if same_root:
        project_root = stage_root
    policy = replace(BrokerPolicy(), runtime_root=socket_root, project_runtime_root=project_root)
    selected = []
    real_scandir = os.scandir

    # Native directory-FD opening is Linux-only. Stop at that exact boundary:
    # this test never supplies a fake successful quiescence or retirement result.
    def root(path: Path, uid: int, stack: object, pins: object, *, stage: bool) -> int:
        assert uid == 0 and stage is True
        selected.append(path)
        if path == stage_root:
            return 912345
        if path != project_root:
            raise AssertionError("socket directory is not an execution root")
        if refuse_execution_root:
            raise PermissionError("execution root is untrusted")
        raise ExecutionRootReached

    @contextmanager
    def scandir(path: object) -> Iterator[Iterator[os.DirEntry[str]]]:
        assert path == 912345
        with real_scandir(stage_root) as entries:
            yield entries

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(controls, "_root", root)
    error = ValueError if same_root else PermissionError if refuse_execution_root else ExecutionRootReached
    # This test deliberately provides only stage metadata on Windows. It stops
    # at _root, before a complete Linux RootConfig could authorize any IO.
    target: object = backend.capture_stopped_stages
    assert callable(target)
    with pytest.raises(error):
        target(
            owner(), "python_old", policy, SimpleNamespace(stage_root=stage_root),
            stage_root / (owner().execution_id + "-abcd1234"), time.monotonic() + 5,
        )
    assert selected == ([stage_root] if same_root else [stage_root, project_root])
    with real_scandir(socket_root) as entries:
        assert sorted(p.name for p in entries) == ["preview-broker.sock", "skill-broker.sock"]
