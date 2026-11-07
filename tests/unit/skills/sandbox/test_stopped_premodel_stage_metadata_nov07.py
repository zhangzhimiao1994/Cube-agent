"""Original retained markers stay unknown; parsing is never execution authority."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import PurePosixPath
from types import SimpleNamespace
from uuid import UUID

import pytest

from agent_hub.skills.sandbox.python_premodel_retirement_backend import parse_stopped_stage
from agent_hub.skills.sandbox.python_project_protocol import PythonFailedMaintenanceInvocation


def owner() -> PythonFailedMaintenanceInvocation:
    return PythonFailedMaintenanceInvocation(
        execution_id="python_release_expired_" + "a" * 32,
        tenant_id=UUID(int=1), run_id=UUID(int=2), project_id="billing", session_id="main",
    )


def marker(**changes: object) -> bytes:
    fields = {"execution_id": "python_old", "tenant_id": str(UUID(int=1)), "run_id": str(UUID(int=2)),
              "project_id": "billing", "session_id": "main", "source_sha256": "b" * 64,
              "state": "UNKNOWN", "unit": "agent-hub-project-python_old.service"}
    fields.update(changes)
    return json.dumps(fields, indent=2).encode()


@pytest.mark.parametrize("state", ["UNKNOWN", "RUNNING", "STAGED"])
def test_retained_original_unknown_bytes_are_parsed_without_claiming_success(state: str) -> None:
    raw = marker(state=state)
    parsed = parse_stopped_stage(raw, "python_old-abcd1234", owner())
    assert parsed is not None and parsed.execution_id == "python_old"
    assert parsed.run_id == owner().run_id
    assert json.loads(raw)["state"] == state


@pytest.mark.parametrize("field,value", [
    ("run_id", str(UUID(int=9))), ("tenant_id", str(UUID(int=9))),
    ("project_id", "other"), ("session_id", "other"),
])
def test_valid_foreign_marker_is_not_this_owner(field: str, value: str) -> None:
    assert parse_stopped_stage(marker(**{field: value}), "python_old-abcd1234", owner()) is None


@pytest.mark.parametrize("changes", [
    {"state": "COMPLETED"}, {"state": True}, {"unit": "another.service"},
    {"execution_id": "other"}, {"source_sha256": "untrusted"}, {"extra": "field"},
])
def test_forged_marker_is_not_liveness_evidence(changes: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        parse_stopped_stage(marker(**changes), "python_old-abcd1234", owner())


@pytest.mark.parametrize("name", ["../python_old-abcd1234", "python_old-abcd/1234",
                                  "python_old-", "/python_old-abcd1234", "other-abcd1234"])
def test_marker_cannot_supply_an_arbitrary_directory(name: str) -> None:
    with pytest.raises((ValueError, TypeError)):
        parse_stopped_stage(marker(), name, owner())


def test_duplicate_control_key_and_oversized_control_rejected() -> None:
    raw = marker().replace(b'"state": "UNKNOWN"', b'"state": "UNKNOWN", "state": "RUNNING"')
    with pytest.raises(ValueError):
        parse_stopped_stage(raw, "python_old-abcd1234", owner())
    with pytest.raises(ValueError):
        parse_stopped_stage(b" " * 65537, "python_old-abcd1234", owner())


def test_both_original_units_require_no_job_and_no_live_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_hub.skills.sandbox import python_premodel_retirement_backend as backend
    from agent_hub.skills.sandbox.base import SkillResult
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.project import ProjectExecutionBroker

    invocation = parse_stopped_stage(marker(), "python_old-abcd1234", owner())
    assert invocation is not None
    calls = []
    properties = {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead",
                  "MainPID": "0", "ControlPID": "0", "ControlGroup": "", "TasksCurrent": "0", "Job": ""}

    async def helper(command: tuple[str, ...], timeout: float) -> SkillResult:
        calls.append(command)
        return SkillResult(exit_code=0, stdout="\n".join(f"{k}={v}" for k, v in properties.items()),
                           stderr="", timed_out=False)

    broker = SimpleNamespace(policy=BrokerPolicy(), _helper=helper,
                             _unit_quiet=ProjectExecutionBroker._unit_quiet)

    def run() -> None:
        coroutine = backend.check_stopped_units(broker, (invocation,), time.monotonic() + 10)
        try:
            with pytest.raises(StopIteration):
                coroutine.send(None)
        finally:
            coroutine.close()

    run()
    assert [c[2] for c in calls] == ["agent-hub-python-broker-python_old.service",
                                   "agent-hub-project-python_old.service"]
    assert all("Job" in c[-1] and c[1] == "show" for c in calls)
    for field, value in (("Job", "34"), ("MainPID", "42"), ("TasksCurrent", "1"),
                         ("ActiveState", "active"), ("SubState", "running")):
        original = properties[field]
        properties[field] = value
        with pytest.raises(ValueError):
            run()
        properties[field] = original


@pytest.mark.parametrize("path,expected", [
    ("/var/lib/cache/abc/environment/bin/python", True),
    ("/var/lib/cache/abc", True),
    ("/var/lib/cache/abc/environment/file (deleted)", True),
    ("/var/lib/cache/abcdef/environment/file", False),
    ("/var/lib/cache/other/environment/file", False),
    ("/var/lib/cache/ab\\143/environment/file", True),
    ("anon_inode:[eventpoll]", False),
])
def test_consumer_paths_use_full_directory_boundaries_and_kernel_escapes(
    path: str, expected: bool,
) -> None:
    from agent_hub.skills.sandbox import python_premodel_retirement_backend as backend

    assert backend.consumer_path_matches(path, ("/var/lib/cache/abc",)) is expected


@pytest.mark.parametrize("gone", [False, True])
def test_missing_process_metadata_does_not_prove_a_live_pid_exited(
    monkeypatch: pytest.MonkeyPatch, gone: bool,
) -> None:
    from agent_hub.skills.sandbox import python_premodel_retirement_backend as backend

    monkeypatch.setattr(backend, "_PROC_ROOT", PurePosixPath("/proc"))

    @contextmanager
    def scandir(path: object):
        yield iter([SimpleNamespace(name="22")] if str(path) == "/proc" else [])

    stat_reads = 0

    def read(path: PurePosixPath, limit: int, deadline: float) -> bytes:
        nonlocal stat_reads
        if path.parent.name == str(backend.os.getpid()) and path.name == "mountinfo":
            return b""
        if path.name == "maps":
            raise FileNotFoundError
        assert path.name == "stat"
        stat_reads += 1
        if gone and stat_reads > 1:
            raise FileNotFoundError
        return b"22 (still-live) " + b" ".join([b"R"] + [b"0"] * 19)

    monkeypatch.setattr(backend.os, "scandir", scandir)
    monkeypatch.setattr(backend.os, "readlink", lambda _: "/unrelated")
    monkeypatch.setattr(backend, "_process_bytes", read)
    if gone:
        backend._scan_process_consumers(("/var/lib/cache/abc",), time.monotonic() + 10)
    else:
        with pytest.raises(ValueError, match="process metadata disappeared"):
            backend._scan_process_consumers(("/var/lib/cache/abc",), time.monotonic() + 10)


@pytest.mark.parametrize("mount", [
    b"31 20 8:1 /var/lib/cache/abc/environment /private/env ro - ext4 /dev/root ro\n",
    b"31 20 0:2 / /private/env rw - overlay overlay rw,lowerdir=/var/lib/cache/abc/environment\n",
])
def test_inspector_own_mount_namespace_is_not_covered_by_own_fd_exemption(
    monkeypatch: pytest.MonkeyPatch, mount: bytes,
) -> None:
    from agent_hub.skills.sandbox import python_premodel_retirement_backend as backend

    calls = []

    def read(path: object, limit: int, deadline: float) -> bytes:
        calls.append(str(path))
        return mount

    def forbidden(*args: object) -> None:
        pytest.fail("own mount must veto before another PID or FD is examined")

    monkeypatch.setattr(backend, "_process_bytes", read)
    monkeypatch.setattr(backend.os, "scandir", forbidden)
    with pytest.raises(ValueError, match="mounted consumer"):
        backend._scan_process_consumers(("/var/lib/cache/abc",), time.monotonic() + 5)
    assert calls == [str(backend._PROC_ROOT / str(backend.os.getpid()) / "mountinfo")]
