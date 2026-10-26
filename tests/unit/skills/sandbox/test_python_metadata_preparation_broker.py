"""Portable v10 orchestration tests; no Linux/systemd isolation claim."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest

from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import ProjectExecutionBroker, _Execution
from agent_hub.skills.sandbox.python_metadata_plan import build_python_metadata_plan
from agent_hub.skills.sandbox.python_metadata_preparation_protocol import (
    PythonMetadataPreparationReceipt,
    PythonMetadataPreparationRequest,
    PythonMetadataPreparationResponse,
    PythonMetadataPreparationStatus,
)
from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileScope
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.skills.test_python_metadata_plan import inputs

MODULE = "agent_hub.skills.sandbox.python_metadata_preparation_broker"
PREPARE: Final = "prepare_python_dependency_metadata"
INSPECT: Final = "inspect_python_dependency_metadata_preparation"
KEY = "metadata_prepare_" + "a" * 32
INSPECT_KEY = "metadata_inspect_" + "b" * 32
HELPER = "agent-hub-python-broker-" + KEY + ".service"
CHILD = "agent-hub-project-" + KEY + ".service"


def test_preparation_broker_exists() -> None:
    assert importlib.util.find_spec(MODULE) is not None, "v10 preparation broker missing"


def request(inspect: bool = False) -> PythonMetadataPreparationRequest:
    planning, root, supply, _ = inputs()
    planning = planning.model_copy(update={"invocation": planning.invocation.model_copy(
        update={"execution_id": INSPECT_KEY if inspect else KEY})})
    plan = build_python_metadata_plan(planning, root=root, supply=supply, cached=())
    return PythonMetadataPreparationRequest(action=INSPECT if inspect else PREPARE,
        planning=planning, expected_plan_sha256=plan.sha256,
        preparation_execution_id=KEY if inspect else None)


def success(item: PythonMetadataPreparationRequest) -> PythonMetadataPreparationResponse:
    _, root, supply, _ = inputs()
    plan = build_python_metadata_plan(item.planning, root=root, supply=supply, cached=())
    receipt = PythonMetadataPreparationReceipt(plan=plan, preparation_execution_id=KEY,
        index_sha256="1" * 64, index_record_count=len(supply.catalog))
    return PythonMetadataPreparationResponse(action=item.action,
        scope=PythonProjectProfileScope.for_invocation(item.invocation), ok=True,
        receipt=receipt if item.action == PREPARE else None,
        status=PythonMetadataPreparationStatus(state="prepared", plan_sha256=plan.sha256,
            preparation_execution_id=KEY, receipt=receipt) if item.action == INSPECT else None)


@pytest.fixture
def api() -> Any:
    assert importlib.util.find_spec(MODULE) is not None, "v10 preparation broker missing"
    return importlib.import_module(MODULE)


@pytest.fixture
def configured(api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    stage = tmp_path / "stages"
    stage.mkdir(mode=0o700)
    policy = replace(BrokerPolicy(), runtime_root=tmp_path / "legacy",
        python_project_runtime_root=stage)
    broker = ProjectExecutionBroker(policy, retained_runtime_roots=(stage,))
    root = SimpleNamespace(stage_root=stage)
    supply = SimpleNamespace(state_root=tmp_path / "supply")
    item = SimpleNamespace(broker=broker, root=stage, events=[], admissions=[],
        stdout=None, exit_code=0, timed_out=False, root_config=root, supply=supply)

    def read_root() -> Any:
        item.events.append("root-config")
        return root

    def read_supply() -> Any:
        item.events.append("supply-config")
        return supply

    def validate(*args: Any) -> None:
        item.events.append("validate")

    def write(stage: Path, value: PythonMetadataPreparationRequest) -> None:
        assert json.loads((stage / "execution.json").read_bytes())["state"] == "STAGED"
        (stage / "request.json").write_bytes(value.model_dump_json().encode())
        item.events.append("write")

    async def admit(actual: Any, job: Any) -> None:
        assert actual is broker and broker.executions[job.invocation.execution_id].slot_acquired
        item.admissions.append(job)
        item.events.append("admit:" + job.unit)

    async def stop(job: Any) -> None:
        item.events.append("stop:" + job.unit)

    original_cleanup = broker._cleanup

    async def cleanup(job: Any) -> None:
        item.events.append("cleanup")
        await original_cleanup(job)

    async def subprocess(command: Any, limits: Any, **kwargs: Any) -> SkillResult:
        item.events.append("spawn")
        assert kwargs["stdin_payload"] == b"" and limits.output_limit_bytes == 2_000_000
        assert 0 < limits.timeout_seconds <= request().invocation.timeout_seconds
        kwargs["process_started"](SimpleNamespace(returncode=item.exit_code))
        value = PythonMetadataPreparationRequest.model_validate_json(Path(command[1]).read_bytes())
        stdout = success(value).model_dump_json() if item.stdout is None else item.stdout
        return SkillResult(exit_code=item.exit_code, stdout=stdout,
            stderr="private-DO-NOT-ECHO", timed_out=item.timed_out)

    monkeypatch.setattr(api, "_systemd_supported", lambda: True)
    monkeypatch.setattr(api, "_private_project_runtime", lambda path: None)
    monkeypatch.setattr(api, "read_root_configuration", read_root)
    monkeypatch.setattr(api, "read_supply_configuration", read_supply)
    monkeypatch.setattr(api, "validate_configuration", validate)
    monkeypatch.setattr(api, "_write_request", write)
    monkeypatch.setattr(api, "build_metadata_preparation_root_command", lambda *a, **kw:
        ("fake-worker", str(kw["stage"] / "request.json")))
    monkeypatch.setattr(api, "_admit_unit", admit)
    monkeypatch.setattr(api, "run_subprocess_with_limits", subprocess)
    monkeypatch.setattr(broker, "_kill_and_confirm", stop)
    monkeypatch.setattr(broker, "_cleanup", cleanup)
    return item


async def handle(api: Any, item: Any, value: Any = None) -> Any:
    return await api.handle_metadata_preparation(item.broker, value or request(),
        peer_uid=item.broker.policy.allowed_uid)


def released(item: Any, key: str = KEY, slots: int = 8) -> None:
    job = item.broker.executions[key]
    assert job.finished.is_set() and job.quiet and not job.slot_acquired and job.task is None
    assert item.broker.slots._value == slots and list(item.root.iterdir()) == []


def unknown(item: Any) -> None:
    job = item.broker.executions[KEY]
    assert job.finished.is_set() and not job.quiet and job.slot_acquired and job.task is None
    assert item.broker.slots._value == 7
    assert json.loads((job.stage / "execution.json").read_bytes())["state"] == "UNKNOWN"
    assert "cleanup" not in item.events


@pytest.mark.parametrize("peer", [0, 10002, True])
async def test_uid_rejected_before_configuration_or_admission(api: Any, configured: Any, peer: int) -> None:
    result = await api.handle_metadata_preparation(configured.broker, request(), peer_uid=peer)
    assert result.error == "metadata_prepare_request_invalid"
    assert not configured.events and not configured.broker.executions


async def test_unsupported_host_allocates_nothing(api: Any, configured: Any,
    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_systemd_supported", lambda: False)
    assert (await handle(api, configured)).error == "metadata_prepare_unavailable"
    assert not configured.events and not configured.broker.executions


@pytest.mark.parametrize("path", ["request", "invocation", "scope"])
async def test_hidden_copied_fields_are_rejected_before_config(api: Any, configured: Any, path: str) -> None:
    value = request()
    if path == "request":
        value = value.model_copy(update={"secret": "hidden"})
    elif path == "invocation":
        value = value.model_copy(update={"planning": value.planning.model_copy(update={
            "invocation": value.invocation.model_copy(update={"secret": "hidden"})})})
    else:
        value = value.model_copy(update={"planning": value.planning.model_copy(update={
            "scope": value.planning.scope.model_copy(update={"secret": "hidden"})})})
    assert (await handle(api, configured, value)).error == "metadata_prepare_request_invalid"
    assert not configured.events and not configured.broker.executions


@pytest.mark.parametrize("inspect", [False, True])
async def test_valid_response_is_bound_and_both_units_stop_before_release(
    api: Any, configured: Any, inspect: bool,
) -> None:
    value = request(inspect)
    result = await handle(api, configured, value)
    assert result == success(value) and result.version == 10
    key = value.invocation.execution_id
    helper, child = "agent-hub-python-broker-" + key + ".service", "agent-hub-project-" + key + ".service"
    events = configured.events
    assert events.index("admit:" + helper) < events.index("admit:" + child) < events.index("spawn")
    assert events.index("stop:" + helper) < events.index("stop:" + child) < events.index("cleanup")
    released(configured, key)


@pytest.mark.parametrize("fence", ["finished", "cancelled", "retained"])
async def test_execution_id_is_never_reused(api: Any, configured: Any, fence: str) -> None:
    item = configured
    if fence == "finished":
        await handle(api, item)
    elif fence == "cancelled":
        item.broker._cancelled_ids.add(KEY)
    else:
        stage = item.root / (KEY + "-retained")
        stage.mkdir()
        (stage / "execution.json").write_text("{}", encoding="ascii")
    before = list(item.events)
    assert (await handle(api, item)).error == "metadata_prepare_consumed"
    assert item.events == before


@pytest.mark.parametrize("unit", [HELPER, CHILD])
@pytest.mark.parametrize("error_type", [ProcessCleanupIncomplete, RuntimeError, asyncio.CancelledError])
async def test_unconfirmed_stop_attempts_both_units_and_retains_unknown(api: Any, configured: Any,
    monkeypatch: pytest.MonkeyPatch, unit: str, error_type: type[BaseException]) -> None:
    async def stop(job: Any) -> None:
        configured.events.append("stop:" + job.unit)
        if job.unit == unit:
            raise error_type("private")
    monkeypatch.setattr(configured.broker, "_kill_and_confirm", stop)
    assert (await handle(api, configured)).error == "metadata_prepare_cleanup_incomplete"
    assert configured.events.count("stop:" + HELPER) == configured.events.count("stop:" + CHILD) == 1
    unknown(configured)


async def test_exit3_retains_stage_and_slot_even_after_quiescence(api: Any, configured: Any) -> None:
    configured.exit_code = 3
    assert (await handle(api, configured)).error == "metadata_prepare_cleanup_incomplete"
    unknown(configured)
    configured.broker.executions.clear()
    assert (await handle(api, configured)).error == "metadata_prepare_consumed"


@pytest.mark.parametrize("mutation", ["scope", "hash", "not_json", "extra", "version"])
async def test_untrusted_worker_stdout_never_authorizes_success(api: Any, configured: Any, mutation: str) -> None:
    payload = success(request()).model_dump(mode="json")
    if mutation == "scope": payload["scope"]["run_id"] = "00000000-0000-0000-0000-000000000009"
    if mutation == "hash": payload["receipt"]["plan"]["draft"]["source_sha256"] = "0" * 64
    if mutation == "extra": payload["url"] = "https://private.invalid"
    if mutation == "version": payload["version"] = 5
    configured.stdout = "private-DO-NOT-ECHO" if mutation == "not_json" else json.dumps(payload)
    result = await handle(api, configured)
    assert result.error == "metadata_prepare_unavailable" and "DO-NOT-ECHO" not in result.model_dump_json()
    released(configured)


async def test_repeated_cancellation_cannot_interrupt_two_unit_drain(api: Any, configured: Any,
    monkeypatch: pytest.MonkeyPatch) -> None:
    started, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def run(*args: Any, **kwargs: Any) -> SkillResult:
        kwargs["process_started"](SimpleNamespace(returncode=None))
        started.set()
        await asyncio.Future()
        raise AssertionError("cancelled worker returned")

    async def stop(job: Any) -> None:
        configured.events.append("stop:" + job.unit)
        if job.unit == HELPER:
            draining.set()
            await release.wait()

    monkeypatch.setattr(api, "run_subprocess_with_limits", run)
    monkeypatch.setattr(configured.broker, "_kill_and_confirm", stop)
    task = asyncio.create_task(handle(api, configured))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(draining.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and configured.broker.slots._value == 7
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        released(configured)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_root_command_changes_only_fixed_worker_module(api: Any) -> None:
    from agent_hub.skills.sandbox.python_input_supply_broker import build_input_supply_root_command

    _, root, supply, _ = inputs()
    value = request().invocation
    kwargs = {"configuration": root, "supply": supply, "stage": root.stage_root / (KEY + "-owned")}
    original = build_input_supply_root_command(value, BrokerPolicy(), **kwargs)
    actual = api.build_metadata_preparation_root_command(value, BrokerPolicy(), **kwargs)
    expected = tuple("agent_hub.skills.sandbox.python_metadata_preparation_worker"
        if x == "agent_hub.skills.sandbox.python_input_supply_worker" else x for x in original)
    assert actual == expected
    assert "PrivateNetwork=yes" in actual and "IPAddressDeny=any" in actual
    assert "ReadWritePaths=/private/stage /private/wheels /private/supply" in actual


@pytest.mark.parametrize("active", ["running", "finished", "foreign_scope", "foreign_version", "missing"])
async def test_inspect_progress_comes_only_from_a_matching_live_execution(
    api: Any, configured: Any, active: str,
) -> None:
    value = request(True)
    if active != "missing":
        invocation = request().invocation
        if active == "foreign_scope":
            invocation = invocation.model_copy(update={"project_id": "other"})
        execution_type = _Execution if active == "foreign_version" else api._MetadataPreparationExecution
        job = execution_type(invocation, asyncio.current_task(), launched=True)
        if active == "finished": job.finished.set()
        configured.broker.executions[KEY] = job
    response = success(value).model_copy(update={"status": PythonMetadataPreparationStatus(
        state="unknown", plan_sha256=value.expected_plan_sha256, preparation_execution_id=KEY)})
    configured.stdout = response.model_dump_json()
    result = await handle(api, configured, value)
    assert result.status.state == ("in_progress" if active == "running" else "unknown")
    released(configured, INSPECT_KEY)


async def test_forced_terminate_and_finally_share_one_two_unit_drain(api: Any, configured: Any,
    monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(*args: Any, **kwargs: Any) -> SkillResult:
        kwargs["process_started"](SimpleNamespace(returncode=137))
        await kwargs["on_forced_terminate"]()
        return SkillResult(exit_code=137, timed_out=True, stdout="", stderr="private")
    monkeypatch.setattr(api, "run_subprocess_with_limits", run)
    assert (await handle(api, configured)).error == "metadata_prepare_unavailable"
    assert configured.events.count("stop:" + HELPER) == configured.events.count("stop:" + CHILD) == 1
    released(configured)


async def test_waiting_for_shared_slot_allocates_no_stage_and_cancel_releases_no_slot(
    api: Any, configured: Any,
) -> None:
    configured.broker.slots = asyncio.Semaphore(0)
    task = asyncio.create_task(handle(api, configured))
    try:
        await asyncio.sleep(0)
        assert KEY in configured.broker.executions and not configured.admissions
        assert list(configured.root.iterdir()) == []
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        released(configured, slots=0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("unit", [HELPER, CHILD])
async def test_failed_admission_does_not_start_or_kill_an_existing_unit(api: Any, configured: Any,
    monkeypatch: pytest.MonkeyPatch, unit: str) -> None:
    original = api._admit_unit
    async def admit(broker: Any, job: Any) -> None:
        await original(broker, job)
        if job.unit == unit: raise ValueError("not quiet")
    monkeypatch.setattr(api, "_admit_unit", admit)
    assert (await handle(api, configured)).error == "metadata_prepare_unavailable"
    assert not any(x == "spawn" or x.startswith("stop:") for x in configured.events)
    released(configured)


@pytest.mark.parametrize("exit_code,timed_out", [(1, False), (0, True)])
async def test_nonzero_or_timeout_cannot_authorize_success_stdout(api: Any, configured: Any,
    exit_code: int, timed_out: bool) -> None:
    configured.exit_code, configured.timed_out = exit_code, timed_out
    assert (await handle(api, configured)).error == "metadata_prepare_unavailable"
    released(configured)
