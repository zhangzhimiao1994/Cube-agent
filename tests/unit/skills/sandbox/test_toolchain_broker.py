"""SDK broker orchestration tests, not native systemd or SDK supply evidence."""

import asyncio
import importlib
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.skills.sandbox import toolchain_protocol
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy, SkillBroker
from agent_hub.skills.sandbox.project import ProjectExecutionBroker
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainCatalog
from tests.unit.skills.sandbox.test_toolchain_catalog import catalog
from tests.unit.skills.sandbox.test_toolchain_protocol import invocation, plan


def test_sdk_root_lifecycle_adapter_exists() -> None:
    assert importlib.util.find_spec("agent_hub.skills.sandbox.toolchain_broker") is not None


@pytest.fixture
def api() -> Any:
    name = "agent_hub.skills.sandbox.toolchain_broker"
    if importlib.util.find_spec(name) is None:
        pytest.skip("SDK adapter absent")
    return importlib.import_module(name)


def test_toolchain_unknown_stages_participate_in_global_duplicate_fence() -> None:
    policy = BrokerPolicy()
    assert policy.toolchain_runtime_root == Path("/var/lib/agent-hub-toolchain-stages")
    broker = SkillBroker(policy)
    assert policy.toolchain_runtime_root in broker._project_broker.retained_runtime_roots


def test_root_helper_is_offline_with_only_exact_root_supply_destinations(api: Any) -> None:
    config = ToolchainCatalog.from_payload(catalog())
    call = invocation(toolchain_protocol)
    stage = Path(config.stage_root) / "sdk_test-fixed"
    command = api.build_toolchain_root_command(call, BrokerPolicy(), catalog=config, stage=stage)
    text = " ".join(command)
    assert "User=root" in command
    assert "NoNewPrivileges=yes" in command
    assert "PrivateNetwork=yes" in command
    assert "IPAddressDeny=any" in command
    assert "RestrictAddressFamilies=AF_UNIX" in command
    assert "ProtectSystem=strict" in command
    assert "agent_hub.skills.sandbox.toolchain_worker" in command
    assert "agent-hub-toolchain-broker-sdk_test" in command
    writes = next(p for p in command if p.startswith("ReadWritePaths=")).split("=", 1)[1]
    assert set(writes.split()) == {
        config.stage_root,
        config.state_root,
        config.runtime_root,
        BrokerPolicy().python_project_runtime_root.as_posix(),
    }
    assert str(BrokerPolicy().project_workspace_root) not in writes
    assert "-I" in command and "-B" in command
    assert "/usr/bin/env" in command and "-i" in command
    assert "dl.google.com" not in text


@pytest.mark.asyncio
async def test_peer_check_precedes_any_configuration_read(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden() -> None:
        pytest.fail("unauthorized peer reached Root configuration")

    monkeypatch.setattr(api, "_configuration", forbidden)
    broker = SkillBroker(BrokerPolicy())._project_broker
    call = invocation(toolchain_protocol)
    request = toolchain_protocol.ToolchainRequest(
        action="supply_project_toolchain", invocation=call, plan=plan(toolchain_protocol)
    )
    result = await api.handle_toolchain_request(broker, request, peer_uid=99999)
    assert not result.ok and result.error == "toolchain_request_invalid"
    assert not broker.executions


@pytest.fixture
def orchestration(api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    stage = tmp_path / "stages"
    stage.mkdir(mode=0o700)
    config = ToolchainCatalog.from_payload(catalog()).model_copy(update={"stage_root": str(stage)})
    policy = replace(BrokerPolicy(), toolchain_runtime_root=stage, runtime_root=tmp_path / "legacy")
    broker = ProjectExecutionBroker(policy, retained_runtime_roots=(stage,))
    worker = importlib.import_module("agent_hub.skills.sandbox.toolchain_worker")
    item = SimpleNamespace(
        broker=broker,
        root=stage,
        events=[],
        exit_code=0,
        invalid=False,
        timed_out=False,
        cancel=False,
        fail_drain=None,
    )
    monkeypatch.setattr(api, "_configuration", lambda: config)
    monkeypatch.setattr(api, "_systemd_supported", lambda: True)
    monkeypatch.setattr(api, "_private_project_runtime", lambda root: None)
    monkeypatch.setattr(worker, "validate_toolchain_configuration", lambda *args: None)
    monkeypatch.setattr(
        api, "build_toolchain_root_command", lambda *args, **kw: (str(kw["stage"]),)
    )

    async def admit(actual: Any, job: Any) -> None:
        assert actual is broker
        item.events.append("admit:" + job.unit)

    async def drain(job: Any) -> None:
        item.events.append("drain:" + job.unit)
        if item.fail_drain == job.unit:
            raise OSError("private-drain-failure")

    async def run(command: Any, limits: Any, **kw: Any) -> SkillResult:
        assert kw["termination_grace_seconds"] == toolchain_protocol.TOOLCHAIN_TERMINATION_SECONDS
        request = toolchain_protocol.parse_toolchain_request(
            (Path(command[0]) / "request.json").read_bytes()
        )
        item.events.append("spawn")
        if item.cancel:
            raise asyncio.CancelledError
        response = toolchain_protocol.ToolchainResponse(
            action=request.action,
            execution_id=request.invocation.execution_id,
            scope=request.invocation.scope,
            ok=False,
            error="toolchain_catalog_no_match",
        )
        return SkillResult(
            exit_code=item.exit_code,
            timed_out=item.timed_out,
            stdout="invalid" if item.invalid else response.model_dump_json(),
            stderr="do-not-echo-private",
        )

    original = broker._cleanup

    async def cleanup(job: Any) -> None:
        item.events.append("cleanup")
        await original(job)

    monkeypatch.setattr(api, "_admit_unit", admit)
    monkeypatch.setattr(api, "run_subprocess_with_limits", run)
    monkeypatch.setattr(broker, "_kill_and_confirm", drain)
    monkeypatch.setattr(broker, "_cleanup", cleanup)
    return item


def planning() -> toolchain_protocol.ToolchainRequest:
    return toolchain_protocol.ToolchainRequest(
        action="plan_project_toolchain", invocation=invocation(toolchain_protocol)
    )


@pytest.mark.asyncio
async def test_finite_outcome_drains_helper_then_fetch_before_cleanup(
    api: Any, orchestration: Any
) -> None:
    item = orchestration
    response = await api.handle_toolchain_request(item.broker, planning(), peer_uid=10001)
    assert response.error == "toolchain_catalog_no_match"
    assert item.events[-3:] == [
        "drain:agent-hub-toolchain-broker-sdk_test.service",
        "drain:agent-hub-project-sdk_test.service",
        "cleanup",
    ]
    assert item.broker.slots._value == 8 and not list(item.root.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["native3", "exit1", "timeout", "invalid", "cancel", "drain"])
async def test_uncertain_native_outcome_retains_stage_and_slot(
    api: Any,
    orchestration: Any,
    boundary: str,
) -> None:
    item = orchestration
    if boundary in {"native3", "exit1"}:
        item.exit_code = 3 if boundary == "native3" else 1
    elif boundary == "drain":
        item.fail_drain = "agent-hub-toolchain-broker-sdk_test.service"
    else:
        setattr(item, {"timeout": "timed_out"}.get(boundary, boundary), True)
    response = await api.handle_toolchain_request(item.broker, planning(), peer_uid=10001)
    assert response.error == "toolchain_cleanup_incomplete"
    job = item.broker.executions["sdk_test"]
    assert not job.quiet and job.slot_acquired and job.finished.is_set() and job.task is None
    assert item.broker.slots._value == 7 and "cleanup" not in item.events
    assert json.loads((job.stage / "execution.json").read_bytes())["state"] == "UNKNOWN"
    assert item.events[-2:] == [
        "drain:agent-hub-toolchain-broker-sdk_test.service",
        "drain:agent-hub-project-sdk_test.service",
    ]


@pytest.mark.asyncio
async def test_each_of_two_units_gets_its_full_drain_budget(
    api: Any,
    orchestration: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = orchestration

    async def delayed(job: Any) -> None:
        await asyncio.sleep(4.1)

    monkeypatch.setattr(item.broker, "_kill_and_confirm", delayed)
    response = await api.handle_toolchain_request(item.broker, planning(), peer_uid=10001)
    assert response.error == "toolchain_catalog_no_match"
    job = item.broker.executions["sdk_test"]
    assert job.termination_grace_seconds >= 16
    assert item.broker.slots._value == 8
