"""Finite broker diagnostics retain UNKNOWN without exporting private inputs."""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_hub.skills.sandbox import python_metadata_preparation_broker as api
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import (
    ProjectExecutionBroker,
    ProjectInvocation,
    _Execution,
    _Limits,
)
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from agent_hub.skills.sandbox.python_metadata_preparation_protocol import (
    PythonMetadataPreparationRequest,
)
from agent_hub.skills.sandbox.python_project_backend import RootConfig
from agent_hub.skills.sandbox.python_resolution_protocol import ResolutionClaim, ResolutionRequest
from agent_hub.skills.sandbox.python_resolution_store import ResolutionLeaseWitness
from tests.unit.skills.sandbox.test_resolution_lease_witness import request
from tests.unit.skills.test_python_dependency_index_store import (
    root_configuration,
    supply_configuration,
)


async def test_configuration_failure_logs_validated_correlation_without_private_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    value = request().model_copy(update={"action": "inspect_python_resolution"})
    broker = ProjectExecutionBroker(BrokerPolicy(runtime_root=tmp_path))

    def unavailable() -> RootConfig:
        raise ValueError("PRIVATE_EXCEPTION /private/source token=SECRET")

    monkeypatch.setattr(api, "_systemd_supported", lambda: True)
    monkeypatch.setattr(api, "read_root_configuration", unavailable)
    with caplog.at_level(logging.WARNING, logger=api.__name__):
        result = await api.handle_resolution_preparation(
            broker, value, peer_uid=broker.policy.allowed_uid,
        )
    assert result.state == "unknown" and result.key == value.key
    assert not broker.executions and broker.slots._value == 8
    records = [record for record in caplog.records if record.name == api.__name__]
    assert len(records) == 1
    record = records[0]
    assert record.getMessage().startswith("python_preparation_failed action=inspect_python_resolution ")
    assert f"key={value.key}" in record.getMessage()
    assert "phase=configuration" in record.getMessage()
    assert "code=metadata_prepare_unavailable" in record.getMessage()
    assert record.__dict__["action"] == "inspect_python_resolution"
    assert record.__dict__["phase"] == "configuration"
    assert record.__dict__["key"] == value.key
    assert record.__dict__["code"] == "metadata_prepare_unavailable"
    assert record.exc_info is None and record.stack_info is None
    rendered = repr(record.__dict__)
    for private in ("PRIVATE_EXCEPTION", "/private/source", "SECRET",
                    str(value.claim.lease_token), value.claim.worker_id,
                    value.claim.planning.draft.source_sha256):
        assert private not in rendered


@pytest.mark.parametrize("defect", ["peer", "action", "hidden_claim"])
async def test_unvalidated_requests_do_not_log_claim_action_or_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    defect: str,
) -> None:
    value = request()
    broker = ProjectExecutionBroker(BrokerPolicy(runtime_root=tmp_path))
    peer = broker.policy.allowed_uid
    if defect == "peer":
        peer += 1
    elif defect == "action":
        value = value.model_copy(update={"action": "PRIVATE_UNVALIDATED_ACTION"})
    else:
        value = value.model_copy(update={"claim": value.claim.model_copy(
            update={"private": "PRIVATE_UNVALIDATED_CLAIM"})})
    monkeypatch.setattr(api, "_systemd_supported", lambda: True)
    with caplog.at_level(logging.WARNING, logger=api.__name__):
        result = await api.handle_resolution_preparation(broker, value, peer_uid=peer)
    assert result.state == "unknown" and not broker.executions
    records = [record for record in caplog.records if record.name == api.__name__]
    assert len(records) == 1
    record = records[0]
    assert record.__dict__["phase"] == "request"
    assert record.__dict__["action"] is None and record.__dict__["key"] is None
    assert record.__dict__["code"] == "metadata_prepare_request_invalid"
    assert "PRIVATE_UNVALIDATED" not in repr(record.__dict__)
    assert record.exc_info is None and record.stack_info is None


@dataclass
class BrokerCase:
    broker: ProjectExecutionBroker
    stage: Path
    stops: list[str]


def portable_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_phase: str,
    exit_code: int | None, timed_out: bool,
) -> BrokerCase:
    """Stub only native IO; retain real broker admission, drain and cleanup state."""
    broker = ProjectExecutionBroker(BrokerPolicy(runtime_root=tmp_path))
    stage = tmp_path / "new-stage"
    stops: list[str] = []

    async def admit(actual: ProjectExecutionBroker, job: _Execution) -> None:
        assert actual is broker and job.invocation == request().invocation
        if failure_phase == "admission":
            raise ValueError("PRIVATE_ADMISSION /private/source")

    def private_runtime(path: Path) -> None:
        if failure_phase == "stage":
            raise OSError("PRIVATE_STAGE /private/source")

    def make_stage(*, prefix: str, dir: Path) -> str:
        assert prefix == request().invocation.execution_id + "-"
        stage.mkdir()
        return str(stage)

    def write_request(path: Path, value: PythonMetadataPreparationRequest | ResolutionRequest) -> None:
        assert path == stage and value.invocation == request().invocation

    def initialize(self: ResolutionLeaseWitness, current: ResolutionClaim | None = None) -> None:
        assert current is not None

    def command(
        invocation: ProjectInvocation, policy: BrokerPolicy, *, configuration: RootConfig,
        supply: RootSupplyConfig, stage: Path,
    ) -> tuple[str, ...]:
        return ("agent_hub.skills.sandbox.python_metadata_preparation_worker",)

    async def run(command: tuple[str, ...], limits: _Limits, **kwargs: object) -> SkillResult:
        return SkillResult(exit_code=exit_code, timed_out=timed_out,
            stdout="PRIVATE_STDOUT /private/source", stderr="PRIVATE_STDERR token=SECRET")

    async def stop(job: _Execution) -> None:
        stops.append(job.unit)
        if failure_phase == "cleanup":
            raise RuntimeError("PRIVATE_CLEANUP token=SECRET")

    monkeypatch.setattr(api, "_systemd_supported", lambda: True)
    monkeypatch.setattr(api, "read_root_configuration", root_configuration)
    monkeypatch.setattr(api, "read_supply_configuration", supply_configuration)
    monkeypatch.setattr(api, "validate_configuration", lambda *args: None)
    monkeypatch.setattr(api, "_admit_unit", admit)
    monkeypatch.setattr(api, "_private_project_runtime", private_runtime)
    monkeypatch.setattr(tempfile, "mkdtemp", make_stage)
    monkeypatch.setattr(api, "_write_request", write_request)
    monkeypatch.setattr(ResolutionLeaseWitness, "initialize", initialize)
    monkeypatch.setattr(api, "build_metadata_preparation_root_command", command)
    monkeypatch.setattr(api, "run_subprocess_with_limits", run)
    monkeypatch.setattr(broker, "_kill_and_confirm", stop)
    return BrokerCase(broker, stage, stops)


@pytest.mark.parametrize("failure_phase,exit_code,timed_out,want_phase,want_code", [
    ("admission", 0, False, "admission", "metadata_prepare_unavailable"),
    ("stage", 0, False, "stage", "metadata_prepare_unavailable"),
    ("execution", 7, False, "execution", "metadata_prepare_unavailable"),
    ("execution", None, True, "execution", "metadata_prepare_unavailable"),
    ("execution", 3, False, "cleanup", "metadata_prepare_cleanup_incomplete"),
    ("response", 0, False, "response", "metadata_prepare_unavailable"),
    ("cleanup", 0, False, "cleanup", "metadata_prepare_cleanup_incomplete"),
])
async def test_failures_log_finite_phase_and_exit_preserving_drain_and_no_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    failure_phase: str, exit_code: int | None, timed_out: bool,
    want_phase: str, want_code: str,
) -> None:
    case = portable_broker(tmp_path, monkeypatch, failure_phase, exit_code, timed_out)
    value = request().model_copy(update={"action": "inspect_python_resolution"})
    with caplog.at_level(logging.WARNING, logger=api.__name__):
        result = await api.handle_resolution_preparation(
            case.broker, value, peer_uid=case.broker.policy.allowed_uid,
        )
    assert result.state == "unknown" and result.key == value.key
    record, = [record for record in caplog.records if record.name == api.__name__]
    assert record.__dict__["action"] == "inspect_python_resolution"
    assert record.__dict__["key"] == value.key
    assert record.__dict__["phase"] == want_phase and record.__dict__["code"] == want_code
    if failure_phase == "execution":
        assert record.__dict__["exit_code"] == exit_code
        assert record.__dict__["timed_out"] is timed_out
    assert record.exc_info is None and record.stack_info is None
    rendered = repr(record.__dict__)
    for private in ("PRIVATE_", "/private/source", "SECRET", str(value.claim.lease_token),
                    value.claim.planning.draft.source_sha256):
        assert private not in rendered
    job = case.broker.executions[value.invocation.execution_id]
    assert job.finished.is_set() and job.task is None
    if failure_phase in {"execution", "response", "cleanup"}:
        assert case.stops == ["agent-hub-python-broker-resolution_test.service",
                              "agent-hub-project-resolution_test.service"]
    if failure_phase == "cleanup" or exit_code == 3:
        assert not job.quiet and job.slot_acquired and case.stage.exists()
        assert case.broker.slots._value == 7
    else:
        assert job.quiet and not job.slot_acquired and not case.stage.exists()
        assert case.broker.slots._value == 8
    before = list(case.stops)
    caplog.clear()
    again = await api.handle_resolution_preparation(
        case.broker, value, peer_uid=case.broker.policy.allowed_uid,
    )
    assert again.state == "unknown" and case.stops == before
    second, = [record for record in caplog.records if record.name == api.__name__]
    assert second.__dict__["code"] == "metadata_prepare_consumed"
