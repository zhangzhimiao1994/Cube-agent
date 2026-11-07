"""Real Linux Root fixtures; never open a production cache or run project code."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agent_hub.skills.sandbox import python_project_backend as root
from agent_hub.skills.sandbox import python_project_cache as cache_api
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.python_premodel_retirement import (
    PythonStoppedPremodelMaintenanceRequest,
)
from agent_hub.skills.sandbox.python_premodel_retirement_backend import maintain_stopped_premodel
from agent_hub.skills.sandbox.python_project_protocol import PythonFailedMaintenanceInvocation
from tests.unit.skills.sandbox.test_python_project_backend import configuration_payload
from tests.unit.skills.sandbox.test_python_project_cache import invocation, ready

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(os, "geteuid"), reason="actual Linux Root required",
)


def test_native_inspector_mount_namespace_is_not_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.skills.sandbox import python_premodel_retirement_backend as backend

    if os.geteuid() != 0:
        pytest.skip("actual Root required")

    def forbidden(*args: object) -> None:
        pytest.fail("own mount must veto before scanning another PID")

    monkeypatch.setattr(backend.os, "scandir", forbidden)
    # The real inspector namespace always mounts /. Reading that actual
    # kernel record proves the own-PID exemption cannot hide its mounts.
    with pytest.raises(ValueError, match="mounted consumer"):
        backend.require_no_native_consumers((Path("/"),), time.monotonic() + 5)


def marker(path: Path, execution: str, state: str, owner: PythonFailedMaintenanceInvocation) -> bytes:
    path.mkdir(mode=0o700)
    value = {key: str(getattr(owner, key)) for key in
             ("tenant_id", "run_id", "project_id", "session_id")}
    value.update(execution_id=execution, source_sha256="b" * 64, state=state,
                 unit=f"agent-hub-project-{execution}.service")
    raw = (json.dumps(value, indent=2) + "\n").encode("ascii")
    control = path / "execution.json"
    control.write_bytes(raw)
    control.chmod(0o600)
    return raw


@pytest.mark.parametrize("defect", [None, "changed-control", "missing-origin"])
def test_native_root_entry_retains_unknown_controls_and_rejects_changed_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str | None,
) -> None:
    if os.geteuid() != 0:
        pytest.skip("actual Root required")
    cache, reference = ready(cache_api, tmp_path)
    original = invocation()
    owner = PythonFailedMaintenanceInvocation(
        execution_id="python_release_expired_" + uuid4().hex,
        tenant_id=original.tenant_id, run_id=original.run_id,
        project_id=original.project_id, session_id=original.session_id,
    )
    stage_root, runtime_root = tmp_path / "stages", tmp_path / "runtime-stages"
    stage_root.mkdir(mode=0o700)
    runtime_root.mkdir(mode=0o700)
    own = stage_root / (owner.execution_id + "-abcd1234")
    marker(own, owner.execution_id, "RUNNING", owner)
    first = runtime_root / "original-abcd1234"
    original_raw = marker(first, "original", "UNKNOWN", owner)
    second = stage_root / "other_abandoned-abcd1234"
    second_raw = marker(second, "other_abandoned", "RUNNING", owner)
    configuration = replace(root.RootConfig.from_payload(configuration_payload()),
                            environment_root=cache.root, stage_root=stage_root,
                            cache_policy=cache.policy)
    policy = replace(BrokerPolicy(), runtime_root=runtime_root)
    # Advance the fixture's cache age; all process, unit and FD observations are real.
    now = time.time()
    monkeypatch.setattr(cache_api.time, "time", lambda: now + cache.policy.retention_seconds + 1)
    request = PythonStoppedPremodelMaintenanceRequest(invocation=owner, phase="inspect")
    stage_fd = os.open(stage_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with root._admission(stage_fd, time.monotonic() + 40):
            response = maintain_stopped_premodel(request, policy, configuration,
                                                 time.monotonic() + 35, stage=own)
            response.require_for(request)
        snapshot = response.retirement_snapshot
        assert snapshot is not None and len(snapshot.stages) == 2
        assert {s.root for s in snapshot.stages} == {"runtime", "stage"}
        assert snapshot.reference_sha256 == hashlib.sha256(snapshot.reference.encode()).hexdigest()
        assert json.loads(snapshot.reference) == reference.model_dump(mode="json")
        # The Root fixture checks transport binding, not database authorization.
        # Real lifecycle tests separately require permanent repository readback.
        archive = snapshot.archive()
        commit = PythonStoppedPremodelMaintenanceRequest(
            invocation=owner, phase="commit", snapshot=snapshot,
            archive_id=archive.id, archive_sha256=archive.content_sha256,
        )
        if defect == "changed-control":
            (second / "execution.json").write_bytes(second_raw + b" ")
        elif defect == "missing-origin":
            (first / "execution.json").unlink()
            first.rmdir()
        with root._admission(stage_fd, time.monotonic() + 40):
            if defect:
                with pytest.raises((ValueError, OSError)):
                    maintain_stopped_premodel(commit, policy, configuration,
                                              time.monotonic() + 35, stage=own)
                assert (cache.root / snapshot.cache_key).exists()
            else:
                done = maintain_stopped_premodel(commit, policy, configuration,
                                                 time.monotonic() + 35, stage=own)
                done.require_for(commit)
                assert done.retirement_removed
                assert not (cache.root / snapshot.cache_key).exists()
        if defect != "missing-origin":
            assert (first / "execution.json").read_bytes() == original_raw
        assert (second / "execution.json").read_bytes() == second_raw + (
            b" " if defect == "changed-control" else b""
        )
        if defect is None:
            with (pytest.raises(cache_api.PythonProjectCacheError, match="cache_incomplete"),
                  cache.reserve(original, next_bytes=1024, deadline=time.monotonic() + 5)):
                pytest.fail("permanently retired original owner was admitted")
    finally:
        os.close(stage_fd)
