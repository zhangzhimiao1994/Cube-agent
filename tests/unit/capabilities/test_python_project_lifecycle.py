"""DB metadata fixtures, with no host commands or cache deletion."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Self, cast
from uuid import UUID

import pytest

from agent_hub.domain.runs import RunStatus
from tests.unit.skills.sandbox.test_python_project_client import invocation, reference


def cancelled_model_checkpoint() -> dict[str, Any]:
    import hashlib

    model = {"status": "running", "step_id": "implement", "attempt": 0,
        "purpose": "step", "actor": "implementer", "call_index": 1,
        "request_sha256": "a" * 64, "artifact_id": None, "sha256": None, "provenance": None}
    material = f"{UUID(int=2)}:implement:0:step:implementer:1"
    key = hashlib.sha256(material.encode()).hexdigest()
    return {"phase": "running", "models": {key: model}, "tools": {}, "terminal": False}


class Rows:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def all(self) -> list[Any]:
        return self.values


class Session:
    def __init__(self) -> None:
        self.row = SimpleNamespace(
            id=UUID(int=2),
            tenant_id=UUID(int=1),
            status="completed",
            worker_id=None,
            worker_lease_token=None,
            worker_lease_expires_at=None,
            worker_heartbeat_at=None,
            version=1,
            routing_decision={
                "project_id": "demo",
                "workspace_session_id": "one",
                "sandbox_profile": "workspace_write",
                "requested_permissions": ["workspace.read"],
                "project_input_preparation": {"schema_version": 2, "phase": "observed"},
            },
        )
        self.approval: Any = None
        self.checkpoints: list[Any] = [{"tool_states": {"one": {"status": "succeeded"}}}]
        self.queries: list[Any] = []
        self.open = False

    async def scalar(self, query: Any) -> Any:
        self.queries.append(query)
        return self.row if len(self.queries) == 1 else self.approval

    async def scalars(self, query: Any) -> Any:
        return Rows(self.checkpoints)

    @asynccontextmanager
    async def begin(self) -> Any:
        self.open = True
        self._reads = 0
        try:
            yield
        finally:
            self.open = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


async def invoke(session: Session, *, refs: Any = None, callback_status: Any = None) -> list[Any]:
    from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook

    calls = []

    class Repository:
        async def run_transaction(self) -> Any:
            return session

    class Verifier:
        async def python_original_context(self, tenant: Any, run: Any) -> Any:
            assert session.open
            calls.append("context")
            return SimpleNamespace(
                invocation=invocation(),
                environment_references=(reference(),) if refs is None else refs,
            )

    class Executor:
        async def release_completed(self, inv: Any, *, expected_environment: Any) -> Any:
            assert session.open
            assert inv.execution_id != invocation().execution_id
            assert inv.install_allowed is False
            assert expected_environment == reference()
            calls.append("release")
            return expected_environment

    await PythonProjectTerminalHook(Repository(), Verifier(), Executor())(
        tenant_id=UUID(int=1),
        run_id=UUID(int=2),
        actor_id=None,
        status=RunStatus.COMPLETED if callback_status is None else callback_status,
        mode=None,
        routing_decision={"terminal": True},
    )
    return calls


async def test_completed_rereads_locked_db_metadata_and_durable_refs_before_rpc() -> None:
    session = Session()
    assert await invoke(session) == ["context", "release"]
    assert session.queries[0]._for_update_arg is not None


@pytest.mark.parametrize("status", list(RunStatus))
async def test_only_completed_db_status_is_eligible(status: RunStatus) -> None:
    session = Session()
    session.row.status = status.value
    calls = await invoke(session)
    assert calls == (["context", "release"] if status is RunStatus.COMPLETED else [])


@pytest.mark.parametrize(
    "field", ["worker_id", "worker_lease_token", "worker_lease_expires_at", "worker_heartbeat_at"]
)
async def test_any_worker_metadata_retains_cache(field: str) -> None:
    session = Session()
    setattr(session.row, field, "retained")
    assert await invoke(session) == []


@pytest.mark.parametrize(
    "change",
    [
        "approval",
        "unknown",
        "running",
        "missing_checkpoint",
        "legacy",
        "foreign_run",
        "foreign_ref",
        "ambiguous_ref",
        "invalid_checkpoint",
        "foreign_project",
    ],
)
async def test_pending_unknown_legacy_or_mismatched_metadata_fails_closed(change: str) -> None:
    session = Session()
    refs: tuple[Any, ...] | None = None
    if change == "approval":
        session.approval = UUID(int=3)
    elif change in {"unknown", "running"}:
        session.checkpoints = [{"tool_states": {"one": {"status": change.upper()}}}]
    elif change == "missing_checkpoint":
        session.checkpoints = []
    elif change == "legacy":
        session.row.routing_decision = {"project_input_preparation": {"schema_version": 1}}
    elif change == "foreign_run":
        session.row.id = UUID(int=9)
    elif change == "invalid_checkpoint":
        session.checkpoints = [None]
    elif change == "foreign_project":
        session.row.routing_decision["project_id"] = "foreign"
    elif change == "foreign_ref":
        refs = (
            reference().model_copy(
                update={"scope": reference().scope.model_copy(update={"run_id": UUID(int=9)})}
            ),
        )
    else:
        refs = (reference(), reference().model_copy(update={"environment_sha256": "0" * 64}))
    assert "release" not in await invoke(session, refs=refs)


async def test_failed_callback_cannot_request_completed_cleanup() -> None:
    assert await invoke(Session(), callback_status=RunStatus.FAILED) == []


async def test_new_cancelled_run_releases_only_its_exact_durable_environment() -> None:
    session = Session()
    session.row.status = 'cancelled'
    session.row.routing_decision['python_project_cancel_cleanup_version'] = 1
    assert await invoke(session, callback_status=RunStatus.CANCELLED) == ['context', 'release']
    assert session.row.version == 2
    assert session.row.routing_decision['python_project_cache_release']['schema_version'] == 1
    assert session.queries[0]._for_update_arg is not None


async def test_cancelled_history_only_model_snapshot_releases_under_lock_without_rewriting_checkpoint() -> None:
    import copy

    session = Session()
    session.row.status = 'cancelled'
    session.row.routing_decision['python_project_cancel_cleanup_version'] = 1
    session.checkpoints = [cancelled_model_checkpoint()]
    before = copy.deepcopy(session.checkpoints)
    assert await invoke(session, callback_status=RunStatus.CANCELLED) == ['context', 'release']
    assert session.checkpoints == before
    assert session.queries[0]._for_update_arg is not None


@pytest.mark.parametrize('defect', [
    'completed', 'failed', 'no-version', 'bad-version', 'selfrepair', 'route-unknown',
    'worker_id', 'worker_lease_token', 'worker_lease_expires_at', 'worker_heartbeat_at', 'approval',
    'tools-running', 'tools-unknown', 'tools-partial', 'tools-prepared', 'tools-malformed',
    'model-unknown', 'model-partial', 'model-prepared', 'model-id', 'foreign-model-id',
    'model-malformed', 'model-status-type', 'model-request-hash', 'model-attempt-bool',
    'phase-unknown', 'phase-case', 'nested-running', 'other-model-running', 'no-models',
    'model-extra-field', 'depth',
])
async def test_cancelled_history_exception_never_ignores_other_uncertainty_or_bad_shapes(defect: str) -> None:
    session = Session()
    session.row.status = 'cancelled'
    route = session.row.routing_decision
    route['python_project_cancel_cleanup_version'] = 1
    state = cancelled_model_checkpoint()
    models = state['models']
    key = next(iter(models))
    model = models[key]
    if defect in {'completed', 'failed'}:
        session.row.status = defect
    elif defect == 'no-version':
        route.pop('python_project_cancel_cleanup_version')
    elif defect == 'bad-version':
        route['python_project_cancel_cleanup_version'] = True
    elif defect == 'selfrepair':
        route['source'] = 'self_repair'
    elif defect == 'route-unknown':
        route['phase'] = 'UNKNOWN'
    elif defect.startswith('worker_'):
        setattr(session.row, defect, 'present')
    elif defect == 'approval':
        session.approval = UUID(int=99)
    elif defect.startswith('tools-'):
        state['tools'] = [] if defect == 'tools-malformed' else {'b' * 64: {'status': defect[6:]}}
    elif defect in {'model-unknown', 'model-partial', 'model-prepared'}:
        model['status'] = defect[6:]
    elif defect in {'model-id', 'foreign-model-id'}:
        models['bad' if defect == 'model-id' else 'f' * 64] = models.pop(key)
    elif defect == 'model-malformed':
        models[key] = 'running'
    elif defect == 'model-status-type':
        model['status'] = True
    elif defect == 'model-request-hash':
        model['request_sha256'] = 'bad'
    elif defect == 'model-attempt-bool':
        model['attempt'] = True
    elif defect in {'phase-unknown', 'phase-case'}:
        state['phase'] = 'unknown' if defect == 'phase-unknown' else 'RUNNING'
    elif defect == 'nested-running':
        state['nested'] = {'phase': 'running'}
    elif defect == 'other-model-running':
        model['provenance'] = {'phase': 'running'}
    elif defect == 'model-extra-field':
        model['extra'] = 'benign-but-not-ledger-schema'
    elif defect == 'no-models':
        state['models'] = {}
    else:
        nested: dict[str, Any] = {}
        state['deep'] = nested
        for _ in range(66):
            nested['next'] = {}
            nested = nested['next']
    session.checkpoints = [state]
    assert await invoke(session, callback_status=RunStatus.CANCELLED) == []
    assert 'python_project_cache_release' not in route


@pytest.mark.parametrize('version', [None, True, False, '1', 1.0, 0, 2])
async def test_legacy_or_invalid_cancelled_policy_keeps_environment(version: object) -> None:
    session = Session()
    session.row.status = 'cancelled'
    if version is not None:
        session.row.routing_decision['python_project_cancel_cleanup_version'] = version
    assert await invoke(session, callback_status=RunStatus.CANCELLED) == []


@pytest.mark.parametrize('field', [
    'self_repair_decision_token_hash', 'self_repair_context', 'self_repair_proposal',
    'self_repair_recovery_baseline_sequence', 'self_repair_accepted',
])
async def test_cancelled_repair_lineage_is_retained(field: str) -> None:
    session = Session()
    session.row.status = 'cancelled'
    session.row.routing_decision.update(python_project_cancel_cleanup_version=1)
    session.row.routing_decision[field] = 'retained'
    assert await invoke(session, callback_status=RunStatus.CANCELLED) == []


async def test_cancelled_self_repair_source_without_other_lineage_is_retained() -> None:
    session = Session()
    session.row.status = 'cancelled'
    session.row.routing_decision.update(python_project_cancel_cleanup_version=1, source='self_repair')
    assert await invoke(session, callback_status=RunStatus.CANCELLED) == []


@pytest.mark.parametrize('change', ['lease', 'approval', 'unknown', 'missing_checkpoint', 'failed', 'callback_only'])
async def test_cancelled_cleanup_preserves_existing_lifecycle_fences(change: str) -> None:
    session = Session()
    session.row.status = 'cancelled'
    session.row.routing_decision['python_project_cancel_cleanup_version'] = 1
    if change == 'lease':
        session.row.worker_lease_token = UUID(int=99)
    elif change == 'approval':
        session.approval = UUID(int=98)
    elif change == 'unknown':
        session.checkpoints = [{'phase': 'UNKNOWN'}]
    elif change == 'missing_checkpoint':
        session.checkpoints = []
    elif change == 'failed':
        session.row.status = 'failed'
    else:
        session.row.routing_decision.pop('python_project_cancel_cleanup_version')
    assert await invoke(session, callback_status=RunStatus.CANCELLED) == []


async def test_successful_cleanup_is_durable_and_notified_marker_does_not_prevent_retry() -> None:
    session = Session()
    session.row.routing_decision["terminal_hook_notified"] = True
    assert await invoke(session) == ["context", "release"]
    assert session.row.routing_decision["python_project_cache_release"]["schema_version"] == 1
    assert session.row.version == 2
    session.queries.clear()
    assert await invoke(session) == []


@pytest.mark.parametrize("candidate_install", [False, True])
@pytest.mark.parametrize('terminal_status', [RunStatus.COMPLETED, RunStatus.CANCELLED])
async def test_hook_uses_restarted_real_service_durable_refs_without_reexecution(
    tmp_path: Any,
    candidate_install: bool,
    terminal_status: RunStatus,
) -> None:
    from agent_hub.capabilities.approvals import capability_approval_scope
    from agent_hub.capabilities.project_verification import ProjectVerificationService
    from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook
    from tests.unit.capabilities.test_python_project_service import configured, observed, request

    verifier, repository, native, inv, _ = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, inv))
    repository.approvals.add(capability_approval_scope(exact))
    if candidate_install:
        native.failure_index = 0
    await observed(verifier, repository, inv)
    if candidate_install:
        native.failure_index = None
        await verifier.verify(tenant_id=inv.tenant_id, run_id=inv.run_id, actor="engineer")
    restarted = ProjectVerificationService(
        root=verifier.root,
        repository=repository,
        executor=None,
        python_executor=native,
    )
    assert not restarted._python.environments and not restarted._python.receipts
    repository.approvals.clear()
    repository.record.routing_decision["requested_permissions"] = ["workspace.read"]
    session = Session()
    session.row.id, session.row.tenant_id = inv.run_id, inv.tenant_id
    session.row.routing_decision = repository.record.routing_decision
    session.row.status = terminal_status.value
    if terminal_status is RunStatus.CANCELLED:
        session.row.routing_decision['python_project_cancel_cleanup_version'] = 1
    before = (len(native.calls), len(native.probes), len(native.inspections))
    releases = []

    class MetadataRepository:
        async def run_transaction(self) -> Any:
            return session

    class Releaser:
        async def release_completed(self, item: Any, *, expected_environment: Any) -> Any:
            assert item.install_allowed is False
            assert item.execution_id not in {entry.execution_id for entry in native.calls}
            releases.append(expected_environment)
            return expected_environment

    await PythonProjectTerminalHook(MetadataRepository(), restarted, Releaser())(
        tenant_id=inv.tenant_id,
        run_id=inv.run_id,
        actor_id=None,
        status=terminal_status,
        mode=None,
        routing_decision={},
    )
    assert len(releases) == 1
    assert releases[0].origin_execution_id == native.calls[-1].execution_id
    assert before == (len(native.calls), len(native.probes), len(native.inspections))


@pytest.mark.parametrize('terminal_status', [RunStatus.COMPLETED, RunStatus.CANCELLED])
async def test_retry_scan_reenters_db_gate_after_failure_and_marks_only_typed_success(
    terminal_status: RunStatus,
) -> None:
    from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook

    class RetrySession(Session):
        async def scalar(self, query: Any) -> Any:
            self.queries.append(query)
            self._reads += 1
            return self.row if self._reads == 1 else self.approval

        async def scalars(self, query: Any) -> Any:
            if query.column_descriptions[0]["name"] == "RunRow":
                params = query.compile().params
                assert "completed" in params.values()
                assert {
                    "project_input_preparation": {"schema_version": 2, "phase": "observed"}
                } in params.values()
                if "python_project_cache_release" in self.row.routing_decision or any(
                    isinstance(value, UUID) and self.row.id <= value for value in params.values()
                ):
                    return Rows([])
                return Rows([self.row])
            return await super().scalars(query)

    session = RetrySession()
    session.row.status = terminal_status.value
    if terminal_status is RunStatus.CANCELLED:
        session.row.routing_decision['python_project_cancel_cleanup_version'] = 1
    attempts = []

    class Repository:
        async def run_transaction(self) -> Any:
            return session

    class Reader:
        async def python_original_context(self, *args: Any) -> Any:
            return SimpleNamespace(invocation=invocation(), environment_references=(reference(),))

    class Releaser:
        async def release_completed(self, inv: Any, *, expected_environment: Any) -> Any:
            attempts.append(inv.execution_id)
            if len(attempts) == 1:
                raise OSError("lost reply")
            return expected_environment

    hook = PythonProjectTerminalHook(Repository(), Reader(), Releaser())
    assert await hook.retry_completed() == 1
    assert "python_project_cache_release" not in session.row.routing_decision
    assert await hook.retry_completed() == 0  # end of page wraps the cursor
    assert await hook.retry_completed() == 1
    assert len(attempts) == len(set(attempts)) == 2
    assert session.row.routing_decision["python_project_cache_release"]["schema_version"] == 1
    assert await hook.retry_completed() == 0


async def test_retry_loop_survives_database_failure_and_stops_cleanly(monkeypatch: Any) -> None:
    import asyncio

    from agent_hub.capabilities.python_project_lifecycle import PythonProjectTerminalHook

    hook = PythonProjectTerminalHook(cast(Any, None), cast(Any, None), cast(Any, None))
    stop = asyncio.Event()
    calls = []

    async def retry() -> int:
        calls.append(True)
        if len(calls) == 1:
            raise OSError("database unavailable")
        stop.set()
        return 0

    monkeypatch.setattr(hook, "retry_completed", retry)
    await hook.retry_loop(stop, interval_seconds=0.001)
    assert len(calls) == 2
