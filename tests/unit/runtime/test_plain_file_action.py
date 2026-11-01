"""Fresh original worker/pool selection with real policy/backend and fixed wire."""

import asyncio
import json
from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.execution_authority import bind_execution_authority
from agent_hub.db.models import RunReviewIntentRow
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.models.admission_bounds import TrustedContextBound
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.physical_budget import _bind_run_budget
from agent_hub.models.run_admission_repository import RunAdmissionRepository
from agent_hub.models.types import Deployment
from agent_hub.runs.authorization import _capture_authenticated_input
from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind, RunEvent
from agent_hub.runtime.defaults import ConfigBackedDirectRuntime, _deployments
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.streams import closing_runtime_events
from tests.integration.runtime.test_model_review_modes_helper import (
    FixtureSecrets,
    fixture_platform,
    submitted_input_scope,
)
from tests.unit.capabilities.review_reachability_typed_fixture import PermissionMode, SandboxProfile
from tests.unit.capabilities.test_concrete_model_review import FixedLedgerReview, FocusedDB
from tests.unit.capabilities.test_model_review_reachability import full_scope
from tests.unit.runtime.post_tool_plain_action_typed_fixture import (
    NoteConfigs,
    NoteSetup,
    ReviewRepositoryForGate,
    note_setup,
    present,
    profile_for,
    string,
)
from tests.unit.runtime.test_plain_file_action_helper import (
    LocalCapacityBoundary,
    plain_note_submission,
)


def note_case(
    tmp_path: Path,
    *,
    mode: PermissionMode = "auto_review",
    preauth: bool = False,
    profile: SandboxProfile = "workspace_write",
) -> NoteSetup:
    s = note_setup(full_scope(tmp_path, mode=mode, preauthorized=preauth, profile=profile))
    body, path, content = plain_note_submission()
    body = body.model_copy(
        update={
            "permission_mode": mode,
            "project_id": "demo",
            "workspace_session_id": "session",
            "conversation_id": s.body.conversation_id,
            "sandbox_profile": s.body.sandbox_profile,
            "requested_permissions": s.body.requested_permissions,
        }
    )
    evidence = _capture_authenticated_input(
        AuthenticatedPrincipal(
            user_id=present(s.task.actor_id),
            tenant_id=s.task.tenant_id,
            role=present(s.task.actor_role),
        ),
        body.message,
        submitted_input_scope(body),
    )
    s.task = s.task.model_copy(update={"request": body.message})
    s.runs.record = replace(s.runs.record, request=body.message)
    old = s.budget
    platform = fixture_platform()
    for model in platform.models.values():
        for deployment in model.deployments:
            deployment.context_window_tokens = 10000
            deployment.max_output_tokens = 1000
    s.budget = type(old)(
        repository=old.repository,
        pool=replace(old._pool, authorization_turn_id=evidence.turn_id),
        evidence=evidence,
        lease=old.lease,
        config=old.config,
        deadline=old.deadline,
        trusted_contexts=tuple(
            TrustedContextBound(d.id, d.logical_model, d.provider_model, 10000)
            for d in _deployments(platform)
        ),
    )
    s.runs.evidence = evidence
    s.db = FocusedDB(s)
    s.budget.repository = RunAdmissionRepository(s.db)
    ledger = ReviewRepositoryForGate(s.db)
    s.ledger = ledger
    s.fixed = FixedLedgerReview(s.ledger)
    s.gate.repository, s.gate.service = ledger, s.fixed
    s.body, s.path, s.content = body, path, content
    s.wire = []

    async def capacity(tenant: UUID, deployments: Sequence[Deployment]) -> LocalCapacityBoundary:
        return LocalCapacityBoundary()

    async def wire(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        s.wire.append(payload)
        text = json.dumps(
            {
                "workspace_batch": {"files": {path: content}, "complete": True, "continuation": ""},
                "summary": f"Saved {path}.",
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "note",
                "object": "chat.completion",
                "created": 1,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
        )

    s.runtime = ConfigBackedDirectRuntime(
        config_service=NoteConfigs(platform),
        secret_service=FixtureSecrets(),
        capacity_factory=capacity,
        transport=LiteLLMClient(_network_delegate_factory=lambda: httpx.MockTransport(wire)),
        artifact_repository=InMemoryArtifactRepository(),
        capability_gateway=s.backend,
        harness_tool_gateway=s.harness,
        action_capability_gateway=s.backend,
        action_harness_tool_gateway=s.harness,
    )
    return s


@pytest.mark.parametrize("mode", ["default", "auto_review"])
async def test_actual_bound_pending_note_selects_existing_action_runtime(
    tmp_path: Path, mode: PermissionMode
) -> None:
    s = note_case(tmp_path, mode=mode)
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        runtime = await s.runtime._runtime_for(s.task)
    assert type(runtime) is ActionApprovedDirectRuntime
    assert s.fixed.calls == 0 and s.wire == [] and s.calls == []


async def test_validated_frozen_context_selects_pending_note_runtime(tmp_path: Path) -> None:
    s = note_case(tmp_path)
    context = s.task.validated_internal_clone()
    assert isinstance(context.routing_decision["requested_permissions"], tuple)
    with (
        bind_execution_authority(context, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        assert type(await s.runtime._runtime_for(context)) is ActionApprovedDirectRuntime


@pytest.mark.parametrize(
    "barrier",
    [
        "worker",
        "budget",
        "role",
        "token",
        "profile",
        "chat",
        "preauth",
        "read_only",
        "restricted",
        "none",
        "pool",
        "auth",
    ],
)
async def test_incomplete_or_ordinary_scope_keeps_direct_runtime(
    tmp_path: Path, barrier: str
) -> None:
    s = note_case(
        tmp_path,
        preauth=barrier == "preauth",
        profile=profile_for(barrier),
    )
    context = s.task
    if barrier == "role":
        context = context.model_copy(update={"actor_role": Role.ADMIN})
    elif barrier == "token":
        context = context.model_copy(update={"execution_token": uuid4()})
    elif barrier == "profile":
        route = dict(context.routing_decision)
        route.pop("approval_review_profile")
        context = context.model_copy(update={"routing_decision": route})
    elif barrier == "chat":
        context = context.model_copy(update={"request": "Compare two bounded observations."})
    elif barrier == "pool":
        s.db.pool.closed = True
    elif barrier == "auth":
        s.runs.evidence = replace(s.runs.evidence, turn_id=uuid4())
    with ExitStack() as stack:
        if barrier != "worker":
            stack.enter_context(
                bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id)
            )
        if barrier != "budget":
            stack.enter_context(_bind_run_budget(s.budget))
        assert type(await s.runtime._runtime_for(context)) is DirectRuntime
    assert s.fixed.calls == 0 and not s.wire and not s.calls


async def test_pending_auto_actual_runtime_writes_note_and_finishes(tmp_path: Path) -> None:
    s = note_case(tmp_path)
    events: list[RunEvent] = []
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        async with closing_runtime_events(s.runtime.run(s.task)) as stream:
            async for event in stream:
                events.append(event)
    root = ProjectWorkspaceStore(tmp_path).session_root(s.task.tenant_id, "demo", "session")
    assert (root / s.path).read_bytes() == s.content.encode()
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert any(
        event.artifact is not None
        and event.artifact.type == "text"
        and s.path in string(event.artifact.content["text"])
        for event in events
    )
    assert len(s.wire) == s.fixed.calls == len(s.calls) == 1
    assert [row.consumed for row in s.db.rows if type(row) is RunReviewIntentRow] == [True]
    assert all(call["name"] == "workspace.write_text" for call in s.calls)


async def test_default_pending_note_waits_and_resumes_only_with_exact_human(tmp_path: Path) -> None:
    s = note_case(tmp_path, mode="default")
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        async with closing_runtime_events(s.runtime.run(s.task)) as stream:
            pending = [event async for event in stream]
        assert pending[-1].kind is EventKind.CHECKPOINT_SAVED
        checkpoint = present(pending[-1].checkpoint)
        assert checkpoint.runtime_type == "direct_action"
        assert checkpoint.state["tool_name"] == "workspace.write_text"
        assert len(s.runs.waits) == 1 and not s.calls and s.fixed.calls == 0
        s.runs.exact = True
        await s.runtime.restore_checkpoint(checkpoint)
        current = s.task.model_copy(update={"checkpoint": checkpoint})
        async with closing_runtime_events(s.runtime.run(current)) as stream:
            resumed = [event async for event in stream]
    assert resumed[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(s.calls) == len(s.wire) == 1 and s.fixed.calls == 0
    root = ProjectWorkspaceStore(tmp_path).session_root(s.task.tenant_id, "demo", "session")
    assert (root / s.path).read_bytes() == s.content.encode()


async def test_selected_plain_runtime_cannot_run_after_worker_scope_exit(tmp_path: Path) -> None:
    from agent_hub.runtime.direct import RuntimeExecutionError

    s = note_case(tmp_path)
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        selected = await s.runtime._runtime_for(s.task)
    with pytest.raises(RuntimeExecutionError):
        async with closing_runtime_events(selected.run(s.task)) as stream:
            _ = [event async for event in stream]
    assert not s.wire and not s.calls and s.fixed.calls == 0


async def test_plain_note_cost_checkpoint_restores_through_fresh_factory(tmp_path: Path) -> None:
    s = note_case(tmp_path)
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        async with closing_runtime_events(s.runtime.run(s.task)) as stream:
            events = [event async for event in stream]
        assert any(event.kind is EventKind.COST_RECORDED for event in events)
        checkpoint_event = next(
            event for event in events if event.kind is EventKind.CHECKPOINT_SAVED
        )
        checkpoint = present(checkpoint_event.checkpoint)
        assert checkpoint.state["next_sequence"] == checkpoint_event.sequence + 1
        assert events[-1].sequence == checkpoint.state["next_sequence"]
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        current = s.task.model_copy(
            update={
                "checkpoint": checkpoint,
                "artifacts": tuple(
                    event.artifact for event in events if event.artifact is not None
                ),
            }
        )
        # Simulate delivery after checkpoint persistence in a new actual factory/target.
        restored = ConfigBackedDirectRuntime(
            config_service=s.runtime._config_service,
            secret_service=s.runtime._secret_service,
            capacity_factory=s.runtime._capacity_factory,
            transport=s.runtime._transport,
            artifact_repository=s.runtime._artifact_repository,
            capability_gateway=s.backend,
            harness_tool_gateway=s.harness,
            action_capability_gateway=s.backend,
            action_harness_tool_gateway=s.harness,
        )
        assert type(await restored._runtime_for(current)) is DirectRuntime
        await restored.restore_checkpoint(checkpoint)
        async with closing_runtime_events(restored.run(current)) as stream:
            resumed = [event async for event in stream]
    assert [(event.kind, event.sequence) for event in resumed] == [
        (EventKind.RUNTIME_COMPLETED, checkpoint.state["next_sequence"])
    ]
    root = ProjectWorkspaceStore(tmp_path).session_root(s.task.tenant_id, "demo", "session")
    assert (root / s.path).read_bytes() == s.content.encode()
    assert len(s.wire) == s.fixed.calls == len(s.calls) == 1
    assert [row.consumed for row in s.db.rows if type(row) is RunReviewIntentRow] == [True]


async def test_cancel_during_plain_note_budget_check_sends_no_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = note_case(tmp_path)
    with (
        bind_execution_authority(s.task, worker_id=s.budget.lease.worker_id),
        _bind_run_budget(s.budget),
    ):
        selected = await s.runtime._runtime_for(s.task)
        assert isinstance(selected, ActionApprovedDirectRuntime)
        entered, release = asyncio.Event(), asyncio.Event()
        actual = s.budget.require_current

        async def pause_after_current() -> None:
            await actual()
            entered.set()
            await release.wait()

        monkeypatch.setattr(s.budget, "require_current", pause_after_current)
        events: list[RunEvent] = []

        async def drain() -> None:
            async with closing_runtime_events(selected.run(s.task)) as stream:
                async for event in stream:
                    events.append(event)

        runner = asyncio.create_task(drain())
        canceller = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            assert [event.kind for event in events] == [EventKind.MODEL_STARTED]
            assert not s.wire and not s.calls
            canceller = asyncio.create_task(selected.cancel())
            # cancel() sets the flag before waiting for the paused producer to drain.
            await asyncio.sleep(0)
            assert selected._cancel_requested
            release.set()
            outcomes = await asyncio.wait_for(
                asyncio.gather(runner, canceller, return_exceptions=True), timeout=10
            )
        finally:
            release.set()
            if not runner.done():
                runner.cancel()
            await asyncio.gather(
                *(task for task in (runner, canceller) if task is not None), return_exceptions=True
            )
    assert isinstance(outcomes[0], asyncio.CancelledError) and outcomes[1] is None
    assert not s.wire and not s.calls and s.fixed.calls == 0
    assert not any(type(row) is RunReviewIntentRow for row in s.db.rows)
    root = ProjectWorkspaceStore(tmp_path).session_root(s.task.tenant_id, "demo", "session")
    assert not (root / s.path).exists()
