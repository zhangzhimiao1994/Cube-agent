import base64
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest

from agent_hub.skills.sandbox import python_input_supply_worker as api
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import _Limits
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_input_supply_catalog import InputSupplyError, RootSupplyConfig
from agent_hub.skills.sandbox.python_input_supply_protocol import PythonInputSupplyRequest
from agent_hub.skills.sandbox.python_project_backend import RootConfig
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectProbeInvocation
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

CommandCall = tuple[Sequence[str], _Limits, dict[str, object]]
Runner = Callable[..., Awaitable[SkillResult]]
SubprocessFixture = tuple[list[CommandCall], Runner]


def request() -> PythonInputSupplyRequest:
    return PythonInputSupplyRequest(action="plan_python_project_inputs",
        invocation=PythonProjectProbeInvocation.model_validate({"execution_id": "fetch_batch_1",
            "tenant_id": UUID(int=1), "run_id": UUID(int=2), "project_id": "project", "session_id": "session",
            "sandbox_profile": "workspace_write", "timeout_seconds": 30,
            "original_reference": {"schema_version": 1, "contract_sha256": "a" * 64,
                                "input_source_sha256": "b" * 64, "file_count": 6}}))


def entries() -> tuple[WheelDownload, ...]:
    return tuple(WheelDownload(filename=name + "-1.0-py3-none-any.whl", name=name, version="1.0",
        sha256="a" * 64, size_bytes=3,
        url="https://files.pythonhosted.org/packages/aa/bb/" + "c" * 60 + "/" + name + "-1.0-py3-none-any.whl")
        for name in ("alpha", "beta"))


def supply() -> RootSupplyConfig:
    return cast(RootSupplyConfig, SimpleNamespace(fetch_timeout_seconds=20, max_total_bytes=1024,
                           state_root=Path("/var/lib/agent-hub-python-input-supply")))


@pytest.fixture
def subprocess(monkeypatch: pytest.MonkeyPatch) -> SubprocessFixture:
    calls: list[CommandCall] = []

    async def run(command: Sequence[str], limits: _Limits, **kwargs: object) -> SkillResult:
        calls.append((command, limits, kwargs))
        if command[0].endswith("systemd-run"):
            data = {"schema_version": 1, "artifacts": [{"filename": item.filename,
                "body_base64": base64.b64encode(b"abc").decode()} for item in entries()]}
            return SkillResult(exit_code=0, stdout=json.dumps(data), stderr="", timed_out=False)
        return SkillResult(exit_code=0,
            stdout="LoadState=not-found\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\nControlGroup=\nTasksCurrent=0\n",
            stderr="", timed_out=False)

    monkeypatch.setattr(api, "run_subprocess_with_limits", run)
    return calls, run


async def fetch(items: tuple[WheelDownload, ...]) -> dict[str, bytes]:
    configuration = SimpleNamespace(environment_root=Path("/var/lib/env"), wheel_root=Path("/var/lib/wheels"))
    return await api._fetch_inputs(items, request(), BrokerPolicy(), cast(RootConfig, configuration), supply(), time.monotonic() + 25)


@pytest.mark.asyncio
async def test_two_artifacts_use_one_unit_one_batch_and_confirm_quiescence(subprocess: SubprocessFixture) -> None:
    calls, _ = subprocess
    assert await fetch(entries()) == {item.filename: b"abc" for item in entries()}
    launched = [row for row in calls if row[0][0].endswith("systemd-run")]
    assert len(launched) == 1 and launched[0][0][-1] == "--worker-batch"
    raw = launched[0][2]["stdin_payload"]
    assert isinstance(raw, bytes)
    payload = json.loads(raw)
    assert len(payload["artifacts"]) == 2 and 0 < payload["timeout_seconds"] <= 20
    assert calls[-1][0][1] == "show" and calls[-2][0][1] == "kill"


@pytest.mark.asyncio
async def test_empty_missing_set_never_launches_network(subprocess: SubprocessFixture) -> None:
    assert await fetch(()) == {}
    assert subprocess[0] == []


@pytest.mark.asyncio
async def test_batch_quota_is_checked_before_unit_side_effects(subprocess: SubprocessFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "build_fetch_command", lambda *a, **k: pytest.fail("quota reached builder"))
    monkeypatch.setattr(__import__(__name__, fromlist=["supply"]), "supply",
        lambda: SimpleNamespace(fetch_timeout_seconds=20, max_total_bytes=5))
    with pytest.raises(InputSupplyError, match="input_supply_quota"):
        await fetch(entries())
    assert subprocess[0] == []


@pytest.mark.asyncio
async def test_failure_always_kills_and_unconfirmed_drain_is_uncertain(subprocess: SubprocessFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, original = subprocess

    async def run(command: Sequence[str], limits: _Limits, **kwargs: object) -> SkillResult:
        if command[0].endswith("systemd-run"):
            calls.append((command, limits, kwargs))
            return SkillResult(exit_code=1, stdout="", stderr="private", timed_out=False)
        if len(calls) > 1 and command[1] == "show":
            return SkillResult(exit_code=0, stdout="ActiveState=active\nMainPID=99\n", stderr="", timed_out=False)
        return await original(command, limits, **kwargs)

    monkeypatch.setattr(api, "run_subprocess_with_limits", run)
    with pytest.raises(ProcessCleanupIncomplete):
        await fetch(entries())
    assert any(command[1] == "kill" for command, _, _ in calls)


@pytest.mark.asyncio
async def test_manager_kill_can_converge_before_being_marked_unknown(subprocess: SubprocessFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    _, original = subprocess
    killed = False
    queries = 0

    async def delayed(command: Sequence[str], limits: _Limits, **kwargs: object) -> SkillResult:
        nonlocal killed, queries
        if command[1] == "kill":
            killed = True
        if killed and command[1] == "show":
            queries += 1
            if queries == 1:
                return SkillResult(exit_code=0, stdout="ActiveState=deactivating\nMainPID=99\n",
                                   stderr="", timed_out=False)
        return await original(command, limits, **kwargs)

    monkeypatch.setattr(api, "run_subprocess_with_limits", delayed)
    assert len(await fetch(entries())) == 2 and queries == 2


def test_root_journal_writer_rejects_unsupported_platform_before_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "open", lambda *a, **k: pytest.fail("unsupported Root journal write"))
    with pytest.raises(ValueError, match="input_supply_unsupported"):
        api._once(123, "intent.json", {"schema_version": 1})


@pytest.mark.parametrize("change", [{"schema_version": True}, {"extra": "unexpected"}])
def test_status_refuses_ambiguous_root_intent(monkeypatch: pytest.MonkeyPatch, change: dict[str, object]) -> None:
    from tests.unit.skills.sandbox.test_python_input_supply_protocol import invocation, plan

    intent = {"schema_version": 1, "plan": plan().model_dump(mode="json"),
              "supply_execution_id": "old_supply_1", **change}
    item = PythonInputSupplyRequest(action="inspect_python_project_input_supply", invocation=invocation(),
        expected_plan_sha256=plan().sha256, supply_execution_id="old_supply_1")
    monkeypatch.setattr(api, "_root", lambda *a, **k: 0)
    monkeypatch.setattr(api, "_open_directory", lambda *a, **k: 1)

    def read(parent: int, name: str, owner: int, pins: object) -> bytes:
        if name == "intent.json":
            return json.dumps(intent).encode()
        raise FileNotFoundError

    monkeypatch.setattr(api, "_read_control", read)
    with pytest.raises(InputSupplyError, match="input_supply_scope_changed"):
        api._status(item, supply())
