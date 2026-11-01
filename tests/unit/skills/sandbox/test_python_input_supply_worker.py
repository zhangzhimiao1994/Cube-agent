import base64
import json
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import NoReturn, Unpack
from uuid import UUID

import pytest

from agent_hub.skills.sandbox import python_input_supply_worker as api
from agent_hub.skills.sandbox.base import ProcessLimits, SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import OriginalProjectReference, ProjectInvocation
from agent_hub.skills.sandbox.project_contract_broker import _Observed
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_input_supply_catalog import InputSupplyError, RootSupplyConfig
from agent_hub.skills.sandbox.python_input_supply_protocol import PythonInputSupplyRequest
from agent_hub.skills.sandbox.python_project_backend import RootConfig
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectProbeInvocation
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.skills.sandbox.python_input_supply_worker_typed_fixture import (
    RecordedCalls,
    RunOptions,
    SubprocessFixture,
    configuration,
    entries,
    invocation,
    json_object,
    plan,
    supply_configuration,
)


def request() -> PythonInputSupplyRequest:
    return PythonInputSupplyRequest(action="plan_python_project_inputs",
        invocation=PythonProjectProbeInvocation(execution_id="fetch_batch_1",
            tenant_id=UUID(int=1), run_id=UUID(int=2), project_id="project", session_id="session",
            sandbox_profile="workspace_write", timeout_seconds=30,
            original_reference=OriginalProjectReference(schema_version=1, contract_sha256="a" * 64,
                                input_source_sha256="b" * 64, file_count=6)))


def supply() -> RootSupplyConfig:
    return supply_configuration()


@pytest.fixture
def subprocess(monkeypatch: pytest.MonkeyPatch) -> SubprocessFixture:
    calls: RecordedCalls = []

    async def run(command: tuple[str, ...], limits: ProcessLimits, **kwargs: Unpack[RunOptions]) -> SkillResult:
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
    return await api._fetch_inputs(items, request(), BrokerPolicy(), configuration(), supply(), time.monotonic() + 25)


@pytest.mark.asyncio
async def test_two_artifacts_use_one_unit_one_batch_and_confirm_quiescence(subprocess: SubprocessFixture) -> None:
    calls, _ = subprocess
    assert await fetch(entries()) == {item.filename: b"abc" for item in entries()}
    launched = [row for row in calls if row[0][0].endswith("systemd-run")]
    assert len(launched) == 1 and launched[0][0][-1] == "--worker-batch"
    raw = launched[0][2]["stdin_payload"]
    assert isinstance(raw, bytes)
    payload = json_object(raw)
    artifacts, timeout = payload["artifacts"], payload["timeout_seconds"]
    assert isinstance(artifacts, list) and isinstance(timeout, (int, float))
    assert len(artifacts) == 2 and 0 < timeout <= 20
    assert calls[-1][0][1] == "show" and calls[-2][0][1] == "kill"


@pytest.mark.asyncio
async def test_empty_missing_set_never_launches_network(subprocess: SubprocessFixture) -> None:
    assert await fetch(()) == {}
    assert subprocess[0] == []


@pytest.mark.asyncio
async def test_batch_quota_is_checked_before_unit_side_effects(subprocess: SubprocessFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_builder(invocation: ProjectInvocation, policy: BrokerPolicy, *, configuration: RootConfig, supply: RootSupplyConfig) -> NoReturn:
        pytest.fail("quota reached builder")
    def small_supply() -> RootSupplyConfig:
        return supply_configuration(max_total_bytes=5)
    monkeypatch.setattr(api, "build_fetch_command", forbidden_builder)
    monkeypatch.setattr(sys.modules[__name__], "supply", small_supply)
    with pytest.raises(InputSupplyError, match="input_supply_quota"):
        await fetch(entries())
    assert subprocess[0] == []


@pytest.mark.asyncio
async def test_failure_always_kills_and_unconfirmed_drain_is_uncertain(subprocess: SubprocessFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    calls, original = subprocess

    async def run(command: tuple[str, ...], limits: ProcessLimits, **kwargs: Unpack[RunOptions]) -> SkillResult:
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

    async def delayed(command: tuple[str, ...], limits: ProcessLimits, **kwargs: Unpack[RunOptions]) -> SkillResult:
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
    def forbidden_open(path: str | bytes | Path | int, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> NoReturn:
        pytest.fail("unsupported Root journal write")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "open", forbidden_open)
    with pytest.raises(ValueError, match="input_supply_unsupported"):
        api._once(123, "intent.json", {"schema_version": 1})


@pytest.mark.parametrize("change", [{"schema_version": True}, {"extra": "unexpected"}])
def test_status_refuses_ambiguous_root_intent(monkeypatch: pytest.MonkeyPatch, change: dict[str, object]) -> None:
    intent = {"schema_version": 1, "plan": plan().model_dump(mode="json"),
              "supply_execution_id": "old_supply_1", **change}
    item = PythonInputSupplyRequest(action="inspect_python_project_input_supply", invocation=invocation(),
        expected_plan_sha256=plan().sha256, supply_execution_id="old_supply_1")
    def root(path: Path, owner: int, stack: ExitStack, pins: list[_Observed], *, stage: bool = False) -> int:
        return 0
    def open_directory(parent: int, name: str, owner: int, stack: ExitStack, pins: list[_Observed], *, mode: int | None = None, full: bool = False) -> int:
        return 1
    monkeypatch.setattr(api, "_root", root)
    monkeypatch.setattr(api, "_open_directory", open_directory)

    def read(parent: int, name: str, owner: int, pins: list[_Observed]) -> bytes:
        if name == "intent.json":
            return json.dumps(intent).encode()
        raise FileNotFoundError

    monkeypatch.setattr(api, "_read_control", read)
    with pytest.raises(InputSupplyError, match="input_supply_scope_changed"):
        api._status(item, supply())
