"""Root release scope fences; portable mocks are not Linux/native evidence."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.skills.sandbox import python_project_backend as backend
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import ProjectExecutionBroker, _Execution
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProjectBrokerRequest,
    PythonProjectBrokerResponse,
    PythonProjectInspectEnvironmentRequest,
    base_python_invocation,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.skills.sandbox.test_python_project_release import release


def test_root_release_must_apply_scope_fence_before_deleting_exact_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    removed: list[object] = []

    async def quiet(*args: Any) -> None:
        pass

    def blocked(*args: Any, **kwargs: Any) -> None:
        # Origin is quiet, but another execution in its run is retained UNKNOWN.
        raise ProcessCleanupIncomplete("python_project_cleanup_incomplete")

    def erase(*args: Any, **kwargs: Any) -> bool:
        removed.append(args)
        return True

    monkeypatch.setattr(ProjectExecutionBroker, "_retained", lambda self, key: False)
    monkeypatch.setattr(backend, "_admit_release_unit", quiet)
    monkeypatch.setattr(backend, "_release_scope_guard", blocked, raising=False)
    monkeypatch.setattr(backend, "PythonProjectEnvironmentCache", lambda *args: SimpleNamespace(
        release_completed=erase,
    ))
    config: Any = SimpleNamespace(stage_root=tmp_path, environment_root=tmp_path, cache_policy=None)
    with pytest.raises(ProcessCleanupIncomplete):
        backend._release(release(), BrokerPolicy(), config, time.monotonic() + 5,
                         stage=tmp_path / "python_check-current")
    assert removed == []


@pytest.mark.parametrize("action", ["run", "inspect", "release"])
def test_every_native_cache_consumer_and_release_share_admission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str,
) -> None:
    from tests.unit.skills.sandbox.test_python_project_backend import configuration_payload

    locked: list[bool] = []
    seen: list[str] = []
    request = release()
    if action == "run":
        request = PythonProjectBrokerRequest(invocation=request.invocation)
    elif action == "inspect":
        request = PythonProjectInspectEnvironmentRequest(invocation=request.invocation,
            expected_environment=request.expected_environment)

    @contextmanager
    def roots(*args: Any) -> Any:
        yield SimpleNamespace(stage_root_fd=42)

    @contextmanager
    def admission(*args: Any) -> Any:
        locked.append(True)
        try:
            yield
        finally:
            locked.pop()

    def handler(actual: Any, *args: Any, **kwargs: Any) -> PythonProjectBrokerResponse:
        assert locked == [True]
        seen.append(actual.action)
        if action == "release":
            assert kwargs["stage"] == tmp_path
        return PythonProjectBrokerResponse(action=actual.action,
            execution_id=actual.invocation.execution_id, ok=False,
            error="python_project_execution_unavailable")

    monkeypatch.setattr(backend, "_native", lambda: None)
    monkeypatch.setattr(backend, "_roots", roots)
    monkeypatch.setattr(backend, "_admission", admission)
    monkeypatch.setattr(backend, "_disk_budget", lambda *args, **kwargs: None)
    for name in ("_run", "_inspect", "_release"):
        monkeypatch.setattr(backend, name, handler)
    result = backend.execute_python_project_request(request, policy=BrokerPolicy(), stage=tmp_path,
        configuration=backend.RootConfig.from_payload(configuration_payload()))
    assert not result.ok and seen == [request.action] and locked == []


def marker(execution_id: object = "python_reused", **updates: object) -> dict[str, object]:
    inv = release().invocation
    return {"execution_id": execution_id, "project_id": inv.project_id,
            "run_id": str(inv.run_id), "session_id": inv.session_id,
            "source_sha256": None, "state": "UNKNOWN", "tenant_id": str(inv.tenant_id),
            "unit": f"agent-hub-project-{execution_id}.service", **updates}


@pytest.mark.parametrize("state", ["STAGED", "RUNNING", "UNKNOWN"])
def test_existing_producer_marker_is_decoded_without_an_invocation_envelope(
    state: str, tmp_path: Path,
) -> None:
    inv = release().invocation.model_copy(update={"execution_id": "python_reused"})
    broker = ProjectExecutionBroker(BrokerPolicy())
    stage = tmp_path / "python_reused-abcdefgh"
    stage.mkdir()
    broker._metadata(_Execution(base_python_invocation(inv), None, stage=stage), state)
    actual = json.loads((stage / "execution.json").read_bytes())
    assert set(actual) == set(marker())
    parsed = backend._release_stage_invocation(actual, stage.name)
    assert (parsed.tenant_id, parsed.run_id, parsed.execution_id) == (
        inv.tenant_id, inv.run_id, inv.execution_id)


@pytest.mark.parametrize("change", [
    {"state": "finished"}, {"state": []}, {"tenant_id": "bad"}, {"run_id": True},
    {"project_id": "../escape"}, {"session_id": "../escape"}, {"execution_id": "../escape"},
    {"unit": "agent-hub-project-foreign.service"}, {"source_sha256": "bad"},
    {"extra": True}, {"invocation": {}},
])
def test_marker_parser_rejects_ambiguous_scope_or_shape(change: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        backend._release_stage_invocation(marker(**change), "python_reused-abcdefgh")


@pytest.mark.parametrize("name", ["python_foreign-abcdefgh", "python_reused-"])
def test_marker_execution_must_bind_its_stage_name(name: str) -> None:
    with pytest.raises(ValueError):
        backend._release_stage_invocation(marker(), name)


@pytest.fixture
def native_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("actual no-follow FD inventory requires Linux root")
    tmp_path.chmod(0o700)
    root = tmp_path / "stages"
    root.mkdir(mode=0o700)
    inv = release().invocation
    stage = Path(tempfile.mkdtemp(prefix=inv.execution_id + "-", dir=root))
    policy = BrokerPolicy(runtime_root=tmp_path / "legacy", python_project_runtime_root=root)
    broker = ProjectExecutionBroker(policy)
    broker._metadata(_Execution(base_python_invocation(inv), None, stage=stage), "RUNNING")
    removed: list[object] = []
    calls: list[str] = []

    async def quiet(_broker: Any, job: Any) -> None:
        calls.append(job.unit)

    def erase(*args: Any, **kwargs: Any) -> bool:
        removed.append(args)
        return True

    monkeypatch.setattr(backend, "_admit_release_unit", quiet)
    monkeypatch.setattr(backend, "PythonProjectEnvironmentCache", lambda *args: SimpleNamespace(
        release_completed=erase,
    ))
    return SimpleNamespace(root=root, stage=stage, policy=policy, removed=removed, calls=calls,
        config=SimpleNamespace(stage_root=root, environment_root=tmp_path, cache_policy=None))


def retained(fixture: Any, payload: dict[str, object], *, mode: int = 0o600) -> Path:
    path: Path = fixture.root / (str(payload["execution_id"]) + "-abcdefgh")
    path.mkdir(mode=0o700)
    control = path / "execution.json"
    control.write_text(json.dumps(payload), encoding="ascii")
    control.chmod(mode)
    return control


def invoke(fixture: Any) -> Any:
    return backend._release(release(), fixture.policy, fixture.config, time.monotonic() + 5,
                            stage=fixture.stage)


@pytest.mark.parametrize("state", ["STAGED", "RUNNING", "UNKNOWN"])
def test_native_other_execution_in_same_run_vetoes_quiet_origin_release(
    native_stage: Any, state: str,
) -> None:
    control = retained(native_stage, marker(state=state))
    before = control.read_bytes()
    with pytest.raises(ProcessCleanupIncomplete):
        invoke(native_stage)
    assert native_stage.removed == []
    assert control.read_bytes() == before
    assert len(native_stage.calls) == 2  # Origin alone appeared quiet.


@pytest.mark.parametrize("field", ["tenant_id", "run_id"])
@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_native_foreign_unknown_is_preserved_without_blocking_exact_release(
    native_stage: Any, field: str, mode: int,
) -> None:
    control = retained(native_stage, marker(**{field: "00000000-0000-0000-0000-000000000099"}),
                       mode=mode)
    before = control.read_bytes()
    response = invoke(native_stage)
    assert response.ok and response.release_outcome == "released"
    assert len(native_stage.removed) == 1
    assert control.read_bytes() == before


@pytest.mark.parametrize("defect", [
    "missing", "json", "duplicate", "extra", "oversize", "symlink", "hardlink", "mode",
    "owner", "directory-symlink", "own-scope", "own-state", "missing-own", "state-type",
])
def test_native_unreadable_or_ambiguous_inventory_never_reaches_cache(
    native_stage: Any, defect: str,
) -> None:
    control = retained(native_stage, marker(run_id="00000000-0000-0000-0000-000000000099"))
    own = native_stage.stage / "execution.json"
    if defect == "missing":
        control.unlink()
    elif defect == "json":
        control.write_bytes(b"{")
    elif defect == "duplicate":
        control.write_bytes(b'{"state":"UNKNOWN",' + control.read_bytes()[1:])
    elif defect == "extra":
        control.write_text(json.dumps(marker(extra=True)), encoding="ascii")
    elif defect == "oversize":
        control.write_bytes(b" " * (backend._MAX_CONTROL + 1))
    elif defect == "symlink":
        control.unlink()
        control.symlink_to(own)
    elif defect == "hardlink":
        os.link(control, control.parent / "alias")
    elif defect == "mode":
        control.chmod(0o666)
    elif defect == "owner":
        if sys.platform == "linux":
            os.chown(control, 61111, 61111)
    elif defect == "directory-symlink":
        path = control.parent
        moved = native_stage.root.parent / "moved"
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
    elif defect == "own-scope":
        own.write_text(json.dumps(marker(release().invocation.execution_id, state="RUNNING",
                       tenant_id="00000000-0000-0000-0000-000000000099")), encoding="ascii")
    elif defect == "own-state":
        payload = json.loads(own.read_bytes())
        payload["state"] = "UNKNOWN"
        own.write_text(json.dumps(payload), encoding="ascii")
    elif defect == "missing-own":
        own.unlink()
    elif defect == "state-type":
        control.write_text(json.dumps(marker(state=[])), encoding="ascii")
    with pytest.raises(ProcessCleanupIncomplete):
        invoke(native_stage)
    assert native_stage.removed == []
