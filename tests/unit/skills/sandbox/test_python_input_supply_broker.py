"""Pure broker orchestration: fake Root config/worker/systemd, never native proof."""

from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import OriginalProjectReference, ProjectExecutionBroker
from agent_hub.skills.sandbox.python_input_supply_catalog import InputSupplyError
from agent_hub.skills.sandbox.python_input_supply_protocol import (
    PythonInputSupplyArtifact,
    PythonInputSupplyPlan,
    PythonInputSupplyReceipt,
    PythonInputSupplyRequest,
    PythonInputSupplyResponse,
    PythonInputSupplyStatus,
    PythonSupplyInterpreter,
    SupplyAction,
)
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProjectProbeInvocation,
    PythonProjectScope,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

KEY = "input_supply_1"
HELPER = "agent-hub-python-broker-" + KEY + ".service"
CHILD = "agent-hub-project-" + KEY + ".service"


def invocation(key: str = KEY) -> PythonProjectProbeInvocation:
    return PythonProjectProbeInvocation(
        execution_id=key, tenant_id=UUID(int=1), run_id=UUID(int=2), project_id="billing",
        session_id="one", sandbox_profile="workspace_write", timeout_seconds=60,
        original_reference=OriginalProjectReference(contract_sha256="a" * 64,
                            input_source_sha256="b" * 64, file_count=6),
    )


def plan() -> PythonInputSupplyPlan:
    item = invocation()
    scope = PythonProjectScope.model_validate({name: getattr(item, name) for name in (
        "tenant_id", "run_id", "project_id", "session_id", "original_reference")}, strict=True)
    return PythonInputSupplyPlan(
        scope=scope, declaration_sha256="c" * 64, manifest_sha256="d" * 64,
        lock_sha256="e" * 64, catalog_sha256="f" * 64,
        interpreter=PythonSupplyInterpreter(implementation="cpython", version="3.12.14", abi="cp312",
                     platform="linux_x86_64", binary_sha256="1" * 64, base_runtime_sha256="2" * 64),
        artifacts=(PythonInputSupplyArtifact(filename="packaging-26.0-py3-none-any.whl",
            name="packaging", version="26.0", sha256="3" * 64, size_bytes=123),),
    )


def request(action: SupplyAction = "plan_python_project_inputs", key: str = KEY) -> PythonInputSupplyRequest:
    return PythonInputSupplyRequest(action=action, invocation=invocation(key),
        expected_plan_sha256=None if action == "plan_python_project_inputs" else plan().sha256,
        supply_execution_id="prior_supply" if action == "inspect_python_project_input_supply" else None)


def success(item: PythonInputSupplyRequest) -> PythonInputSupplyResponse:
    expected = plan()
    if item.action == "plan_python_project_inputs":
        return PythonInputSupplyResponse(action=item.action, execution_id=item.invocation.execution_id,
            ok=True, plan=expected, missing_wheels=(expected.artifacts[0].filename,))
    if item.action == "supply_python_project_inputs":
        receipt = PythonInputSupplyReceipt(plan=expected,
            supply_execution_id=item.invocation.execution_id,
            published=(expected.artifacts[0].filename,), reused=())
        return PythonInputSupplyResponse(action=item.action, execution_id=item.invocation.execution_id,
                                         ok=True, receipt=receipt)
    assert item.supply_execution_id is not None
    status = PythonInputSupplyStatus(state="absent", plan_sha256=expected.sha256,
                                     supply_execution_id=item.supply_execution_id)
    return PythonInputSupplyResponse(action=item.action, execution_id=item.invocation.execution_id,
                                     ok=True, status=status)


@pytest.fixture
def api() -> Any:
    return importlib.import_module("agent_hub.skills.sandbox.python_input_supply_broker")


@pytest.fixture
def configured(api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    stage = tmp_path / "stages"
    stage.mkdir(mode=0o700)
    policy = replace(BrokerPolicy(), runtime_root=tmp_path / "legacy",
                     python_project_runtime_root=stage)
    broker = ProjectExecutionBroker(policy, retained_runtime_roots=(stage,))
    root_config = SimpleNamespace(stage_root=stage, wheel_root=tmp_path / "wheels")
    supply_config = SimpleNamespace(state_root=tmp_path / "supply")
    item = SimpleNamespace(broker=broker, root=stage, events=[], admissions=[],
                           root_config=root_config, supply_config=supply_config,
                           stdout=None, exit_code=0, timed_out=False)
    backend = importlib.import_module("agent_hub.skills.sandbox.python_project_backend")
    catalog = importlib.import_module("agent_hub.skills.sandbox.python_input_supply_catalog")
    worker = importlib.import_module("agent_hub.skills.sandbox.python_input_supply_worker")

    def root_read() -> Any:
        item.events.append("root-config")
        return root_config

    def supply_read() -> Any:
        item.events.append("supply-config")
        return supply_config

    def validate(actual_policy: Any, actual_root: Any, actual_supply: Any) -> None:
        assert actual_policy is policy and actual_root is root_config and actual_supply is supply_config
        item.events.append("validate-config")

    def write(stage: Path, value: PythonInputSupplyRequest) -> None:
        # Only the native writer is replaced: the real broker metadata and cleanup remain active.
        assert stage.parent == item.root
        assert json.loads((stage / "execution.json").read_bytes())["state"] == "STAGED"
        item.events.append("write:" + value.invocation.execution_id)
        (stage / "request.json").write_bytes(value.model_dump_json().encode())

    def command(invocation: Any, actual_policy: Any, **kwargs: Any) -> tuple[str, ...]:
        assert actual_policy is policy
        assert kwargs["configuration"] is root_config and kwargs["supply"] is supply_config
        return ("fake-input-supply-worker", str(kwargs["stage"]))

    async def admit(actual_broker: Any, job: Any) -> None:
        assert actual_broker is broker
        assert job.slot_acquired or job.unit.startswith("agent-hub-python-broker-")
        item.admissions.append(job)
        item.events.append("admit:" + job.unit)

    async def stop(job: Any) -> None:
        item.events.append("stop:" + job.unit)

    original_cleanup = broker._cleanup

    async def cleanup(job: Any) -> None:
        item.events.append("cleanup:" + job.invocation.execution_id)
        await original_cleanup(job)

    async def subprocess(command: Any, limits: Any, **kwargs: Any) -> SkillResult:
        stage = Path(command[1])
        value = PythonInputSupplyRequest.model_validate_json((stage / "request.json").read_bytes())
        item.events.append("spawn:" + value.invocation.execution_id)
        assert 0 < limits.timeout_seconds <= value.invocation.timeout_seconds
        assert limits.output_limit_bytes == 2_000_000 and kwargs["stdin_payload"] == b""
        assert callable(kwargs["on_forced_terminate"])
        kwargs["process_started"](SimpleNamespace(returncode=item.exit_code))
        response = PythonInputSupplyResponse(action=value.action,
            execution_id=value.invocation.execution_id, ok=False, error="input_supply_artifact_missing")
        return SkillResult(exit_code=item.exit_code, timed_out=item.timed_out,
            stdout=response.model_dump_json() if item.stdout is None else item.stdout,
            stderr="private-worker-detail-DO-NOT-ECHO")

    monkeypatch.setattr(api, "_systemd_supported", lambda: True)
    monkeypatch.setattr(api, "_private_project_runtime", lambda path: None)
    monkeypatch.setattr(backend, "read_root_configuration", root_read)
    monkeypatch.setattr(catalog, "read_supply_configuration", supply_read)
    monkeypatch.setattr(worker, "validate_configuration", validate)
    monkeypatch.setattr(api, "_write_request", write)
    monkeypatch.setattr(api, "build_input_supply_root_command", command)
    monkeypatch.setattr(api, "_admit_unit", admit)
    monkeypatch.setattr(api, "run_subprocess_with_limits", subprocess)
    monkeypatch.setattr(broker, "_kill_and_confirm", stop)
    monkeypatch.setattr(broker, "_cleanup", cleanup)
    return item


async def handle(api: Any, item: Any, value: PythonInputSupplyRequest | None = None) -> PythonInputSupplyResponse:
    return cast(PythonInputSupplyResponse, await api.handle_input_supply(
        item.broker, value or request(), peer_uid=item.broker.policy.allowed_uid))


def assert_released(item: Any, key: str = KEY, slots: int = 8) -> None:
    job = item.broker.executions[key]
    assert job.finished.is_set() and job.quiet and not job.slot_acquired and job.task is None
    assert item.broker.slots._value == slots and list(item.root.iterdir()) == []


def assert_unknown(item: Any, slots: int = 7) -> Any:
    job = item.broker.executions[KEY]
    assert job.finished.is_set() and not job.quiet and job.slot_acquired and job.task is None
    assert item.broker.slots._value == slots and job.stage is not None and job.stage.is_dir()
    metadata = json.loads((job.stage / "execution.json").read_bytes())
    assert metadata["state"] == "UNKNOWN" and metadata["execution_id"] == KEY
    assert metadata["unit"] == CHILD
    assert "cleanup:" + KEY not in item.events
    return job


@pytest.mark.parametrize("peer", [0, 10002])
async def test_unauthorized_peer_cannot_read_config_touch_stage_or_take_slot(
    api: Any, configured: Any, peer: int,
) -> None:
    item = configured
    response = await api.handle_input_supply(item.broker, request(), peer_uid=peer)
    assert not response.ok and response.error == "input_supply_request_invalid"
    assert not item.events and not item.broker.executions and not list(item.root.iterdir())
    assert item.broker.slots._value == 8


async def test_unsupported_host_stops_before_config_or_admission(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "_systemd_supported", lambda: False)
    response = await handle(api, configured)
    assert response.error == "input_supply_unsupported" and not configured.events
    assert not configured.broker.executions and configured.broker.slots._value == 8


@pytest.mark.parametrize("boundary,code", [
    ("root", "input_supply_configuration_missing"),
    ("supply", "input_supply_catalog_unavailable"),
    ("validation", "input_supply_configuration_untrusted"),
])
async def test_configuration_rejections_preserve_typed_code_and_allocate_nothing(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch, boundary: str, code: Any,
) -> None:
    targets = {
        "root": ("python_project_backend", "read_root_configuration"),
        "supply": ("python_input_supply_catalog", "read_supply_configuration"),
        "validation": ("python_input_supply_worker", "validate_configuration"),
    }
    module, name = targets[boundary]
    def reject(*args: Any) -> None:
        raise InputSupplyError(code)
    monkeypatch.setattr(importlib.import_module("agent_hub.skills.sandbox." + module), name, reject)
    response = await handle(api, configured)
    assert not response.ok and response.error == code
    assert not configured.broker.executions and not list(configured.root.iterdir())
    assert configured.broker.slots._value == 8
    assert not any(event.startswith(("admit:", "spawn:")) for event in configured.events)


async def test_typed_worker_failure_drains_both_units_before_cleanup_and_slot_release(
    api: Any, configured: Any,
) -> None:
    item = configured
    response = await handle(api, item)
    assert not response.ok and response.error == "input_supply_artifact_missing"
    assert "DO-NOT-ECHO" not in response.model_dump_json()
    assert item.events.index("admit:" + HELPER) < item.events.index("admit:" + CHILD)
    assert item.events.index("stop:" + HELPER) < item.events.index("stop:" + CHILD)
    assert item.events.index("stop:" + CHILD) < item.events.index("cleanup:" + KEY)
    assert_released(item)
    assert all(job.process is None for job in item.admissions)


async def test_shared_slot_must_be_acquired_before_any_unit_or_stage_is_admitted(
    api: Any, configured: Any,
) -> None:
    item = configured
    item.broker.slots = asyncio.Semaphore(0)
    task = asyncio.create_task(handle(api, item))
    try:
        await asyncio.sleep(0)
        assert KEY in item.broker.executions and not task.done()
        assert not item.admissions and not list(item.root.iterdir())
        assert item.events == ["root-config", "supply-config", "validate-config"]
        item.broker.slots.release()
        response = await asyncio.wait_for(task, 2)
        assert response.error == "input_supply_artifact_missing"
        assert_released(item, slots=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelling_while_waiting_does_not_release_an_unowned_slot(
    api: Any, configured: Any,
) -> None:
    item = configured
    item.broker.slots = asyncio.Semaphore(0)
    task = asyncio.create_task(handle(api, item))
    try:
        await asyncio.sleep(0)
        assert KEY in item.broker.executions
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert_released(item, slots=0)
        assert not item.admissions and not any(event.startswith("stop:") for event in item.events)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("fence", ["cancel_tombstone", "finished_execution", "other_version_execution"])
async def test_duplicate_and_cancel_tombstones_reject_before_configuration(
    api: Any, configured: Any, fence: str,
) -> None:
    item = configured
    if fence == "cancel_tombstone":
        await item.broker.terminate(KEY)
    elif fence == "finished_execution":
        await handle(api, item)
    else:
        item.broker.executions[KEY] = SimpleNamespace(finished=asyncio.Event())
    before = list(item.events)
    response = await handle(api, item)
    assert response.error == "input_supply_consumed" and not response.ok
    assert item.events == before and item.broker.slots._value == 8


async def test_duplicate_of_a_queued_execution_cannot_claim_another_slot(
    api: Any, configured: Any,
) -> None:
    item = configured
    item.broker.slots = asyncio.Semaphore(0)
    task = asyncio.create_task(handle(api, item))
    try:
        await asyncio.sleep(0)
        before = list(item.events)
        response = await handle(api, item)
        assert response.error == "input_supply_consumed" and item.events == before
        assert len(item.broker.executions) == 1 and not item.admissions
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert_released(item, slots=0)


@pytest.mark.parametrize("timed_out", [False, True])
async def test_exit3_retains_unknown_stage_and_slot_even_after_both_units_are_quiet(
    api: Any, configured: Any, timed_out: bool,
) -> None:
    item = configured
    item.exit_code, item.timed_out = 3, timed_out
    # Even a valid typed body cannot override the helper's native-uncertain exit.
    item.stdout = success(request()).model_dump_json()
    response = await handle(api, item)
    assert response.error == "input_supply_cleanup_incomplete" and not response.ok
    job = assert_unknown(item)
    assert "stop:" + HELPER in item.events and "stop:" + CHILD in item.events
    assert all(unit.process is None for unit in item.admissions)
    before_bytes = {path.name: path.read_bytes() for path in job.stage.iterdir()}
    before = list(item.events)
    item.broker.executions.clear()
    restarted = ProjectExecutionBroker(item.broker.policy, retained_runtime_roots=(item.root,))
    replay = await api.handle_input_supply(restarted, request(), peer_uid=restarted.policy.allowed_uid)
    assert replay.error == "input_supply_consumed" and not replay.ok
    assert item.events == before and not restarted.executions
    assert {path.name: path.read_bytes() for path in job.stage.iterdir()} == before_bytes


@pytest.mark.parametrize("unit", [HELPER, CHILD])
@pytest.mark.parametrize("error_type", [ProcessCleanupIncomplete, RuntimeError, asyncio.CancelledError])
async def test_unconfirmed_stop_always_attempts_both_units_and_retains_unknown(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch,
    unit: str, error_type: type[BaseException],
) -> None:
    item = configured
    async def stop(job: Any) -> None:
        item.events.append("stop:" + job.unit)
        if job.unit == unit:
            raise error_type("unconfirmed fake unit")
    monkeypatch.setattr(item.broker, "_kill_and_confirm", stop)
    response = await handle(api, item)
    assert response.error == "input_supply_cleanup_incomplete"
    assert_unknown(item)
    assert item.events.count("stop:" + HELPER) == item.events.count("stop:" + CHILD) == 1


async def test_forced_terminate_callback_and_finally_share_one_drain_task(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = configured
    async def forced(*args: Any, **kwargs: Any) -> SkillResult:
        kwargs["process_started"](SimpleNamespace(returncode=137))
        await kwargs["on_forced_terminate"]()
        return SkillResult(exit_code=137, timed_out=True, stdout="", stderr="private")
    monkeypatch.setattr(api, "run_subprocess_with_limits", forced)
    response = await handle(api, item)
    assert response.error == "input_supply_unavailable"
    assert item.events.count("stop:" + HELPER) == item.events.count("stop:" + CHILD) == 1
    assert_released(item)


@pytest.mark.parametrize("blocked_unit", [HELPER, CHILD])
async def test_repeated_cancellation_cannot_interrupt_two_unit_cleanup(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch, blocked_unit: str,
) -> None:
    item = configured
    started, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def blocked(*args: Any, **kwargs: Any) -> SkillResult:
        kwargs["process_started"](SimpleNamespace(returncode=None))
        started.set()
        await asyncio.Future()
        pytest.fail("cancelled worker unexpectedly returned")
    async def stop(job: Any) -> None:
        item.events.append("stop:" + job.unit)
        if job.unit == blocked_unit:
            draining.set()
            await release.wait()
    monkeypatch.setattr(api, "run_subprocess_with_limits", blocked)
    monkeypatch.setattr(item.broker, "_kill_and_confirm", stop)
    task = asyncio.create_task(handle(api, item))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(draining.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        job = item.broker.executions[KEY]
        assert not task.done() and not job.finished.is_set() and job.stage.is_dir()
        assert item.broker.slots._value == 7 and "cleanup:" + KEY not in item.events
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert item.events.index("stop:" + CHILD) < item.events.index("cleanup:" + KEY)
        assert_released(item)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("unit", [HELPER, CHILD])
async def test_existing_nonquiet_unit_is_not_killed_or_started_by_failed_admission(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch, unit: str,
) -> None:
    item = configured
    original = api._admit_unit
    async def reject(actual_broker: Any, job: Any) -> None:
        await original(actual_broker, job)
        if job.unit == unit:
            raise ValueError("existing unit is not quiet")
    monkeypatch.setattr(api, "_admit_unit", reject)
    response = await handle(api, item)
    assert response.error == "input_supply_unavailable"
    assert not any(event.startswith(("stop:", "spawn:", "write:")) for event in item.events)
    assert_released(item)


async def test_both_unconfirmed_stops_still_retain_the_same_unknown_fence(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = configured
    async def fail(job: Any) -> None:
        item.events.append("stop:" + job.unit)
        raise ProcessCleanupIncomplete("both fake units unconfirmed")
    monkeypatch.setattr(item.broker, "_kill_and_confirm", fail)
    response = await handle(api, item)
    assert response.error == "input_supply_cleanup_incomplete"
    assert item.events.count("stop:" + HELPER) == item.events.count("stop:" + CHILD) == 1
    assert_unknown(item)


async def test_explicit_broker_terminate_waits_for_both_unit_groups(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = configured
    started = asyncio.Event()
    async def blocked(*args: Any, **kwargs: Any) -> SkillResult:
        kwargs["process_started"](SimpleNamespace(returncode=None))
        started.set()
        await asyncio.Future()
        pytest.fail("terminated worker unexpectedly returned")
    monkeypatch.setattr(api, "run_subprocess_with_limits", blocked)
    task = asyncio.create_task(handle(api, item))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(item.broker.terminate(KEY), 2)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "stop:" + HELPER in item.events and "stop:" + CHILD in item.events
        assert_released(item)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("action", ["plan_python_project_inputs", "supply_python_project_inputs",
                                     "inspect_python_project_input_supply"])
async def test_valid_typed_response_is_bound_and_returned_for_each_action(
    api: Any, configured: Any, action: SupplyAction,
) -> None:
    value = request(action)
    expected = success(value)
    configured.stdout = expected.model_dump_json()
    response = await handle(api, configured, value)
    assert response == expected and response.ok and response.version == 5
    assert_released(configured)


@pytest.mark.parametrize("mutation", ["wrong_id", "wrong_action", "version4", "string_ok",
    "unknown_error", "extra_field", "bare_success", "not_json", "trailing_output"])
async def test_invalid_or_misbound_worker_response_never_becomes_success(
    api: Any, configured: Any, mutation: str,
) -> None:
    payload = success(request()).model_dump(mode="json")
    if mutation == "wrong_id":
        payload["execution_id"] = "unrelated_execution"
    elif mutation == "wrong_action":
        payload = success(request("supply_python_project_inputs")).model_dump(mode="json")
    elif mutation == "version4":
        payload["version"] = 4
    elif mutation == "string_ok":
        payload["ok"] = "true"
    elif mutation == "unknown_error":
        payload = {"version": 5, "action": request().action, "execution_id": KEY,
                   "ok": False, "error": "private-untrusted-error-DO-NOT-ECHO"}
    elif mutation == "extra_field":
        payload["url"] = "https://private.invalid/DO-NOT-ECHO"
    elif mutation == "bare_success":
        payload = {"version": 5, "action": request().action, "execution_id": KEY, "ok": True}
    configured.stdout = ("private-DO-NOT-ECHO" if mutation == "not_json" else
                         json.dumps(payload) + ("\nprivate-DO-NOT-ECHO" if mutation == "trailing_output" else ""))
    response = await handle(api, configured)
    assert not response.ok and response.error == "input_supply_unavailable"
    assert "DO-NOT-ECHO" not in response.model_dump_json()
    assert_released(configured)


@pytest.mark.parametrize("field", ["tenant_id", "run_id", "project_id", "session_id", "original_reference"])
async def test_plan_scope_is_checked_against_original_request_not_worker_payload(
    api: Any, configured: Any, field: str,
) -> None:
    payload = success(request()).model_dump(mode="json")
    scope = payload["plan"]["scope"]
    if field in {"tenant_id", "run_id"}:
        scope[field] = str(UUID(int=999))
    elif field == "original_reference":
        scope[field]["contract_sha256"] = "0" * 64
    else:
        scope[field] = "different-scope"
    configured.stdout = json.dumps(payload)
    response = await handle(api, configured)
    assert response.error == "input_supply_unavailable" and not response.ok
    assert_released(configured)


@pytest.mark.parametrize("action,mutation", [
    ("supply_python_project_inputs", "plan"), ("supply_python_project_inputs", "execution"),
    ("inspect_python_project_input_supply", "plan"),
    ("inspect_python_project_input_supply", "execution"),
])
async def test_receipt_and_status_must_match_independent_expected_pins(
    api: Any, configured: Any, action: SupplyAction, mutation: str,
) -> None:
    value = request(action)
    payload = success(value).model_dump(mode="json")
    if action == "supply_python_project_inputs":
        if mutation == "plan":
            payload["receipt"]["plan"]["manifest_sha256"] = "0" * 64
        else:
            payload["receipt"]["supply_execution_id"] = "unrelated_supply"
    else:
        payload["status"]["plan_sha256" if mutation == "plan" else "supply_execution_id"] = (
            "0" * 64 if mutation == "plan" else "unrelated_supply")
    configured.stdout = json.dumps(payload)
    response = await handle(api, configured, value)
    assert response.error == "input_supply_unavailable" and not response.ok
    assert_released(configured)


@pytest.mark.parametrize("exit_code,timed_out", [(1, False), (137, False), (0, True)])
async def test_nonzero_or_timeout_cannot_authorize_even_valid_success_stdout(
    api: Any, configured: Any, exit_code: int, timed_out: bool,
) -> None:
    configured.exit_code, configured.timed_out = exit_code, timed_out
    configured.stdout = success(request()).model_dump_json()
    response = await handle(api, configured)
    assert response.error == "input_supply_unavailable" and not response.ok
    assert_released(configured)
