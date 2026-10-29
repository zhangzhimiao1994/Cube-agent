"""Offline DB hydration and real Crew receipts; never execute native cleanup."""
from __future__ import annotations

import copy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.db.models import RunCheckpointRow, RuntimeArtifactRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.harness.types import HarnessToolCallResult
from agent_hub.models.gateway import GatewayRejectedOutput
from agent_hub.models.types import RejectedOutputEvidence, TokenUsage, ToolCall
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import Artifact, RuntimeCheckpoint
from agent_hub.runtime.crew.adapter import RuntimeExecutionError
from agent_hub.skills.sandbox.base import SkillResult
from tests.unit.capabilities.test_python_failed_environment_lifecycle_oct28 import (
    POLICY,
    RELEASE,
    failed_session,
)
from tests.unit.capabilities.test_python_project_lifecycle import Rows, Session, invoke
from tests.unit.runtime.crew.test_adapter_failure_reason import (
    RecordingHarnessToolGateway,
    _context,
)
from tests.unit.runtime.crew.test_tool_incomplete_recovery import (
    ToolIncompleteGateway,
    subject,
    tool_plan,
)


class EvidenceSession(Session):
    saved: RunCheckpointRow
    private_rows: list[RuntimeArtifactRow]

    async def scalars(self, query: Any) -> Rows:
        self.queries.append(query)
        expression = query.column_descriptions[0]["expr"]
        if expression is RunCheckpointRow:
            return Rows([self.saved])
        if expression is RuntimeArtifactRow:
            assert self.open
            return Rows(self.private_rows)
        return Rows(self.checkpoints)


@pytest.fixture
async def evidence(request: pytest.FixtureRequest, tmp_path: Path) -> EvidenceSession:
    python_failure = getattr(request, 'param', '') == 'python'
    post_tool = getattr(request, 'param', '') in {'post_tool', 'post_tool_growth'}
    growth = getattr(request, 'param', '') == 'post_tool_growth'
    diagnostic_payload: dict[str, Any] = {"execution_diagnostic": dict(ExecutionDiagnostic(
        "python", SkillResult(exit_code=1, stdout="", stderr="failed", timed_out=False)).to_payload())}
    if python_failure:
        from tests.unit.runtime.test_python_project_history import history_case

        _, _, _, original, _ = await history_case(tmp_path)
        diagnostic_payload = {"execution_diagnostic": original.observation["execution_diagnostic"],
                              "python_verification_failure": original.observation["verification"]}

    class Gateway(ToolIncompleteGateway):
        async def complete_with_context(self, request: Any) -> Any:
            if not self.requests:
                completion = await super().complete_with_context(request)
                if python_failure:
                    completion = replace(completion, response=replace(completion.response, tool_calls=(
                        ToolCall(id="verify-red", name="project_verify", arguments={}),
                    )))
                return completion
            self.requests.append(request)
            raise GatewayRejectedOutput(
                evidence=RejectedOutputEvidence(final_text="invalid batch", usage=TokenUsage(1, 1, 2),
                    usage_status="known", status="completed", reason="invalid_tool"),
                deployment_id="selected", logical_model=request.logical_model,
                provider_id="provider", provider_model="provider/model", cost_usd=Decimal(0),
            )

    class Harness(RecordingHarnessToolGateway):
        async def invoke(self, tenant_id: Any, request: Any, **kwargs: Any) -> Any:
            return HarnessToolCallResult(
                call_id=request.call_id, tool_name=request.tool_name, status="failed",
                failure_reason="execution failed", payload=diagnostic_payload,
            )

    repository = InMemoryArtifactRepository()
    plan = tool_plan()
    if python_failure:
        plan = plan.model_copy(update={"allowed_tools": ("project.verify",),
            "agents": (plan.agents[0].model_copy(update={"allowed_tools": ("project.verify",)}), *plan.agents[1:]),
            "steps": (plan.steps[0].model_copy(update={"tools": ("project.verify",)}), *plan.steps[1:])})
    if post_tool:
        from tests.unit.runtime.crew.test_post_tool_finalization_growth import SaturatedGateway

        runtime = subject(SaturatedGateway('tool', 'incomplete', 'incomplete', capacity=8192),
                          plan, repository, RecordingHarnessToolGateway())
    else:
        runtime = subject(Gateway("tool"), plan, repository, Harness())
    events = []
    context = _context(token_budget=30_000, routing_decision={
        'post_tool_finalization_growth_version': 1,
    } if growth else {}).model_copy(update={"run_id": UUID(int=2), "tenant_id": UUID(int=1)})
    with pytest.raises(RuntimeExecutionError, match='growth exhausted' if growth else 'structured output invalid'):
        async for event in runtime.run(context):
            events.append(event)
    child = next(event.checkpoint for event in reversed(events)
                 if event.checkpoint is not None and event.checkpoint.state["phase"] == "running")
    parent = RuntimeCheckpoint(id=uuid4(), runtime_type="hybrid", runtime_version="3",
        run_id=child.run_id, tenant_id=child.tenant_id, mode=TaskMode.HYBRID, state={
            "plan_digest": "a" * 64, "artifact_registry": {}, "output_artifact_registry": {},
            "next_sequence": 10, "next_stage": 0, "terminal": False, "reason": None,
            "remaining_token_budget": 1000, "token_budget_grant": 2000, "token_budget_spent": 1000,
            "remaining_timeout_seconds": 10.0, "remaining_absolute_timeout_seconds": 10.0,
            "timeout_progress_units": 0, "last_child_progress_fingerprint": None,
            "child_checkpoint": cast(Any, child.to_payload()),
            "engineering_hybrid_report_version": 1,
        })
    session = EvidenceSession()
    session.row = failed_session().row
    assert (child.run_id, child.tenant_id) == (session.row.id, session.row.tenant_id)
    payload = parent.to_payload()
    session.saved = RunCheckpointRow(id=parent.id, tenant_id=parent.tenant_id, run_id=parent.run_id,
        sequence=10, runtime_type=parent.runtime_type, runtime_version=parent.runtime_version,
        mode=parent.mode.value, state=payload["state"], state_sha256=parent.state_sha256, payload=payload)
    session.checkpoints = [payload["state"]]
    session.private_rows = []
    for (tenant, run, artifact_id), artifact in repository._artifacts.items():
        raw = PostgresArtifactRepository._encode_payload(artifact.to_payload())
        session.private_rows.append(RuntimeArtifactRow(id=artifact_id, tenant_id=tenant, run_id=run,
            content_sha256=artifact.content_sha256, payload=raw, byte_size=len(raw.encode("utf-8"))))
    return session


@pytest.mark.parametrize('evidence', ['post_tool', 'post_tool_growth'], indirect=True)
async def test_known_incomplete_model_receipts_release_only_the_safe_terminal_environment(
    evidence: EvidenceSession,
) -> None:
    before = copy.deepcopy(evidence.saved.payload), copy.deepcopy(evidence.checkpoints)
    child = cast(Any, evidence.saved.payload)['state']['child_checkpoint']['state']
    assert any(row['failure_reason'] == 'model post-tool incomplete'
               for row in child['models'].values())
    assert all(row['status'] in {'succeeded', 'rejected'} for row in child['models'].values())
    assert any(row['output_status'] == 'incomplete' and row['usage_status'] == 'known'
               for row in child['rejected_outputs'].values())
    assert await invoke(evidence, callback_status=RunStatus.FAILED) == ['context', 'release']
    assert evidence.row.status == 'failed' and RELEASE in evidence.row.routing_decision
    assert (evidence.saved.payload, evidence.checkpoints) == before


async def test_verified_hybrid_failed_tool_releases_only_environment_and_preserves_history(
    evidence: EvidenceSession,
) -> None:
    before = copy.deepcopy(evidence.saved.payload), copy.deepcopy(evidence.checkpoints)
    assert await invoke(evidence, callback_status=RunStatus.FAILED) == ["context", "release"]
    assert evidence.row.status == "failed" and evidence.row.error_summary == "existing_failure"
    assert RELEASE in evidence.row.routing_decision
    assert (evidence.saved.payload, evidence.checkpoints) == before
    assert evidence.queries[0]._for_update_arg is not None


@pytest.mark.parametrize('evidence', ['python'], indirect=True)
async def test_actual_typed_python_failure_feedback_is_cleanup_evidence(evidence: EvidenceSession) -> None:
    assert await invoke(evidence, callback_status=RunStatus.FAILED) == ['context', 'release']


async def test_verified_hybrid_uses_existing_generated_readonly_release_chain(evidence: EvidenceSession) -> None:
    from tests.unit.capabilities.test_python_generated_lifecycle import generated_session, hook_for

    evidence.row.routing_decision = generated_session().row.routing_decision
    evidence.row.routing_decision[POLICY] = 1
    hook, calls = hook_for(evidence)
    await hook(tenant_id=evidence.row.tenant_id, run_id=evidence.row.id, status=RunStatus.FAILED,
               actor_id=None, mode=TaskMode.HYBRID, routing_decision={})
    assert calls[0] == 'generated-context' and len(calls) == 2
    assert RELEASE in evidence.row.routing_decision


async def test_verified_history_cannot_override_root_unknown(evidence: EvidenceSession) -> None:
    from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook
    from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
    from tests.unit.capabilities.test_python_generated_lifecycle import generated_session, hook_for

    evidence.row.routing_decision = generated_session().row.routing_decision
    evidence.row.routing_decision[POLICY] = 1
    hook, calls = hook_for(evidence)

    async def blocked(*args: Any, **kwargs: Any) -> Any:
        raise ProcessCleanupIncomplete('python_project_cleanup_incomplete')

    hook.executor.release_completed = blocked
    assert isinstance(hook, PythonProjectTerminalHook)
    with pytest.raises(ProcessCleanupIncomplete):
        await hook(tenant_id=evidence.row.tenant_id, run_id=evidence.row.id, status=RunStatus.FAILED,
                   actor_id=None, mode=TaskMode.HYBRID, routing_decision={})
    assert calls == ['generated-context'] and RELEASE not in evidence.row.routing_decision
    assert evidence.row.status == 'failed' and evidence.row.version == 1


def rehash(session: EvidenceSession) -> None:
    payload = cast(dict[str, Any], session.saved.payload)
    child = payload['state'].get('child_checkpoint')
    if child is not None:
        child['state_sha256'] = RuntimeCheckpoint.from_payload({**child, 'state_sha256': ''}).state_sha256
    checkpoint = RuntimeCheckpoint.from_payload({**payload, 'state_sha256': ''})
    session.saved.payload = checkpoint.to_payload()
    session.saved.state = cast(dict[str, object], session.saved.payload['state'])
    session.saved.state_sha256 = checkpoint.state_sha256
    session.checkpoints = [session.saved.state]


@pytest.mark.parametrize('evidence', ['post_tool_growth'], indirect=True)
@pytest.mark.parametrize('defect', [
    'unknown_outcome', 'unknown_usage', 'no_usage', 'wrong_provenance',
    'unexpected_reason', 'unexpected_failure', 'model_running', 'tool_running', 'missing_receipt',
])
async def test_incomplete_cleanup_never_turns_uncertainty_or_bad_receipts_into_authority(
    evidence: EvidenceSession, defect: str,
) -> None:
    child = cast(Any, evidence.saved.payload)['state']['child_checkpoint']['state']
    key, row = next((key, row) for key, row in child['models'].items()
                    if row['failure_reason'] == 'model post-tool incomplete')
    receipt = child['rejected_outputs'][key]
    if defect == 'unknown_outcome':
        receipt['output_status'] = 'unknown'
    elif defect == 'unknown_usage':
        receipt['usage_status'], receipt['usage'] = 'unknown', None
    elif defect == 'no_usage':
        receipt['usage'] = None
    elif defect == 'wrong_provenance':
        receipt['provenance'] = {}
    elif defect == 'unexpected_reason':
        receipt['reason'] = 'invalid_json'
    elif defect == 'unexpected_failure':
        row['failure_reason'] = 'unclassified model failure'
    elif defect == 'model_running':
        row['status'] = 'running'
    elif defect == 'tool_running':
        next(iter(child['tools'].values()))['status'] = 'running'
    elif defect == 'missing_receipt':
        child['rejected_outputs'].pop(key)
    rehash(evidence)
    before = copy.deepcopy(evidence.saved.payload)
    assert await invoke(evidence, callback_status=RunStatus.FAILED) == []
    assert RELEASE not in evidence.row.routing_decision
    assert evidence.saved.payload == before


async def test_fully_verified_direct_crew_failed_history_uses_same_gate(evidence: EvidenceSession) -> None:
    child = RuntimeCheckpoint.from_payload(cast(Any, evidence.saved.payload)['state']['child_checkpoint'])
    evidence.saved.id = child.id
    evidence.saved.runtime_type = child.runtime_type
    evidence.saved.runtime_version = child.runtime_version
    evidence.saved.mode = child.mode.value
    evidence.saved.payload = child.to_payload()
    rehash(evidence)
    assert await invoke(evidence, callback_status=RunStatus.FAILED) == ['context', 'release']


@pytest.mark.parametrize('marker', [None, True, False, '1', 1.0, 0, 2])
async def test_wrapper_never_backfills_or_coerces_failed_opt_in(evidence: EvidenceSession, marker: Any) -> None:
    if marker is None:
        evidence.row.routing_decision.pop(POLICY)
    else:
        evidence.row.routing_decision[POLICY] = marker
    assert await invoke(evidence, callback_status=RunStatus.FAILED) == []


@pytest.mark.parametrize('defect', ['public_result', 'wrong_producer', 'wrong_sources', 'wrong_arguments',
                                  'missing_batch', 'model_running', 'receipt_unknown', 'wrapper_extra'])
async def test_rehashed_but_unproven_semantics_are_not_release_authority(
    evidence: EvidenceSession, defect: str,
) -> None:
    child = cast(Any, evidence.saved.payload)['state']['child_checkpoint']['state']
    tool = next(iter(child['tools'].values()))
    if defect == 'missing_batch':
        child['tools'] = {}
    elif defect == 'model_running':
        next(iter(child['models'].values()))['status'] = 'running'
    elif defect == 'receipt_unknown':
        next(iter(child['rejected_outputs'].values()))['output_status'] = 'unknown'
    elif defect == 'wrapper_extra':
        cast(Any, evidence.saved.payload)['state']['other'] = {'phase': 'running'}
    else:
        row = next(row for row in evidence.private_rows if str(row.id) == tool['artifact_id'])
        original = PostgresArtifactRepository._hydrate(row)
        content = dict(original.content)
        if defect == 'public_result':
            content['result'] = {'status': 'failed', 'summary': 'claimed failure'}
        elif defect == 'wrong_arguments':
            content['arguments_sha256'] = 'a' * 64
        altered = Artifact(id=original.id, type=original.type,
            producer='other' if defect == 'wrong_producer' else original.producer,
            content=content, source_ids=() if defect == 'wrong_sources' else original.source_ids)
        row.payload = PostgresArtifactRepository._encode_payload(altered.to_payload())
        row.byte_size = len(row.payload.encode())
        row.content_sha256 = altered.content_sha256
        tool['sha256'] = altered.content_sha256
        child['artifact_registry'][str(altered.id)] = altered.content_sha256
    rehash(evidence)
    assert await invoke(evidence, callback_status=RunStatus.FAILED) == []
    assert RELEASE not in evidence.row.routing_decision


@pytest.mark.parametrize("defect", [
    "missing_artifact", "wrong_scope", "bad_payload", "bad_hash", "partial", "unknown", "prepared",
    "tool_running", "child_hash", "child_no_hash", "child_scope", "outer_hash", "no_marker",
    "route_executing", "nested_wrapper",
])
async def test_bad_or_unproven_history_never_releases(evidence: EvidenceSession, defect: str) -> None:
    state = cast(Any, evidence.saved.payload)["state"]
    child = state["child_checkpoint"]
    tool = next(iter(child["state"]["tools"].values()))
    if defect == "missing_artifact":
        evidence.private_rows = []
    elif defect == "wrong_scope":
        evidence.private_rows[0].run_id = UUID(int=99)
    elif defect == "bad_payload":
        evidence.private_rows[0].payload = "{}"
    elif defect == "bad_hash":
        tool["sha256"] = "0" * 64
    elif defect in {"partial", "unknown", "prepared", "tool_running"}:
        tool["status"] = "running" if defect == "tool_running" else defect
    elif defect == "child_hash":
        child["state_sha256"] = "0" * 64
    elif defect == "child_no_hash":
        child["state_sha256"] = ""
    elif defect == "child_scope":
        child["run_id"] = str(UUID(int=99))
    elif defect == "outer_hash":
        evidence.saved.state_sha256 = "0" * 64
    elif defect == "no_marker":
        evidence.row.routing_decision.pop(POLICY)
    elif defect == "route_executing":
        evidence.row.routing_decision["phase"] = "executing"
    elif defect == "nested_wrapper":
        child["runtime_type"] = "hybrid"
    # Recompute all legitimate transport hashes except the one intentionally corrupted.
    if defect not in {"child_hash", "child_no_hash"}:
        child["state_sha256"] = RuntimeCheckpoint.from_payload({**child, "state_sha256": ""}).state_sha256
    outer = RuntimeCheckpoint.from_payload({**evidence.saved.payload, "state_sha256": ""})
    evidence.saved.payload = outer.to_payload()
    evidence.saved.state = cast(dict[str, object], evidence.saved.payload["state"])
    if defect != "outer_hash":
        evidence.saved.state_sha256 = outer.state_sha256
    evidence.checkpoints = [evidence.saved.state]
    assert await invoke(evidence, callback_status=RunStatus.FAILED) == []
    assert RELEASE not in evidence.row.routing_decision
