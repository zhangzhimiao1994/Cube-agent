"""Real verifier/receipt CAS/private storage; SQL and native report transport are doubles."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from agent_hub.capabilities.project_prerequisites import validate_project_prerequisite_report
from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.runs.project_verification_receipts import RECEIPT_KEY
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import ProjectInvocation
from tests.unit.capabilities.test_project_prerequisites import raw_report
from tests.unit.capabilities.test_project_verification import Executor
from tests.unit.capabilities.test_scoped_read import project_path, write_file
from tests.unit.capabilities.test_verification_cold_recovery import Fixture, restore, verify
from tests.unit.runs.project_auto_repair_typed_fixture import sequence
from tests.unit.runs.reviewed_report_revision_typed_fixture import read_map

ERROR = "project verification receipt is unavailable or changed"
BLOCKED = "project verification blocked by execution prerequisites"


class PrerequisiteExecutor(Executor):
    def __init__(self, *, reason: str = "invalid_package_manifest") -> None:
        super().__init__()
        self.reason: str | None = reason

    async def run(self, invocation: ProjectInvocation) -> SkillResult:
        result = await super().run(invocation)
        if self.reason is None:
            return result
        statuses = ("passed", "not_run", "not_run") if self.reason == "input_lock_changed" else (
            "not_run", "not_run", "not_run")
        report = raw_report(statuses, reason=self.reason)
        report["source_sha256"] = json.loads(result.stdout)["source_sha256"]
        return result.model_copy(update={"stdout": json.dumps(report)})


def blocked_fixture(tmp_path: Path, *, reason: str = "invalid_package_manifest") -> tuple[Fixture, PrerequisiteExecutor]:
    fixture = Fixture(tmp_path)
    executor = PrerequisiteExecutor(reason=reason)
    executor.root = fixture.root
    fixture.executor = executor
    return fixture, executor


@pytest.mark.parametrize("old_passed", [False, True])
async def test_bound_manifest_block_closes_failed_allows_only_new_action(
    tmp_path: Path, old_passed: bool,
) -> None:
    fixture, executor = blocked_fixture(tmp_path)
    service = fixture.verifier()
    old_index = None
    if old_passed:
        executor.reason = None
        with fixture.bind():
            await verify(service, "old-passed")
        old_index = deepcopy(fixture.db.row.routing_decision[RECEIPT_KEY])
        executor.reason = "invalid_package_manifest"
    artifacts_before = set(fixture.artifacts._artifacts)
    with fixture.bind(), pytest.raises(ProjectVerificationError, match=BLOCKED) as caught:
        await verify(service, "manifest-blocked")
    observation = caught.value.project_prerequisite
    assert observation is not None and observation.reason == "invalid_package_manifest"
    issued = fixture.db.row.routing_decision[RECEIPT_KEY]
    assert issued["status"] == "failed", "a confirmed source-bound pre-execution rejection must not remain UNKNOWN"
    assert issued["source_sha256"] == observation.source_sha256 == observation.to_payload()["source_sha256"]
    assert all(read_map(check)["status"] == "not_run" for check in sequence(observation.to_payload()["checks"]))
    assert issued["action_key"] == "manifest-blocked" and issued["artifact_references"] == []
    assert set(fixture.artifacts._artifacts) == artifacts_before
    calls = len(executor.calls)
    with fixture.bind(), pytest.raises(ProjectVerificationError, match=ERROR):
        await verify(service, "manifest-blocked")
    assert len(executor.calls) == calls
    with pytest.raises(ProjectVerificationError):
        await service.require_current_receipt(fixture.db.row.tenant_id, fixture.db.row.id)
    if old_index is not None:
        assert issued["generation"] == old_index["generation"] + 1
        with fixture.bind(), pytest.raises(ProjectVerificationError):
            await restore(service, "old-passed")
    fixture.db.new_lease()
    cold = fixture.verifier()
    with fixture.bind(), pytest.raises(ProjectVerificationError):
        await restore(cold, "manifest-blocked")
    write_file(fixture.root, project_path() + "/package.json",
               '{"name":"repaired","scripts":{"build":"node -v","test":"node --test"}}')
    executor.reason = None
    with fixture.bind():
        result = await verify(cold, "manifest-repaired")
    assert result["verification"]["status"] == "passed"
    assert len(executor.calls) == calls + 1
    repaired = fixture.db.row.routing_decision[RECEIPT_KEY]
    assert repaired["status"] == "passed" and repaired["generation"] == issued["generation"] + 1
    assert repaired["source_sha256"] != issued["source_sha256"]


@pytest.mark.parametrize("reason", [
    "node_runtime_missing", "install_authorization_required", "unsupported_project", "node_runtime_untrusted",
    "input_lock_changed", "cleanup_deadline_exceeded", "deadline_exceeded", "work_quota_exceeded",
    "unsafe_input_entry", "phase_launch_unavailable",
])
async def test_other_typed_prerequisites_remain_running_and_cannot_restart(tmp_path: Path, reason: str) -> None:
    fixture, executor = blocked_fixture(tmp_path, reason=reason)
    service = fixture.verifier()
    with fixture.bind(), pytest.raises(ProjectVerificationError, match=BLOCKED) as caught:
        await verify(service)
    assert caught.value.project_prerequisite is not None
    assert caught.value.project_prerequisite.reason == reason
    assert fixture.db.row.routing_decision[RECEIPT_KEY]["status"] == "running"
    with fixture.bind(), pytest.raises(ProjectVerificationError, match=ERROR):
        await verify(fixture.verifier(), "different-action-cannot-restart")
    assert len(executor.calls) == 1 and not fixture.artifacts._artifacts


@pytest.mark.parametrize("defect", [
    "foreign_source", "payload_source", "untyped", "passed_check", "diagnostic", "project_failure", "python_failure",
])
async def test_invalid_or_mixed_prerequisite_cannot_complete_current_issuance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    fixture, executor = blocked_fixture(tmp_path)
    service = fixture.verifier()
    original = service._verify

    async def corrupt(**kwargs: Any) -> Any:
        try:
            return await original(**kwargs)
        except ProjectVerificationError as error:
            observation = error.project_prerequisite
            assert observation is not None
            if defect == "foreign_source":
                report = raw_report(reason="invalid_package_manifest")
                error.project_prerequisite = validate_project_prerequisite_report(report, report["source_sha256"])
            elif defect == "untyped":
                error.project_prerequisite = dict(observation.to_payload())  # type: ignore[assignment]
            elif defect in {"payload_source", "passed_check"}:
                report = json.loads(json.dumps(observation.to_payload(), default=dict))
                if defect == "payload_source":
                    report["source_sha256"] = "b" * 64
                else:
                    report["checks"][0] = {"phase": "install", "status": "passed"}
                object.__setattr__(observation, "_report", report)
            else:
                setattr(error, "python_project_failure" if defect == "python_failure" else defect, object())
            raise

    monkeypatch.setattr(service, "_verify", corrupt)
    with fixture.bind(), pytest.raises(ProjectVerificationError):
        await verify(service)
    assert fixture.db.row.routing_decision[RECEIPT_KEY]["status"] == "running"
    with fixture.bind(), pytest.raises(ProjectVerificationError, match=ERROR):
        await verify(fixture.verifier(), "different-action-cannot-restart")
    assert len(executor.calls) == 1 and not fixture.artifacts._artifacts


async def test_manifest_closure_rejects_a_lease_lost_during_native_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, executor = blocked_fixture(tmp_path)
    service = fixture.verifier()
    original = executor.run

    async def lose_lease(invocation: ProjectInvocation) -> SkillResult:
        result = await original(invocation)
        fixture.db.new_lease()
        return result

    monkeypatch.setattr(executor, "run", lose_lease)
    with fixture.bind(), pytest.raises(ProjectVerificationError, match=ERROR) as caught:
        await verify(service)
    assert caught.value.project_prerequisite is None
    assert fixture.db.row.routing_decision[RECEIPT_KEY]["status"] == "running"
    with fixture.bind(), pytest.raises(ProjectVerificationError, match=ERROR):
        await verify(fixture.verifier(), "new-lease-cannot-restart")
    assert len(executor.calls) == 1


@pytest.mark.parametrize("fault", ["before_commit", "after_commit"])
async def test_manifest_closure_cas_uncertainty_never_returns_a_recoverable_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    fixture, executor = blocked_fixture(tmp_path)
    service = fixture.verifier()
    assert service._durable is not None
    original = service._durable.receipts.complete
    completions = []

    async def uncertain(*args: Any, **kwargs: Any) -> Any:
        completions.append(kwargs["status"])
        if fault == "before_commit":
            fixture.db.failure = "commit"
        try:
            await original(*args, **kwargs)
        finally:
            fixture.db.failure = None
        raise OSError("private CAS acknowledgement lost")

    monkeypatch.setattr(service._durable.receipts, "complete", uncertain)
    with fixture.bind(), pytest.raises(ProjectVerificationError, match=ERROR) as caught:
        await verify(service)
    assert completions == ["failed"]
    assert caught.value.project_prerequisite is None and caught.value.project_failure is None
    assert fixture.db.row.routing_decision[RECEIPT_KEY]["status"] == (
        "running" if fault == "before_commit" else "failed")
    with fixture.bind(), pytest.raises(ProjectVerificationError):
        await restore(fixture.verifier())
    assert len(executor.calls) == 1 and not fixture.artifacts._artifacts


async def test_unknown_native_outcome_cannot_be_restarted_as_manifest_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, executor = blocked_fixture(tmp_path)
    service = fixture.verifier()
    original = executor.run

    async def unknown(invocation: ProjectInvocation) -> SkillResult:
        await original(invocation)
        raise OSError("native transport outcome lost")

    monkeypatch.setattr(executor, "run", unknown)
    with fixture.bind(), pytest.raises(ProjectVerificationError):
        await verify(service)
    assert fixture.db.row.routing_decision[RECEIPT_KEY]["status"] == "running"
    with fixture.bind(), pytest.raises(ProjectVerificationError, match=ERROR):
        await verify(fixture.verifier(), "new-action-cannot-restart")
    assert len(executor.calls) == 1
