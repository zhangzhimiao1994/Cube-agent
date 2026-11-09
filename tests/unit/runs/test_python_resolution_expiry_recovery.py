"""A worker retires only proven, quiescent, expired original-project approvals."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.domain.runs import TaskMode
from agent_hub.runs.repository import RunRepository, _public_plugin_approval_events
from agent_hub.runtime.contracts import EventKind
from tests.unit.runs.test_python_resolution_public_projection import object_map, pending


@pytest.mark.parametrize("case_kind", ["expired", "current", "worker", "legacy", "changed_binding"])
async def test_periodic_recovery_ends_only_expired_original_resolution(
    monkeypatch: pytest.MonkeyPatch, case_kind: str,
) -> None:
    case = pending(native=False, mode=TaskMode.DISCUSS)
    assert case.run.routing_decision is not None
    case.run.routing_decision["approval_checkpoint_required"] = False
    now = datetime.now(UTC)
    case.run.created_at = now - timedelta(minutes=10)
    deadline = now + timedelta(seconds=1 if case_kind == "current" else -1)
    binding = object_map(case.approval.payload["python_resolution_binding"])
    if case_kind != "legacy":
        binding["schema_version"] = 2
        binding["arguments"] = {"original_deadline": deadline.isoformat()}
    if case_kind == "worker":
        case.run.worker_id = "worker-with-unfinished-cleanup"
    persist = AsyncMock()
    monkeypatch.setattr(
        "agent_hub.capabilities.python_resolution_authorization.resolution_binding_matches",
        lambda *_: case_kind != "changed_binding",
    )
    monkeypatch.setattr(AsyncSession, "execute", AsyncMock(return_value=SimpleNamespace(
        tuples=lambda: SimpleNamespace(all=lambda: [(case.run, deadline.isoformat())]),
    )))
    monkeypatch.setattr(AsyncSession, "scalar", AsyncMock(return_value=case.approval))
    monkeypatch.setattr(AsyncSession, "flush", AsyncMock())
    monkeypatch.setattr(RunRepository, "next_event_sequence", AsyncMock(return_value=3))
    monkeypatch.setattr(RunRepository, "persist_event", persist)
    repository = RunRepository(async_sessionmaker(expire_on_commit=False))

    count = await repository.expire_original_python_resolution_approvals(limit=10, now=now)

    if case_kind == "expired":
        assert count == 1
        assert case.run.status == "failed" and case.run.version == 5
        assert case.approval.status == "expired"
        assert case.run.routing_decision is not None
        assert "approval_id" not in case.run.routing_decision
        assert case.approval.payload["python_resolution_binding"] == binding
        assert persist.await_args is not None
        event = persist.await_args.kwargs["event"]
        assert event.kind is EventKind.RUNTIME_FAILED
        assert event.payload["error_code"] == "runtime.python_dependency_approval_expired"
        history = _public_plugin_approval_events([case.approval], case.run)
        assert [item["kind"] for item in history] == ["approval.requested", "approval.resolved"]
        assert history[-1]["created_at"] == now and "decision" not in history[-1]
        assert history[-1]["payload"] == {"approval_expired": True}
        assert "PRIVATE_" not in str(history)
    else:
        assert count == 0
        assert case.run.status == "waiting_approval" and case.run.version == 4
        assert case.approval.status == "pending"
        persist.assert_not_awaited()
