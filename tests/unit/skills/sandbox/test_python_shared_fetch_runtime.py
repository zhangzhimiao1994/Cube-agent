"""Shared Root fetch boundary; subprocesses are simulated, not native isolation proof."""

import asyncio
import base64
import json
import math
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
from agent_hub.skills.sandbox.project import (
    ProjectExecutionBroker,
    ProjectInvocation,
    _Execution,
    _Limits,
)
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_input_supply_catalog import InputSupplyError, RootSupplyConfig
from agent_hub.skills.sandbox.python_input_supply_protocol import PythonInputSupplyRequest
from agent_hub.skills.sandbox.python_project_backend import RootConfig
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectProbeInvocation
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

Call = tuple[Sequence[str], _Limits, dict[str, object]]
Runner = Callable[..., Awaitable[SkillResult]]
SubprocessFixture = tuple[list[Call], Runner]


def invocation() -> ProjectInvocation:
    return ProjectInvocation(execution_id="shared_fetch_1", tenant_id=UUID(int=1),
        run_id=UUID(int=2), project_id="project", session_id="session",
        sandbox_profile="restricted", timeout_seconds=30, output_limit_bytes=1)


def entries() -> tuple[WheelDownload, ...]:
    return tuple(WheelDownload(filename=name + "-1.0-py3-none-any.whl", name=name, version="1.0",
        sha256="a" * 64, size_bytes=3,
        url="https://files.pythonhosted.org/packages/aa/" + name + "-1.0-py3-none-any.whl")
        for name in ("alpha", "beta"))


def settings() -> tuple[BrokerPolicy, RootConfig, RootSupplyConfig]:
    return (BrokerPolicy(),
        cast(RootConfig, SimpleNamespace(environment_root=Path("/var/lib/env"),
                                        wheel_root=Path("/var/lib/wheels"))),
        cast(RootSupplyConfig, SimpleNamespace(fetch_timeout_seconds=20, max_total_bytes=1024,
            state_root=Path("/var/lib/agent-hub-python-input-supply"))))


def response() -> dict[str, object]:
    return {"schema_version": 1, "artifacts": [{"filename": item.filename,
        "body_base64": base64.b64encode(b"abc").decode()} for item in entries()]}


@pytest.fixture
def subprocess(monkeypatch: pytest.MonkeyPatch) -> SubprocessFixture:
    calls: list[Call] = []

    async def run(command: Sequence[str], limits: _Limits, **kwargs: object) -> SkillResult:
        calls.append((command, limits, kwargs))
        stdout = (json.dumps(response()) if command[0].endswith("systemd-run") else
            "LoadState=not-found\nActiveState=inactive\nSubState=dead\nMainPID=0\n"
            "ControlPID=0\nControlGroup=\nTasksCurrent=0\n")
        return SkillResult(exit_code=0, stdout=stdout, stderr="", timed_out=False)

    monkeypatch.setattr(api, "run_subprocess_with_limits", run)
    return calls, run


async def fetch(
    item: ProjectInvocation, *, items: tuple[WheelDownload, ...] | None = None,
) -> dict[str, bytes]:
    return await api.fetch_catalog_wheels(entries() if items is None else items, item,
        *settings(), time.monotonic() + 25)


@pytest.mark.parametrize("profile", ["restricted", "workspace_write"])
async def test_base_invocation_fetches_one_batch_and_drains_verified_unit(
    subprocess: SubprocessFixture, monkeypatch: pytest.MonkeyPatch, profile: str,
) -> None:
    item = invocation().model_copy(update={"sandbox_profile": profile})
    original = ProjectExecutionBroker._unit_quiet

    def quiet(job: _Execution, result: SkillResult) -> bool:
        checked = job.invocation
        assert type(checked) is ProjectInvocation and checked is not item
        assert vars(checked) == vars(item)
        return original(job, result)

    monkeypatch.setattr(ProjectExecutionBroker, "_unit_quiet", quiet)
    assert await fetch(item) == {
        "alpha-1.0-py3-none-any.whl": b"abc", "beta-1.0-py3-none-any.whl": b"abc"}
    calls, _ = subprocess
    assert [call[0][1] for call in calls] == ["show", "--wait", "kill", "show"]
    assert calls[0][0][2] == calls[-1][0][2] == "agent-hub-project-shared_fetch_1.service"
    command, limits, kwargs = calls[1]
    assert command[-1] == "--worker-batch"
    assert "agent-hub-project-shared_fetch_1" in command
    payload = json.loads(cast(bytes, kwargs["stdin_payload"]))
    assert len(payload["artifacts"]) == 2 and 0 < payload["timeout_seconds"] <= 20
    assert limits.timeout_seconds == payload["timeout_seconds"] + 2
    assert limits.output_limit_bytes == sum(
        4 * math.ceil(e.size_bytes / 3) + 6 * len(e.filename) + 256 for e in entries()) + 8192
    assert callable(kwargs["on_forced_terminate"])
    assert "DynamicUser=yes" in command and "ProtectHome=yes" in command
    assert "CapabilityBoundingSet=" in command and "LimitFSIZE=0" in command
    assert "BindReadOnlyPaths=/etc/resolv.conf" in command
    assert "PrivateTmp=no" in command and "/usr/bin/env" in command and "-i" in command
    assert not any(arg.startswith(("ReadWritePaths=", "BindPaths=")) for arg in command)


async def test_empty_catalog_never_touches_systemd(subprocess: SubprocessFixture) -> None:
    assert await fetch(invocation(), items=()) == {}
    assert subprocess[0] == []


@pytest.mark.parametrize("update", [
    {"install_allowed": True}, {"install_allowed": 0}, {"timeout_seconds": "30"},
    {"timeout_seconds": True}, {"timeout_seconds": 0}, {"execution_id": "bad;unit"},
    {"tenant_id": str(UUID(int=1))}, {"sandbox_profile": "unrestricted"},
    {"memory_limit_bytes": 0}, {"output_limit_bytes": 0}, {"cpu_quota_percent": 401},
    {"original_reference": "hidden copied field"}, {"credential": "hidden copied field"},
])
@pytest.mark.parametrize("empty", [False, True])
async def test_copied_invocation_is_rechecked_before_any_unit_or_empty_shortcut(
    subprocess: SubprocessFixture, update: dict[str, object], empty: bool,
) -> None:
    with pytest.raises(ValueError):
        await fetch(invocation().model_copy(update=update), items=() if empty else entries())
    assert subprocess[0] == []


@pytest.mark.parametrize("kind", ["probe", "subclass", "mapping"])
async def test_shared_boundary_requires_exact_base_invocation(
    subprocess: SubprocessFixture, kind: str,
) -> None:
    class DerivedInvocation(ProjectInvocation):
        pass

    values = vars(invocation())
    item: object
    if kind == "probe":
        item = PythonProjectProbeInvocation.model_validate({**values, "original_reference": {
            "schema_version": 1, "contract_sha256": "a" * 64,
            "input_source_sha256": "b" * 64, "file_count": 6}}, strict=True)
    elif kind == "subclass":
        item = DerivedInvocation.model_validate(values, strict=True)
    else:
        item = values
    with pytest.raises(ValueError):
        await fetch(cast(ProjectInvocation, item))
    assert subprocess[0] == []


@pytest.mark.parametrize("budget", ["deadline", "total_bytes", "payload"])
async def test_budget_failures_happen_before_systemd(
    subprocess: SubprocessFixture, budget: str,
) -> None:
    policy, configuration, supply = settings()
    deadline = time.monotonic() - 1 if budget == "deadline" else time.monotonic() + 25
    items = entries()
    if budget == "total_bytes":
        supply = cast(RootSupplyConfig, SimpleNamespace(fetch_timeout_seconds=20, max_total_bytes=5))
    elif budget == "payload":
        items *= 2000
        supply = cast(RootSupplyConfig, SimpleNamespace(fetch_timeout_seconds=20,
                                                       max_total_bytes=1_000_000))
    code = "input_supply_timeout" if budget == "deadline" else "input_supply_quota"
    with pytest.raises(InputSupplyError, match=code):
        await api.fetch_catalog_wheels(items, invocation(), policy, configuration, supply, deadline)
    assert subprocess[0] == []


@pytest.mark.parametrize("mutation", [
    "schema_bool", "extra", "count", "order", "row_extra", "base64", "size", "duplicate_key",
])
async def test_malformed_complete_body_is_rejected_after_drain(
    subprocess: SubprocessFixture, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    calls, original = subprocess
    payload = response()
    rows = cast(list[dict[str, object]], payload["artifacts"])
    if mutation == "schema_bool":
        payload["schema_version"] = True
    elif mutation == "extra":
        payload["extra"] = True
    elif mutation == "count":
        rows.pop()
    elif mutation == "order":
        rows.reverse()
    elif mutation == "row_extra":
        rows[0]["extra"] = True
    elif mutation == "base64":
        rows[0]["body_base64"] = "!!!!"
    elif mutation == "size":
        rows[0]["body_base64"] = "YWI="
    stdout = json.dumps(payload)
    if mutation == "duplicate_key":
        stdout = stdout.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')

    async def run(command: Sequence[str], limits: _Limits, **kwargs: object) -> SkillResult:
        result = await original(command, limits, **kwargs)
        return result.model_copy(update={"stdout": stdout}) if command[0].endswith("systemd-run") else result

    monkeypatch.setattr(api, "run_subprocess_with_limits", run)
    with pytest.raises(InputSupplyError, match="input_supply_artifact_integrity"):
        await fetch(invocation())
    assert [call[0][1] for call in calls][-2:] == ["kill", "show"]


@pytest.mark.parametrize("failure", ["exit", "timeout", "cancel"])
async def test_failed_fetch_always_drains_before_returning(
    subprocess: SubprocessFixture, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    calls, original = subprocess

    async def run(command: Sequence[str], limits: _Limits, **kwargs: object) -> SkillResult:
        result = await original(command, limits, **kwargs)
        if command[0].endswith("systemd-run"):
            if failure == "cancel":
                raise asyncio.CancelledError
            if failure == "timeout":
                await cast(Callable[[], Awaitable[None]], kwargs["on_forced_terminate"])()
            return result.model_copy(update={"exit_code": 1, "timed_out": failure == "timeout"})
        return result

    monkeypatch.setattr(api, "run_subprocess_with_limits", run)
    expected = asyncio.CancelledError if failure == "cancel" else InputSupplyError
    with pytest.raises(expected):
        await fetch(invocation())
    assert [call[0][1] for call in calls][-2:] == ["kill", "show"]


async def test_nonquiet_existing_unit_is_never_started_or_killed(
    subprocess: SubprocessFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, original = subprocess

    async def run(command: Sequence[str], limits: _Limits, **kwargs: object) -> SkillResult:
        result = await original(command, limits, **kwargs)
        return result.model_copy(update={"stdout": "ActiveState=active\nMainPID=99\n"})

    monkeypatch.setattr(api, "run_subprocess_with_limits", run)
    with pytest.raises(ProcessCleanupIncomplete):
        await fetch(invocation())
    assert [call[0][1] for call in calls] == ["show"]


async def test_legacy_wrapper_converts_probe_before_shared_boundary(
    subprocess: SubprocessFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = PythonProjectProbeInvocation.model_validate({**vars(invocation()),
        "sandbox_profile": "workspace_write", "original_reference": {"schema_version": 1,
            "contract_sha256": "a" * 64, "input_source_sha256": "b" * 64, "file_count": 6}},
        strict=True)
    request = PythonInputSupplyRequest(action="plan_python_project_inputs", invocation=probe)
    original = api.fetch_catalog_wheels
    observed: list[ProjectInvocation] = []

    async def shared(
        items: tuple[WheelDownload, ...], item: ProjectInvocation, policy: BrokerPolicy,
        configuration: RootConfig, supply: RootSupplyConfig, deadline: float,
    ) -> dict[str, bytes]:
        assert type(item) is ProjectInvocation and "original_reference" not in vars(item)
        observed.append(item)
        return await original(items, item, policy, configuration, supply, deadline)

    monkeypatch.setattr(api, "fetch_catalog_wheels", shared)
    result = await api._fetch_inputs(entries(), request, *settings(), time.monotonic() + 25)
    assert result == {"alpha-1.0-py3-none-any.whl": b"abc", "beta-1.0-py3-none-any.whl": b"abc"}
    assert len(observed) == 1 and len(subprocess[0]) == 4
