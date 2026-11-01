from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime import defaults
from agent_hub.runtime.autogen.adapter import _checkpoint_ack_selected
from agent_hub.runtime.contracts import RuntimeCheckpoint
from agent_hub.runtime.engineering_admission import (
    EngineeringAdmissionRuntime,
    _fingerprint,
    admission_checkpoint_valid,
    with_engineering_admission,
)
from tests.unit.runs.project_auto_repair_typed_fixture import present
from tests.unit.runs.test_repository_approval_upserts import ApprovalProjectionSession
from tests.unit.runtime.test_action_admission_helper import (
    NoExecutionBackend,
    OfflineCapacity,
    OfflineTransport,
    factory,
    task,
)
from tests.unit.runtime.test_engineering_admission import Authorizer, Child, context


def selected(mode: TaskMode = TaskMode.DISCUSS, **route: Any) -> Any:
    ctx = context(mode)
    return ctx.model_copy(update={'routing_decision': {
        **ctx.routing_decision, 'permission_mode': 'default',
        'engineering_action_admission_version': 1, **route,
    }})


@pytest.mark.parametrize('mode', [TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
async def test_ready_selected_child_receives_unchanged_context_without_dummy_authorization(mode: TaskMode) -> None:
    ctx = selected(mode)
    child, authorizer = Child(mode), Authorizer()
    runtime = with_engineering_admission(child, authorizer, ctx, concrete_actions=True)
    assert runtime is child
    _ = [event async for event in runtime.run(ctx)]
    assert child.calls == [ctx] and authorizer.calls == []


@pytest.mark.parametrize('marker', [None, True, '1', 0, 2])
def test_only_exact_action_admission_policy_may_skip_dummy_gate(marker: Any) -> None:
    ctx = selected(engineering_action_admission_version=marker)
    assert isinstance(with_engineering_admission(Child(ctx.mode), Authorizer(), ctx, concrete_actions=True),
                      EngineeringAdmissionRuntime)


@pytest.mark.parametrize('ready', [False, None, 1, 'true'])
def test_factory_must_attest_actual_concrete_checkpoint_wiring(ready: Any) -> None:
    ctx = selected()
    assert isinstance(with_engineering_admission(Child(ctx.mode), Authorizer(), ctx, concrete_actions=ready),
                      EngineeringAdmissionRuntime)


async def test_existing_admission_checkpoint_cannot_be_silently_skipped() -> None:
    ctx = selected()
    child, authorizer = Child(ctx.mode), Authorizer()
    legacy = EngineeringAdmissionRuntime(child, authorizer)
    events = [event async for event in legacy.run(ctx)]
    ctx = ctx.model_copy(update={'checkpoint': events[-1].checkpoint})
    assert isinstance(with_engineering_admission(child, authorizer, ctx, concrete_actions=True),
                      EngineeringAdmissionRuntime)


def test_direct_and_read_only_are_not_selected_by_this_factory_policy() -> None:
    ctx = selected(TaskMode.DIRECT)
    assert isinstance(with_engineering_admission(Child(ctx.mode), Authorizer(), ctx, concrete_actions=True),
                      EngineeringAdmissionRuntime)
    ctx = selected(permission_mode='read_only')
    assert isinstance(with_engineering_admission(Child(ctx.mode), Authorizer(), ctx, concrete_actions=True),
                      EngineeringAdmissionRuntime)


@pytest.mark.parametrize('mode', [TaskMode.DISPATCH, TaskMode.DISCUSS, TaskMode.HYBRID])
@pytest.mark.parametrize('wiring', [
    'valid', 'capability_none', 'shared_harness_none', 'artifact_repository_none',
    'noncallable_invoke', 'pending_legacy_admission',
])
async def test_actual_configured_planning_factory_rejects_missing_concrete_wiring(
    mode: TaskMode, wiring: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    db = ApprovalProjectionSession()
    ctx = task(db, mode, 'default')
    present(db.run.routing_decision).update(engineering_action_admission_version=1,
        engineering_delivery_contract_version=1, engineering_action_version=1)
    ctx = ctx.model_copy(update={'routing_decision': db.run.routing_decision})
    # Planning does not gain a workspace delivery obligation or require an engineering ACK.
    assert 'project_delivery' not in ctx.routing_decision
    assert not _checkpoint_ack_selected(ctx)
    transport, backend = OfflineTransport([]), NoExecutionBackend()
    monkeypatch.setattr(defaults, 'CapacityPool', OfflineCapacity)
    monkeypatch.setenv('AGENT_HUB_CREWAI_STORAGE_DIR', str(tmp_path / 'crewai'))
    configured, _ = factory(mode, db, transport, backend)
    planned = await configured._runtime_for(ctx)
    assert isinstance(planned, defaults._PlannedRuntime)
    assert planned.mode is mode
    harness, artifacts = configured._harness_tool_gateway, configured._artifact_repository
    assert callable(harness.invoke) and artifacts is not None
    assert defaults._configured_concrete_actions(planned, ctx, capability_gateway=backend,
        harness_tool_gateway=harness, artifact_repository=artifacts, pending=None) is True
    pending = None
    if wiring == 'pending_legacy_admission':
        pending = RuntimeCheckpoint(id=uuid4(), runtime_type='engineering_admission', runtime_version='1',
            run_id=ctx.run_id, tenant_id=ctx.tenant_id, mode=mode, state={
                'phase': 'before_model', 'completed': False, 'approval_id': 'approval_factory_wiring',
                'tool_name': 'project.verify', 'context_sha256': _fingerprint(ctx),
            })
        assert admission_checkpoint_valid(pending)
    if wiring == 'shared_harness_none':
        harness = None
    elif wiring == 'noncallable_invoke':
        harness = SimpleNamespace(invoke=None)
    actual = defaults._configured_concrete_actions(planned, ctx,
        capability_gateway=None if wiring == 'capability_none' else backend,
        harness_tool_gateway=cast(Any, harness),
        artifact_repository=None if wiring == 'artifact_repository_none' else artifacts, pending=pending)
    assert actual is (wiring == 'valid')
    assert transport.requests == [] and backend.effects == 0 and db.approvals == {}
