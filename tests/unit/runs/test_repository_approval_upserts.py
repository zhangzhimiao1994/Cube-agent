from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from agent_hub.auth.models import Role
from agent_hub.capabilities.approvals import ApprovalService, InMemoryApprovalStore
from agent_hub.capabilities.defaults import DefaultRuntimeCapabilityPolicyGateway
from agent_hub.capabilities.gateway import CapabilityGateway
from agent_hub.capabilities.policy import CapabilityPolicy, CapabilityRule
from agent_hub.capabilities.types import CapabilityRequest, PolicyEffect
from agent_hub.db.models import RunApprovalRow, RunEventRow, RunRow
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.plugins.runtime import RuntimePluginService
from agent_hub.runs.repository import RunConflict, RunNotFound, RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import EventKind, RunEvent, RuntimeCheckpoint
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.plugins.test_runtime import FakeAdminService, plugin
from tests.unit.runs.test_terminal_hooks import RuntimeCompletes

_script_path = Path(__file__).resolve().parents[3] / "scripts/real_user_plugin_acceptance.py"
_script_spec = importlib.util.spec_from_file_location(
    "_repository_approval_plugin_acceptance", _script_path,
)
assert _script_spec is not None and _script_spec.loader is not None
_script_module = importlib.util.module_from_spec(_script_spec)
sys.modules[_script_spec.name] = _script_module
_script_spec.loader.exec_module(_script_module)
FrozenPluginApproval = _script_module.FrozenPluginApproval
_verify_plugin_approval = _script_module._verify_plugin_approval


class RecordingSession:
    def __init__(self) -> None:
        self.statement: Any | None = None

    async def execute(self, statement: Any) -> Any:
        from types import SimpleNamespace

        self.statement = statement
        return SimpleNamespace(scalar_one_or_none=uuid4)


def _compiled_upsert_statement(session: RecordingSession) -> str:
    assert session.statement is not None
    return str(session.statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


@pytest.mark.asyncio
async def test_capability_approval_resolution_updates_only_existing_matching_request() -> None:
    session = RecordingSession()

    await RunRepository._upsert_capability_approval(
        session,  # type: ignore[arg-type]
        uuid4(),
        uuid4(),
        approval_id="approval-1",
        approval_fingerprint="fingerprint-1",
        status="approved",
    )

    statement = _compiled_upsert_statement(session)
    assert statement.startswith("UPDATE agent_hub_run_approvals SET action=")
    assert "agent_hub_run_approvals.tenant_id =" in statement
    assert "agent_hub_run_approvals.run_id =" in statement
    assert "agent_hub_run_approvals.approval_id =" in statement
    assert session.statement is not None
    assert "||" in statement and "approval_fingerprint" in str(session.statement.compile().params)
    updates = [value for value in session.statement.compile().params.values()
               if isinstance(value, dict)]
    assert updates == [{"status": "approved"}]


class ApprovalProjectionSession:
    """Replace only SQL I/O; exercise production gateway, inserts and public projection."""

    def __init__(self) -> None:
        self.run = RunRow(
            id=uuid4(), tenant_id=uuid4(), actor_id=uuid4(), request="plugin approval",
            mode=TaskMode.DISPATCH.value, status=RunStatus.RUNNING.value, version=1,
            created_at=datetime.now(UTC), routing_decision={},
        )
        self.approvals: dict[str, RunApprovalRow] = {}
        self.events: list[RunEventRow] = []
        self.outbox: list[object] = []

    def __call__(self) -> ApprovalProjectionSession:
        return self

    async def __aenter__(self) -> Self:
        self._saved_state = {key: deepcopy(getattr(self.run, key)) for key in (
            "status", "version", "routing_decision", "worker_id", "worker_lease_token",
            "worker_lease_expires_at", "worker_heartbeat_at",
        )}
        return self

    async def __aexit__(self, error_type: object, *_args: object) -> None:
        if error_type is not None:
            for key, value in self._saved_state.items():
                setattr(self.run, key, value)

    def begin(self) -> ApprovalProjectionSession:
        return self

    async def scalar(self, statement: Any) -> object:
        params = statement.compile().params
        if str(statement).startswith("SELECT max("):
            assert self.run.id in params.values()
            return None
        if "agent_hub_runs" in str(statement):
            if self.run.tenant_id not in params.values() or self.run.id not in params.values():
                return None
            return self.run
        raise AssertionError(f"unexpected SQL scalar: {statement}")

    async def scalars(self, statement: Any) -> Any:
        from types import SimpleNamespace

        params = statement.compile().params
        assert self.run.tenant_id in params.values() and self.run.id in params.values()
        rows = (list(self.approvals.values()) if "agent_hub_run_approvals" in str(statement)
                else self.events)
        return SimpleNamespace(all=lambda: rows)

    async def execute(self, statement: Any) -> Any:
        from types import SimpleNamespace

        params = statement.compile().params
        assert statement.table.name == "agent_hub_run_approvals"
        if str(statement).startswith("UPDATE"):
            approval = self.approvals.get(params["approval_id_1"])
            if approval is None or approval.status != "pending" or (
                approval.payload.get("approval_fingerprint") not in params.values()
            ):
                return SimpleNamespace(scalar_one_or_none=lambda: None)
            delta = next(value for value in params.values() if isinstance(value, dict))
            approval.payload = approval.payload | delta
            approval.status = params["status"]
            approval.action = params["action"]
            return SimpleNamespace(scalar_one_or_none=uuid4)
        approval_id = params["approval_id"]
        payload = params["payload"]
        if approval_id in self.approvals:
            if "DO NOTHING" in str(statement):
                return SimpleNamespace(scalar_one_or_none=lambda: None)
            payload = self.approvals[approval_id].payload | payload
        self.approvals[approval_id] = RunApprovalRow(
            id=params["id"], tenant_id=params["tenant_id"], run_id=params["run_id"],
            approval_id=approval_id, action=params["action"], status=params["status"],
            payload=payload, created_at=datetime.now(UTC),
        )
        return SimpleNamespace(scalar_one_or_none=uuid4)

    async def flush(self) -> None:
        pass

    def add(self, row: object) -> None:
        self.outbox.append(row)


async def _actual_plugin_wait(
    session: ApprovalProjectionSession, *, permission_class: str = "calendar.write",
) -> tuple[RunRepository, str]:
    runtime = RuntimePluginService(
        tenant_id=session.run.tenant_id,
        admin_service=FakeAdminService((plugin(
            "trusted-plugin", capability_id="calendar.create_event",
            permission_class=permission_class, policy_effect="require_approval",
        ),)),
    )
    await runtime.start()
    repository = RunRepository(session)  # type: ignore[arg-type]
    gateway = DefaultRuntimeCapabilityPolicyGateway(
        ApprovalService(InMemoryApprovalStore()), repository, plugin_policy_rules=runtime,
    )
    parts = runtime.capability_policy_parts(session.run.tenant_id, "calendar_create")
    assert parts is not None
    capability, operation, resource = parts
    actor_id = session.run.actor_id
    assert isinstance(actor_id, UUID)
    result = await gateway.invoke(CapabilityRequest(
        tenant_id=session.run.tenant_id, user_id=actor_id, run_id=session.run.id,
        agent_id="implementer", capability=capability, operation=operation, resource=resource,
        arguments={"plugin_id": "forged-plugin", "capability_id": "forged.capability",
                   "source": "PRIVATE-SOURCE", "token": "PRIVATE-TOKEN"},
        idempotency_key="real-plugin-wait",
    ), role=Role.OPERATOR)
    assert result.approval_id is not None
    return repository, result.approval_id


@pytest.mark.parametrize("checkpoint_kind", ["missing", "stale", "matching"])
async def test_public_readiness_uses_the_durable_checkpoint_gate(
    monkeypatch: pytest.MonkeyPatch, checkpoint_kind: str,
) -> None:
    session = ApprovalProjectionSession()
    session.run.worker_lease_token = uuid4()
    repository, approval_id = await _actual_plugin_wait(session)
    actor_id = session.run.actor_id
    assert isinstance(actor_id, UUID)

    async def checkpoint(*_args: object, **_kwargs: object) -> RuntimeCheckpoint | None:
        if checkpoint_kind == "missing":
            return None
        return RuntimeCheckpoint(
            id=uuid4(), runtime_type="crew", runtime_version="11",
            run_id=session.run.id, tenant_id=session.run.tenant_id, mode=TaskMode.DISPATCH,
            state={"tools": {"tool-key": {
                "status": "waiting_approval",
                "approval_id": approval_id if checkpoint_kind == "matching" else "stale",
            }}},
        )

    monkeypatch.setattr(repository, "latest_checkpoint", checkpoint)
    assert await repository.capability_approval_ready(
        session.run.tenant_id, session.run.id, actor_id=actor_id,
    ) is (checkpoint_kind == "matching")
    with pytest.raises(RunNotFound):
        await repository.capability_approval_ready(
            session.run.tenant_id, session.run.id, actor_id=uuid4(),
        )
    session.run.status = RunStatus.CANCELLED.value
    assert await repository.capability_approval_ready(
        session.run.tenant_id, session.run.id, actor_id=actor_id,
    ) is False


@pytest.mark.parametrize("permission_class", ["calendar.write", "plugin.use"])
async def test_actual_plugin_wait_supplies_public_binding_to_acceptance_guard(
    permission_class: str,
) -> None:
    session = ApprovalProjectionSession()
    repository, approval_id = await _actual_plugin_wait(session, permission_class=permission_class)
    # No fabricated approval event: Crew only emits tool.failed at this boundary.
    assert session.events == []
    events = await repository.capability_approval_events(session.run.tenant_id, session.run.id)
    _verify_plugin_approval(
        {"items": events, "ready": True}, run_id=str(session.run.id), approval_id=approval_id,
        version=session.run.version, plugin_id="trusted-plugin",
        capability_id="calendar.create_event",
    )
    payload = events[0]["payload"]
    assert payload == {"plugin_id": "trusted-plugin", "capability_id": "calendar.create_event",
                       "run_id": str(session.run.id), "version": 2}
    assert "PRIVATE" not in str(events) and "forged" not in str(events)
    assert "approval_fingerprint" not in str(events)
    assert session.approvals[approval_id].payload["public_binding"] == payload


@pytest.mark.parametrize("mutation", ["version", "approval_id", "fingerprint", "status"])
async def test_stale_pending_plugin_projection_cannot_authorize_acceptance(mutation: str) -> None:
    session = ApprovalProjectionSession()
    repository, approval_id = await _actual_plugin_wait(session)
    if mutation == "version":
        session.run.version += 1
    elif mutation == "status":
        session.run.status = RunStatus.RUNNING.value
    else:
        key = "approval_id" if mutation == "approval_id" else "approval_fingerprint"
        routing = session.run.routing_decision
        assert isinstance(routing, dict)
        routing[key] = "changed"
    with pytest.raises((RunConflict, FrozenPluginApproval)):
        events = await repository.capability_approval_events(session.run.tenant_id, session.run.id)
        _verify_plugin_approval(
            {"items": events, "ready": True}, run_id=str(session.run.id), approval_id=approval_id,
            version=session.run.version, plugin_id="trusted-plugin",
            capability_id="calendar.create_event",
        )


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.parametrize("mutation", ["actor", "tenant", "run", "version", "approval_id"])
async def test_public_plugin_resolution_rejects_foreign_or_stale_binding(
    action: str, mutation: str,
) -> None:
    session = ApprovalProjectionSession()
    repository, approval_id = await _actual_plugin_wait(session)
    args: dict[str, Any] = {
        "tenant_id": session.run.tenant_id, "run_id": session.run.id,
        "actor_id": session.run.actor_id, "approval_id": approval_id,
        "version": session.run.version,
    }
    key = {"actor": "actor_id", "tenant": "tenant_id", "run": "run_id"}.get(mutation, mutation)
    args[key] = args[key] - 1 if mutation == "version" else (
        "foreign-approval" if mutation == "approval_id" else uuid4()
    )
    service = RunService(repository, runtime_registry=RuntimeRegistry((RuntimeCompletes(),)), router=None,
                         task_queue=None)  # type: ignore[arg-type]
    method = service.approve_capability if action == "approve" else service.reject_capability
    with pytest.raises((RunNotFound, RunConflict)):
        await method(**args)
    assert session.run.status == RunStatus.WAITING_APPROVAL.value
    assert session.outbox == [] and session.approvals[approval_id].status == "pending"


async def test_public_plugin_approval_rejects_other_actor_event_read() -> None:
    session = ApprovalProjectionSession()
    repository, _ = await _actual_plugin_wait(session)
    with pytest.raises(RunNotFound):
        await repository.capability_approval_events(
            session.run.tenant_id, session.run.id, actor_id=uuid4(),
        )


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_public_plugin_resolution_preserves_bound_history(action: str) -> None:
    session = ApprovalProjectionSession()
    repository, approval_id = await _actual_plugin_wait(session)
    method = (repository.approve_capability_and_enqueue if action == "approve"
              else repository.reject_capability_approval)
    await method(tenant_id=session.run.tenant_id, run_id=session.run.id,
                 actor_id=session.run.actor_id, approval_id=approval_id, version=2)
    events = await repository.capability_approval_events(session.run.tenant_id, session.run.id)
    assert [event["kind"] for event in events] == ["approval.requested", "approval.resolved"]
    requested_payload = events[0]["payload"]
    assert isinstance(requested_payload, dict)
    assert requested_payload["version"] == 2
    assert events[1]["approval_id"] == approval_id
    requested_sequence = events[0]["sequence"]
    resolved_sequence = events[1]["sequence"]
    assert type(requested_sequence) is int and type(resolved_sequence) is int
    assert resolved_sequence > requested_sequence


async def test_runtime_approval_projection_cannot_replace_transaction_binding() -> None:
    session = RecordingSession()
    run_id = uuid4()
    await RunRepository._persist_approval(
        session, uuid4(), run_id,  # type: ignore[arg-type]
        RunEvent(kind=EventKind.APPROVAL_RESOLVED, sequence=5, run_id=run_id,
                 actor="implementer", approval_id="approval-current", decision="approved"),
    )
    sql = _compiled_upsert_statement(session)
    assert session.statement is not None
    assert "WHERE NOT" in sql and "public_request" in str(session.statement.compile().params)


async def test_plugin_wait_without_catalog_still_requires_run_actor() -> None:
    session = ApprovalProjectionSession()
    repository = RunRepository(session)  # type: ignore[arg-type]
    gateway = CapabilityGateway(CapabilityPolicy((CapabilityRule(
        tenant_id=session.run.tenant_id, role=Role.OPERATOR, agent_id=None,
        capability="plugin", operation="use", resource_prefix="plugin/",
        effect=PolicyEffect.REQUIRE_APPROVAL,
    ),)), ApprovalService(InMemoryApprovalStore()), repository)
    with pytest.raises(RunNotFound):
        await gateway.invoke(CapabilityRequest(
            tenant_id=session.run.tenant_id, run_id=session.run.id, user_id=uuid4(),
            agent_id="implementer", capability="plugin", operation="use",
            resource="plugin/calendar/create_event", idempotency_key="foreign-actor",
        ), role=Role.OPERATOR)
    assert session.run.status == RunStatus.RUNNING.value and session.approvals == {}


async def test_pending_approval_sequence_does_not_move_when_worker_events_arrive() -> None:
    session = ApprovalProjectionSession()
    repository, approval_id = await _actual_plugin_wait(session)
    before = await repository.capability_approval_events(session.run.tenant_id, session.run.id)
    session.events.append(RunEventRow(
        id=uuid4(), tenant_id=session.run.tenant_id, run_id=session.run.id, sequence=17,
        kind="custom.progress", created_at=datetime.now(UTC),
        payload={"kind": "custom.progress", "sequence": 17,
                 "run_id": str(session.run.id), "payload": {"ok": True}},
    ))
    after = await repository.capability_approval_events(session.run.tenant_id, session.run.id)
    assert next(event["sequence"] for event in before if event.get("approval_id") == approval_id) == (
        next(event["sequence"] for event in after if event.get("approval_id") == approval_id)
    )
    raw_events = await repository.events(session.run.tenant_id, session.run.id)
    assert len(raw_events) == 1 and raw_events[0]["sequence"] == 17
    assert after == before


async def test_runtime_approval_history_stays_in_event_stream_without_becoming_consent() -> None:
    session = ApprovalProjectionSession()
    repository, approval_id = await _actual_plugin_wait(session)
    raw = RunEvent(kind=EventKind.APPROVAL_REQUESTED, sequence=17, run_id=session.run.id,
                   actor="implementer", approval_id=approval_id, action="capability_tool",
                   reason="runtime requested", payload={"note": "runtime history"})
    session.events.append(RunEventRow(
        id=uuid4(), tenant_id=session.run.tenant_id, run_id=session.run.id, sequence=17,
        kind=str(raw.kind), payload=raw.to_payload(), created_at=datetime.now(UTC),
    ))
    events = await repository.events(session.run.tenant_id, session.run.id)
    assert len(events) == 1 and events[0]["sequence"] == 17
    assert events[0]["payload"] == {"note": "runtime history"}
    with pytest.raises(FrozenPluginApproval):
        _verify_plugin_approval({"items": events, "ready": True}, run_id=str(session.run.id), approval_id=approval_id,
                                version=2, plugin_id="trusted-plugin",
                                capability_id="calendar.create_event")


async def test_reused_approval_id_rolls_back_without_rebinding_public_request() -> None:
    from agent_hub.capabilities.approvals import PluginApprovalIdentity

    session = ApprovalProjectionSession()
    repository, approval_id = await _actual_plugin_wait(session)
    await repository.reject_capability_approval(
        tenant_id=session.run.tenant_id, run_id=session.run.id, actor_id=session.run.actor_id,
        approval_id=approval_id, version=2,
    )
    before = deepcopy(session.approvals[approval_id].payload)
    session.run.status = RunStatus.RUNNING.value
    version = session.run.version
    with pytest.raises(RunConflict):
        await repository.begin_capability_approval(
            session.run.tenant_id, session.run.id, actor_id=session.run.actor_id,
            approval_id=approval_id, approval_fingerprint="replacement-fingerprint",
            plugin_identity=PluginApprovalIdentity("replacement-plugin", "replacement.capability"),
        )
    assert session.approvals[approval_id].payload == before
    assert session.run.status == RunStatus.RUNNING.value and session.run.version == version


async def test_review_upsert_cannot_downgrade_persisted_request() -> None:
    session = RecordingSession()
    await RunRepository._upsert_capability_approval_review(
        session, uuid4(), uuid4(),  # type: ignore[arg-type]
        approval_id="approval-current", approval_fingerprint="fingerprint",
        reviewer="auto_review", reason="safe", status="approved",
    )
    assert "WHERE NOT" in _compiled_upsert_statement(session)
    assert session.statement is not None
    assert "public_request" in str(session.statement.compile().params)


async def test_resolution_cannot_create_missing_request_evidence() -> None:
    session = ApprovalProjectionSession()
    repository, approval_id = await _actual_plugin_wait(session)
    session.approvals.clear()
    with pytest.raises(RunConflict):
        await repository.reject_capability_approval(
            tenant_id=session.run.tenant_id, run_id=session.run.id, actor_id=session.run.actor_id,
            approval_id=approval_id, version=2,
        )
    assert session.run.status == RunStatus.WAITING_APPROVAL.value and session.run.version == 2
    assert session.approvals == {}


def test_plugin_approval_identity_matches_catalog_capability_id_grammar() -> None:
    from agent_hub.capabilities.approvals import PluginApprovalIdentity

    assert PluginApprovalIdentity("trusted-plugin", "1calendar.create").capability_id == "1calendar.create"


@pytest.mark.asyncio
async def test_capability_approval_review_upsert_refreshes_row_action_on_conflict() -> None:
    session = RecordingSession()

    await RunRepository._upsert_capability_approval_review(
        session,  # type: ignore[arg-type]
        uuid4(),
        uuid4(),
        approval_id="approval-1",
        approval_fingerprint="fingerprint-1",
        reviewer="auto_policy",
        reason="safe",
        status="approved",
    )

    statement = _compiled_upsert_statement(session)
    assert "ON CONFLICT (run_id, approval_id) DO UPDATE" in statement
    assert "SET action = " in statement


@pytest.mark.asyncio
async def test_runtime_approval_event_upsert_refreshes_row_action_on_conflict() -> None:
    session = RecordingSession()
    run_id = uuid4()

    await RunRepository._persist_approval(
        session,  # type: ignore[arg-type]
        uuid4(),
        run_id,
        RunEvent(
            kind=EventKind.APPROVAL_RESOLVED,
            sequence=2,
            run_id=run_id,
            actor="operator",
            approval_id="approval-1",
            decision="approved",
        ),
    )

    statement = _compiled_upsert_statement(session)
    assert "ON CONFLICT (run_id, approval_id) DO UPDATE" in statement
    assert "SET action = " in statement
