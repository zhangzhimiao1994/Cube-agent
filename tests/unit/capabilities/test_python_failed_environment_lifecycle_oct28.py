"""Prospective FAILED cleanup: real readonly stores, fake locked DB/release transport.

No native project, service, network, cache deletion, or paid model is executed.
"""
from __future__ import annotations

import copy
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from uuid import UUID

import pytest

from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.capabilities.test_python_generated_lifecycle import generated_session, hook_for
from tests.unit.capabilities.test_python_project_lifecycle import (
    Rows,
    Session,
    cancelled_model_checkpoint,
    invoke,
)
from tests.unit.skills.sandbox.test_python_project_client import invocation, reference

POLICY = "terminal_failed_python_environment_cleanup_version"
RELEASE = "python_project_cache_release"


def failed_session(*, generated: bool = False) -> Session:
    session = generated_session() if generated else Session()
    session.row.status = "failed"
    session.row.error_summary = "existing_failure"
    session.row.routing_decision[POLICY] = 1
    return session


@pytest.mark.parametrize("generated", [False, True])
@pytest.mark.parametrize("model_history", [False, True])
async def test_opted_in_failed_preserves_failure_history_and_scope_under_db_lock(
    generated: bool, model_history: bool,
) -> None:
    session = failed_session(generated=generated)
    if model_history:
        session.checkpoints = [cancelled_model_checkpoint()]
    route, checkpoint = copy.deepcopy(session.row.routing_decision), copy.deepcopy(session.checkpoints)
    if generated:
        hook, calls = hook_for(session)
        await hook(tenant_id=session.row.tenant_id, run_id=session.row.id, status=RunStatus.FAILED,
                   actor_id=None, mode=TaskMode.HYBRID, routing_decision={})
        assert len(calls) == 2 and calls[0] == "generated-context"
    else:
        assert await invoke(session, callback_status=RunStatus.FAILED) == ["context", "release"]
    assert session.queries[0]._for_update_arg is not None
    assert session.row.status == "failed" and session.row.error_summary == "existing_failure"
    assert session.row.routing_decision == route | {
        RELEASE: {"schema_version": 1, "environment_reference": reference().model_dump(mode="json")}}
    assert session.checkpoints == checkpoint and session.row.version == 2


@pytest.mark.parametrize("version", [None, True, False, "1", 1.0, 0, 2, -1, {}, []])
@pytest.mark.parametrize("generated", [False, True])
async def test_failed_cleanup_requires_persisted_strict_int_one(version: object, generated: bool) -> None:
    session = failed_session(generated=generated)
    if version is None:
        session.row.routing_decision.pop(POLICY)
    else:
        session.row.routing_decision[POLICY] = version
    if generated:
        hook, calls = hook_for(session)
        await hook(tenant_id=session.row.tenant_id, run_id=session.row.id, status=RunStatus.FAILED,
                   actor_id=None, mode=None, routing_decision={POLICY: 1})
        assert calls == []
    else:
        assert await invoke(session, callback_status=RunStatus.FAILED) == []
    assert RELEASE not in session.row.routing_decision and session.row.version == 1


@pytest.mark.parametrize("run", [
    "89d8bbe0-4706-4b5a-be35-5e30fc9088c1", "4c80a763-c341-4edb-9ca6-99c58a23f080",
    "76258254-f7d6-4f81-a1e7-4fa6f5369225", "6c895bf3-854a-43ab-aaa3-f096db7dca33",
    "6bf67c88-638b-4916-bb54-2a732ee182ff",
])
async def test_historical_failed_ids_without_opt_in_never_read_context_or_release(run: str) -> None:
    session = failed_session()
    session.row.id = UUID(run)
    session.row.routing_decision.pop(POLICY)
    before = copy.deepcopy(vars(session.row))

    class Repository:
        async def run_transaction(self) -> Any:
            return session

    class Forbidden:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError("legacy scope must not reach readonly context or RPC")

    await PythonProjectTerminalHook(Repository(), Forbidden(), Forbidden())(
        tenant_id=session.row.tenant_id, run_id=session.row.id, status=RunStatus.FAILED,
        actor_id=None, mode=None, routing_decision={POLICY: 1})
    assert vars(session.row) == before


@pytest.mark.parametrize("generated", [False, True])
@pytest.mark.parametrize("defect", [
    "worker_id", "worker_lease_token", "worker_lease_expires_at", "worker_heartbeat_at",
    "approval", "route-unknown", "missing-checkpoint", "malformed-checkpoint", "checkpoint-unknown",
    "tool-running", "tool-unknown", "tool-partial", "tool-prepared", "tool-malformed",
    "model-unknown", "model-partial", "model-prepared", "model-wrong-run", "model-request-sha",
    "model-attempt-bool", "model-status-bool", "model-extra", "model-empty", "nested-running",
    "phase-unknown", "self-repair", "repair-lineage", "wrong-project", "unobserved",
])
async def test_failed_policy_cannot_override_leases_or_unknown_or_bad_history(
    generated: bool, defect: str,
) -> None:
    session = failed_session(generated=generated)
    route = session.row.routing_decision
    state = cancelled_model_checkpoint()
    key = next(iter(state["models"]))
    model = state["models"][key]
    if defect.startswith("worker_"):
        setattr(session.row, defect, "retained")
    elif defect == "approval":
        session.approval = UUID(int=99)
    elif defect == "route-unknown":
        route["retained"] = "UNKNOWN"
    elif defect == "missing-checkpoint":
        session.checkpoints = []
    elif defect == "malformed-checkpoint":
        session.checkpoints = [None]
    elif defect == "checkpoint-unknown":
        state["uncertainty"] = "UNKNOWN"
    elif defect.startswith("tool-"):
        state["tools"] = [] if defect == "tool-malformed" else {"b" * 64: {"status": defect[5:]}}
    elif defect in {"model-unknown", "model-partial", "model-prepared"}:
        model["status"] = defect[6:]
    elif defect == "model-wrong-run":
        state["models"]["f" * 64] = state["models"].pop(key)
    elif defect == "model-request-sha":
        model["request_sha256"] = "invalid"
    elif defect == "model-attempt-bool":
        model["attempt"] = True
    elif defect == "model-status-bool":
        model["status"] = True
    elif defect == "model-extra":
        model["unrecognized"] = "value"
    elif defect == "model-empty":
        state["models"] = {}
    elif defect == "nested-running":
        state["other"] = {"phase": "running"}
    elif defect == "phase-unknown":
        state["phase"] = "unknown"
    elif defect == "self-repair":
        route["source"] = "self_repair"
    elif defect == "repair-lineage":
        route["self_repair_context"] = "retained"
    elif defect == "wrong-project":
        route["project_id"] = "foreign"
    elif generated:
        route["generated_project_preparation"]["phase"] = "captured"
        route["generated_project_preparation"].pop("python_recipe")
    else:
        route["project_input_preparation"]["phase"] = "captured"
    if defect not in {"missing-checkpoint", "malformed-checkpoint"}:
        session.checkpoints = [state]
    before = copy.deepcopy(vars(session.row)), copy.deepcopy(session.checkpoints)
    if generated:
        hook, calls = hook_for(session)
        await hook(tenant_id=session.row.tenant_id, run_id=session.row.id, status=RunStatus.FAILED,
                   actor_id=None, mode=None, routing_decision={})
        assert not [entry for entry in calls if entry != "generated-context"]
    else:
        assert "release" not in await invoke(session, callback_status=RunStatus.FAILED)
    assert (vars(session.row), session.checkpoints) == before


@pytest.mark.parametrize("refs", [(), (reference(), reference().model_copy(update={"environment_sha256": "9" * 64})),
    (reference().model_copy(update={"scope": reference().scope.model_copy(update={"run_id": UUID(int=99)})}),)])
async def test_failed_scope_requires_one_exact_real_reference(refs: tuple[Any, ...]) -> None:
    session = failed_session()
    assert "release" not in await invoke(session, callback_status=RunStatus.FAILED, refs=refs)
    assert RELEASE not in session.row.routing_decision


@pytest.mark.parametrize("failure", [ProcessCleanupIncomplete("python_project_cleanup_incomplete"),
                                      OSError("lost reply"), ValueError("foreign reply")])
async def test_failed_root_unknown_or_transport_failure_never_marks_release(failure: Exception) -> None:
    session = failed_session(generated=True)
    session.checkpoints = [cancelled_model_checkpoint()]
    hook, calls = hook_for(session)

    async def failed(*args: Any, **kwargs: Any) -> Any:
        assert session.open
        raise failure

    hook.executor.release_completed = failed
    with pytest.raises(type(failure)):
        await hook(tenant_id=session.row.tenant_id, run_id=session.row.id, status=RunStatus.FAILED,
                   actor_id=None, mode=None, routing_decision={})
    assert calls == ["generated-context"]
    assert RELEASE not in session.row.routing_decision and session.row.version == 1


@pytest.mark.parametrize("outcome", ["released", "absent"])
async def test_failed_hook_calls_actual_release_client_with_fresh_no_install_context(
    monkeypatch: pytest.MonkeyPatch, outcome: Literal["released", "absent"],
) -> None:
    from agent_hub.skills.sandbox import python_project_client as client
    from agent_hub.skills.sandbox.python_project_protocol import (
        PythonProjectBrokerResponse,
        PythonProjectReleaseEnvironmentRequest,
    )

    session = failed_session()
    calls = []

    class Repository:
        async def run_transaction(self) -> Any:
            return session

    class Reader:
        async def python_original_context(self, tenant: UUID, run: UUID) -> Any:
            assert session.open and (tenant, run) == (session.row.tenant_id, session.row.id)
            return SimpleNamespace(invocation=invocation(), environment_references=(reference(),))

    async def transport(request: Any, **kwargs: Any) -> Any:
        assert session.open and type(request) is PythonProjectReleaseEnvironmentRequest
        assert request.invocation.install_allowed is False
        assert request.invocation.execution_id.startswith("python_release_")
        assert request.invocation.execution_id != request.expected_environment.origin_execution_id
        assert request.invocation.timeout_seconds == 30
        assert request.expected_environment == reference()
        assert not {"terminal", "completed", "host_path", "status"} & request.model_dump().keys()
        calls.append(request)
        response = PythonProjectBrokerResponse(action=request.action,
            execution_id=request.invocation.execution_id, ok=True,
            released_environment=request.expected_environment, release_outcome=outcome)
        response.require_for(request)
        return response

    monkeypatch.setattr(client, "_systemd_supported", lambda: True)
    monkeypatch.setattr(client, "request_python_project_broker", transport)
    await PythonProjectTerminalHook(Repository(), Reader(), client.PythonProjectSystemdSandbox())(
        tenant_id=session.row.tenant_id, run_id=session.row.id, status=RunStatus.FAILED,
        actor_id=None, mode=None, routing_decision={})
    assert len(calls) == 1 and RELEASE in session.row.routing_decision
    assert session.row.status == "failed" and session.row.error_summary == "existing_failure"


async def test_failed_retry_remains_bounded_candidates_not_released_credit() -> None:
    class RetrySession(Session):
        async def scalar(self, query: Any) -> Any:
            self.queries.append(query)
            self._reads += 1
            return self.row if self._reads == 1 else self.approval

        async def scalars(self, query: Any) -> Any:
            if query.column_descriptions[0]["name"] != "RunRow":
                return await super().scalars(query)
            params = query.compile().params
            assert "failed" in params.values() and {POLICY: 1} in params.values()
            assert query._limit_clause.value == 8
            if RELEASE in self.row.routing_decision or any(
                isinstance(value, UUID) and self.row.id <= value for value in params.values()
            ):
                return Rows([])
            return Rows([self.row])

    session = RetrySession()
    session.row = failed_session(generated=True).row
    hook, calls = hook_for(session, fail_once=True)
    assert await hook.retry_completed() == 1
    assert RELEASE not in session.row.routing_decision  # candidate count is not released capacity
    assert await hook.retry_completed() == 0
    assert await hook.retry_completed() == 1
    ids = [v for v in calls if v != "generated-context"]
    assert len(ids) == len(set(ids)) == 2
    assert session.row.status == "failed" and session.row.version == 2
    assert await hook.retry_completed() == 0


@pytest.mark.parametrize("change", ["marker-removed", "lease-acquired", "status-changed"])
async def test_failed_candidate_query_cannot_replace_locked_current_state(change: str) -> None:
    class RacedSession(Session):
        async def scalars(self, query: Any) -> Any:
            assert query.column_descriptions[0]["name"] == "RunRow"
            return Rows([self.row])

        async def scalar(self, query: Any) -> Any:
            assert self.open and query._for_update_arg is not None
            if change == "marker-removed":
                self.row.routing_decision.pop(POLICY)
            elif change == "lease-acquired":
                self.row.worker_lease_token = UUID(int=99)
            else:
                self.row.status = "running"
            return self.row

    session = RacedSession()
    session.row = failed_session(generated=True).row
    hook, calls = hook_for(session)
    assert await hook.retry_completed() == 1  # examined only, never a capacity credit
    assert calls == [] and RELEASE not in session.row.routing_decision
    assert session.row.version == 1


@pytest.mark.parametrize("candidate_install", [False, True])
async def test_restarted_actual_original_readonly_service_failed_metadata_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate_install: bool,
) -> None:
    from agent_hub.capabilities.approvals import capability_approval_scope
    from agent_hub.capabilities.project_verification import ProjectVerificationService
    from tests.unit.capabilities.test_python_project_service import configured, observed, request

    verifier, repository, native, inv, source = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, inv))
    repository.approvals.add(capability_approval_scope(exact))
    if candidate_install:
        native.failure_index = 0
    await observed(verifier, repository, inv)
    if candidate_install:
        native.failure_index = None
        await verifier.verify(tenant_id=inv.tenant_id, run_id=inv.run_id, actor="engineer")
    route = repository.record.routing_decision
    route[POLICY] = 1
    route["requested_permissions"] = ["workspace.read"]
    repository.record = SimpleNamespace(**vars(repository.record), status=RunStatus.FAILED)
    repository.approvals.clear()
    restarted = ProjectVerificationService(root=verifier.root, repository=repository, executor=None,
                                            python_executor=native)
    session = failed_session()
    session.row.id, session.row.tenant_id = inv.run_id, inv.tenant_id
    session.row.routing_decision = route
    before_source = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    before = len(native.calls), len(native.probes), len(native.inspections)

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("metadata-only cleanup must never execute/probe/inspect/approve")

    for name in ("run", "probe", "inspect_environment"):
        monkeypatch.setattr(native, name, forbidden)
    monkeypatch.setattr(restarted, "authorization_request", forbidden)

    class Repository:
        async def run_transaction(self) -> Any:
            return session

    releases = []

    class Releaser:
        async def release_completed(self, item: Any, *, expected_environment: Any) -> Any:
            assert session.open and item.install_allowed is False
            releases.append(expected_environment)
            return expected_environment

    await PythonProjectTerminalHook(Repository(), restarted, Releaser())(
        tenant_id=inv.tenant_id, run_id=inv.run_id, status=RunStatus.FAILED,
        actor_id=None, mode=None, routing_decision={})
    assert len(releases) == 1 and releases[0].origin_execution_id == native.calls[-1].execution_id
    assert before == (len(native.calls), len(native.probes), len(native.inspections))
    assert before_source == {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert session.row.status == "failed" and not restarted._python.receipts


async def test_restarted_actual_generated_readonly_service_failed_keeps_live_authorizer_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.capabilities.generated_project_service import (
        GeneratedProjectError,
        generated_authority,
    )
    from tests.unit.capabilities.test_python_generated_service import approve, configured, request

    api = importlib.import_module("agent_hub.capabilities.python_generated_service")
    service, generated, repository, native, _, context = await configured(api, tmp_path)
    approve(repository, await service.authorization_request(request(), context))
    await service.verify(context, "engineer")
    repository.approvals.clear()
    row = repository.row
    row.status = RunStatus.FAILED
    row.worker_id = row.worker_lease_token = row.worker_lease_expires_at = row.worker_heartbeat_at = None
    row.routing_decision[POLICY] = 1
    row.version = 1
    session = failed_session(generated=True)
    session.row = row
    before_source = {p.relative_to(generated.root): p.read_bytes() for p in generated.root.rglob("*") if p.is_file()}
    before = len(native.calls), len(native.probes), len(native.inspections), repository.updates

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("readonly metadata must not dispatch or authorize")

    for name in ("run", "probe", "inspect_environment"):
        monkeypatch.setattr(native, name, forbidden)
    monkeypatch.setattr(generated, "prepare", forbidden)
    restarted = api.PythonGeneratedProjectService(service.owner, generated, native)
    with pytest.raises(GeneratedProjectError):
        generated_authority(row, row.tenant_id, row.id)

    class Repository:
        async def run_transaction(self) -> Any:
            return session

    releases = []

    class Releaser:
        async def release_completed(self, item: Any, *, expected_environment: Any) -> Any:
            assert session.open and item.install_allowed is False and item.source_kind == "candidate"
            releases.append(expected_environment)
            return expected_environment

    await PythonProjectTerminalHook(Repository(), service.owner, Releaser(), generated_reader=restarted)(
        tenant_id=row.tenant_id, run_id=row.id, status=RunStatus.FAILED,
        actor_id=None, mode=TaskMode.HYBRID, routing_decision={})
    assert len(releases) == 1
    assert before == (len(native.calls), len(native.probes), len(native.inspections), repository.updates)
    assert before_source == {p.relative_to(generated.root): p.read_bytes() for p in generated.root.rglob("*") if p.is_file()}
    assert row.status is RunStatus.FAILED and not restarted.receipts
