"""Python helper orchestration shares execution fences, not Node payload schemas."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import ProjectExecutionBroker, ProjectInvocation
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProjectBrokerResponse,
    PythonProjectProbeInvocation,
    PythonProjectProbeRequest,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete


@pytest.fixture
def api() -> Any:
    name = "agent_hub.skills.sandbox.python_project_broker"
    assert importlib.util.find_spec(name) is not None, "Python root-helper orchestration missing"
    return importlib.import_module(name)


def invocation() -> ProjectInvocation:
    return ProjectInvocation(
        execution_id="python_1",
        tenant_id=UUID(int=1),
        run_id=UUID(int=2),
        project_id="billing",
        session_id="one",
        sandbox_profile="workspace_write",
        timeout_seconds=60,
    )


def test_helper_is_a_separate_contained_root_unit_not_a_project_venv(api: Any) -> None:
    configuration = SimpleNamespace(
        stage_root=Path("/var/lib/python-stages"),
        environment_root=Path("/var/lib/python-environments"),
    )
    args = api.build_python_root_worker_command(
        invocation(),
        BrokerPolicy(),
        configuration=configuration,
        stage=Path("/var/lib/python-stages/python_1-abc"),
    )
    properties = [args[index + 1] for index, value in enumerate(args) if value == "-p"]
    assert args[:7] == (
        "/usr/bin/systemd-run",
        "--wait",
        "--collect",
        "--pipe",
        "--quiet",
        "--unit",
        "agent-hub-python-broker-python_1",
    )
    assert "User=root" in properties and "KillMode=control-group" in properties
    assert "ProtectSystem=strict" in properties and "PrivateNetwork=yes" in properties
    assert "ReadWritePaths=/var/lib/python-stages /var/lib/python-environments" in properties
    assert (
        "CapabilityBoundingSet=" not in properties
    )  # Root must read private DynamicUser/service trees.
    assert "DynamicUser=yes" not in properties
    assert args[-3:] == (
        "agent_hub.skills.sandbox.python_project_backend",
        "--stage",
        "/var/lib/python-stages/python_1-abc",
    )
    assert "/usr/bin/env" in args and "-i" in args and "-I" in args


def test_helper_namespace_cannot_collide_with_project_execution_unit(api: Any) -> None:
    item = api.PythonRootExecution(invocation(), None)
    assert item.unit == "agent-hub-python-broker-python_1.service"
    assert item.unit != "agent-hub-project-python_1.service"


def test_helper_stage_cannot_escape_its_root_configuration(api: Any) -> None:
    with pytest.raises(ValueError):
        api.build_python_root_worker_command(
            invocation(),
            BrokerPolicy(),
            configuration=SimpleNamespace(
                stage_root=Path("/var/lib/python-stages"),
                environment_root=Path("/var/lib/python-environments"),
            ),
            stage=Path("/unrelated/python_1-abc"),
        )


def request() -> PythonProjectProbeRequest:
    return PythonProjectProbeRequest(
        invocation=PythonProjectProbeInvocation.model_validate(
            invocation().model_dump()
            | {
                "original_reference": {
                    "schema_version": 1,
                    "contract_sha256": "a" * 64,
                    "input_source_sha256": "b" * 64,
                    "file_count": 6,
                }
            },
            strict=True,
        )
    )


@pytest.mark.asyncio
async def test_wrong_peer_cannot_read_config_create_stage_or_claim_execution(
    api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = ProjectExecutionBroker(BrokerPolicy(runtime_root=tmp_path))

    def forbidden() -> None:
        pytest.fail("unauthorized configuration access")

    monkeypatch.setattr(api, "_configuration", forbidden)
    result = await api.handle_python_project(
        broker, request(), peer_uid=broker.policy.allowed_uid + 1
    )
    assert not result.ok and not broker.executions and list(tmp_path.iterdir()) == []


@pytest.fixture
def configured(api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    stage = tmp_path / "stages"
    stage.mkdir(mode=0o700)
    policy = replace(
        BrokerPolicy(), runtime_root=tmp_path / "legacy", python_project_runtime_root=stage
    )
    broker = ProjectExecutionBroker(policy, retained_runtime_roots=(stage,))
    monkeypatch.setattr(api, "_systemd_supported", lambda: True)
    monkeypatch.setattr(api, "_private_project_runtime", lambda root: None)
    monkeypatch.setattr(
        api,
        "_configuration",
        lambda: SimpleNamespace(stage_root=stage, environment_root=tmp_path / "environments"),
    )
    monkeypatch.setattr(
        api, "build_python_root_worker_command", lambda *args, **kwargs: ("test-root-helper",)
    )
    events = []

    async def stop(job: Any) -> None:
        events.append("stop:" + job.unit)

    monkeypatch.setattr(broker, "_kill_and_confirm", stop)

    async def subprocess(command: Any, limits: Any, **kwargs: Any) -> SkillResult:
        events.append("spawn")
        assert limits.output_limit_bytes >= request().invocation.output_limit_bytes * 16 + 65536
        kwargs["process_started"](SimpleNamespace(returncode=0))
        return SkillResult(
            exit_code=0,
            stdout='{"version":4,"action":"probe_python_project",'
            '"execution_id":"python_1","ok":false,"error":"python_project_runtime_missing"}',
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr(api, "run_subprocess_with_limits", subprocess)

    async def admission(broker: Any, job: Any) -> None:
        events.append("admit:" + job.unit)

    monkeypatch.setattr(api, "_admit_unit", admission)
    return SimpleNamespace(broker=broker, root=stage, events=events)


@pytest.mark.asyncio
async def test_typed_root_failure_stops_helper_before_stage_cleanup(
    api: Any, configured: Any
) -> None:
    item = configured
    response = await api.handle_python_project(
        item.broker, request(), peer_uid=item.broker.policy.allowed_uid
    )
    assert not response.ok and response.error == "python_project_runtime_missing"
    assert list(item.root.iterdir()) == []
    assert item.events.index("stop:agent-hub-python-broker-python_1.service") < item.events.index(
        "stop:agent-hub-project-python_1.service"
    )
    job = item.broker.executions["python_1"]
    assert job.finished.is_set() and job.quiet and item.broker.slots._value == 8


@pytest.mark.asyncio
async def test_unknown_cleanup_retains_stage_and_execution_slot(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = configured

    async def stop(job: Any) -> None:
        item.events.append("stop:" + job.unit)
        if job.unit.startswith("agent-hub-python-broker-"):
            raise ProcessCleanupIncomplete("unconfirmed helper")

    monkeypatch.setattr(item.broker, "_kill_and_confirm", stop)
    response = await api.handle_python_project(
        item.broker, request(), peer_uid=item.broker.policy.allowed_uid
    )
    assert not response.ok and response.error == "python_project_cleanup_incomplete"
    job = item.broker.executions["python_1"]
    assert not job.quiet and job.finished.is_set() and job.stage.is_dir()
    assert item.broker.slots._value == 7
    assert "stop:agent-hub-project-python_1.service" in item.events


@pytest.mark.asyncio
async def test_helper_native_uncertainty_exit_preserves_durable_fence_even_after_parent_drain(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = configured
    async def uncertain(*args: Any, **kwargs: Any) -> SkillResult:
        kwargs['process_started'](SimpleNamespace(returncode=3))
        return SkillResult(exit_code=3, timed_out=False, stdout='',
            stderr='python_project_cleanup_incomplete\n')
    monkeypatch.setattr(api, 'run_subprocess_with_limits', uncertain)
    response = await api.handle_python_project(item.broker, request(), peer_uid=item.broker.policy.allowed_uid)
    assert response.error == 'python_project_cleanup_incomplete' and not response.ok
    job = item.broker.executions['python_1']
    assert job.finished.is_set() and not job.quiet and job.stage.is_dir()
    assert item.broker.slots._value == 7
    assert json.loads((job.stage / 'execution.json').read_bytes())['state'] == 'UNKNOWN'
    assert 'stop:agent-hub-project-python_1.service' in item.events
    item.broker.executions.clear()
    before = list(item.events)
    replay = await api.handle_python_project(item.broker, request(), peer_uid=item.broker.policy.allowed_uid)
    assert not replay.ok and item.events == before


@pytest.mark.asyncio
async def test_duplicate_and_cancel_tombstone_never_launch_helper(
    api: Any, configured: Any
) -> None:
    item = configured
    item.broker._cancelled_ids.add("python_1")
    response = await api.handle_python_project(
        item.broker, request(), peer_uid=item.broker.policy.allowed_uid
    )
    assert not response.ok and "spawn" not in item.events and not item.broker.executions


@pytest.mark.asyncio
async def test_cancellation_drains_both_unit_groups_before_releasing_slot(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = configured
    started = asyncio.Event()

    async def blocked(*args: Any, **kwargs: Any) -> SkillResult:
        kwargs["process_started"](SimpleNamespace(returncode=None))
        started.set()
        await asyncio.Future()
        pytest.fail("cancelled helper returned success")

    monkeypatch.setattr(api, "run_subprocess_with_limits", blocked)
    pending = asyncio.create_task(
        api.handle_python_project(item.broker, request(), peer_uid=item.broker.policy.allowed_uid)
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert list(item.root.iterdir()) == [] and item.broker.slots._value == 8
    assert item.broker.executions["python_1"].finished.is_set()


@pytest.mark.asyncio
async def test_explicit_v4_wire_uses_python_handler_and_typed_response(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("agent_hub.skills.sandbox.broker")
    broker = module.SkillBroker(BrokerPolicy())
    calls = []

    async def handle(item: Any, *, peer_uid: int) -> PythonProjectBrokerResponse:
        calls.append((item, peer_uid))
        return PythonProjectBrokerResponse(
            action=item.action,
            execution_id=item.invocation.execution_id,
            ok=False,
            error="python_project_runtime_missing",
        )

    monkeypatch.setattr(broker, "handle_python_project", handle)
    monkeypatch.setattr(module, "_peer_uid", lambda writer: 10001)

    class Writer:
        def __init__(self) -> None:
            self.data = bytearray()
            self.closed = False

        def write(self, value: bytes) -> None:
            self.data.extend(value)

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            pass

    reader, writer = asyncio.StreamReader(), Writer()
    item = request()
    body = item.model_dump_json().encode()
    reader.feed_data(len(body).to_bytes(4) + body)
    reader.feed_eof()
    await module._serve_client(reader, writer, broker)
    assert calls == [(item, 10001)] and writer.closed
    response = json.loads(writer.data[4:])
    assert response["version"] == 4 and response["error"] == "python_project_runtime_missing"
