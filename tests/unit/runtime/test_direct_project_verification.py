from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.contracts import EventKind, JsonValue, TaskContext
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import ProjectInvocation, project_source_fingerprint
from tests.unit.capabilities.test_scoped_read import FakeRunRepository, stored_run

BROKEN = "import sys; print('F01_EXPECTED_2_GOT_1', file=sys.stderr); sys.exit(3)\n"
FIXED = "print('F01_PASS')\n"


def context(**updates: Any) -> TaskContext:
    values: dict[str, Any] = {
        "run_id": uuid4(), "tenant_id": uuid4(), "mode": TaskMode.DIRECT,
        "request": "Build and verify the project in this workspace.",
        "actor_id": uuid4(), "actor_role": Role.OPERATOR,
        "token_budget": 50_000, "timeout_seconds": 60,
        "routing_decision": {
            "project_scale": "small", "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle", "sandbox_profile": "workspace_write",
        },
    }
    values.update(updates)
    return TaskContext(**values)


def batch(files: Mapping[str, str], *, complete: bool = True) -> str:
    return json.dumps({"workspace_batch": {
        "files": dict(files), "complete": complete,
        "continuation": "" if complete else "Continue implementation.",
    }, "summary": "Model workspace result."})


class Model:
    def __init__(self, *texts: str, usage: int = 180) -> None:
        self.texts = list(texts)
        self.requests: list[ModelRequest] = []
        self.usage = usage

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        assert self.texts, "Direct made an unexpected model call"
        return GatewayCompletion(
            response=ModelResponse(
                text=self.texts.pop(0), usage=TokenUsage(self.usage - 80, 80, self.usage),
            ),
            deployment_id="fixture", logical_model=request.logical_model,
            provider_id="fixture", provider_model="fixture/model",
            attempted_logical_models=(request.logical_model,),
        )


class Backend(RuntimeCapabilityGateway):
    def __init__(self, root: Path, ctx: TaskContext, *, failure: str = "exit") -> None:
        super().__init__(
            skill_store_dir=root / "skills", project_workspace_dir=root / "workspaces",
            generated_artifact_dir=root / "generated",
            run_repository=FakeRunRepository(stored_run(
                tenant=ctx.tenant_id, run=ctx.run_id,
                project_id="direct-verification", session="same-task",
            )),
        )
        self.root = (root / "workspaces" / str(ctx.tenant_id) / "projects"
                     / "direct-verification" / "sessions" / "same-task")
        self.failure = failure
        self.ctx = ctx
        self.workspace_root = root / "workspaces"
        self.calls: list[str] = []
        self.process_results: list[SkillResult] = []
        self.availability: object = True
        self.require_verified_bundle = True
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    def is_available(self, tenant_id: Any, name: str) -> bool:
        if name == "project.verify":
            if self.availability == "error":
                raise RuntimeError("private availability error")
            return self.availability  # type: ignore[return-value]
        return super().is_available(tenant_id, name)

    async def execute(self, *, name: str, **kwargs: Any) -> Any:
        self.calls.append(name)
        if name != "project.verify":
            if name == "workspace.bundle" and self.require_verified_bundle:
                assert self.process_results[-1].exit_code == 0
            return await super().execute(name=name, **kwargs)
        assert kwargs["arguments"] == {}
        assert kwargs["actor"] == "main_agent"
        assert (self.root / "DELIVERY_MANIFEST.json").is_file()
        if self.failure == "blocking":
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        if self.failure == "backend":
            raise RuntimeError("private backend exception")
        if self.failure == "untyped":
            raise RuntimeCapabilityError("skill execution failed")
        if self.failure in {"unknown", "failed_payload", "missing", "boolean"}:
            payloads = {
                "unknown": {"verification": {"status": "UNKNOWN"}},
                "failed_payload": {"verification": {"status": "failed"}},
                "missing": {}, "boolean": {"verified": True},
            }
            return payloads[self.failure]
        completed = await asyncio.to_thread(subprocess.run,
            [sys.executable, str(self.root / "main.py")],
            capture_output=True, text=True, timeout=5, check=False,
        )
        result = SkillResult(
            exit_code=completed.returncode, stdout=completed.stdout,
            stderr=completed.stderr, timed_out=False,
        )
        self.process_results.append(result)
        if result.exit_code != 0:
            if self.failure == "unavailable_after_failure":
                self.availability = False
            raise RuntimeCapabilityError(
                "skill execution failed",
                execution_diagnostic=ExecutionDiagnostic(backend="fixture", result=result),
            )
        fingerprint = await project_source_fingerprint(self.workspace_root, ProjectInvocation(
            execution_id="fixture", tenant_id=self.ctx.tenant_id, run_id=self.ctx.run_id,
            project_id="direct-verification", session_id="same-task",
            sandbox_profile="workspace_write", timeout_seconds=60,
        ))
        return {"verification": {
            "schema_version": 1, "status": "passed", "source_sha256": fingerprint,
            "checks": ({"name": "fixture", "exit_code": 0, "stdout": result.stdout},),
        }}


class Policy:
    def __init__(self, ctx: TaskContext, status: CapabilityStatus) -> None:
        self.ctx = ctx
        self.status = status
        self.calls: list[Any] = []

    async def invoke(self, request: Any, *, role: Role) -> CapabilityResult:
        assert request.tenant_id == self.ctx.tenant_id
        assert request.run_id == self.ctx.run_id
        assert request.user_id == self.ctx.actor_id
        assert request.agent_id == "main_agent"
        assert role is self.ctx.actor_role
        self.calls.append(request)
        return CapabilityResult(
            status=self.status, run_id=request.run_id,
            approval_id="fixture-approval" if self.status is CapabilityStatus.WAITING_APPROVAL
            else None,
        )


class SharedGateway(HarnessToolGateway):
    def __init__(self, backend: Backend, policy: Policy) -> None:
        super().__init__(backend, policy_gateway=policy, require_actor_identity=True)
        self.requests: list[HarnessToolCallRequest] = []
        self.results: list[HarnessToolCallResult] = []

    async def invoke(self, tenant_id: Any, request: HarnessToolCallRequest,
                     **kwargs: Any) -> HarnessToolCallResult:
        self.requests.append(request)
        result = await super().invoke(tenant_id, request, **kwargs)
        self.results.append(result)
        return result


def runner(tmp_path: Path, ctx: TaskContext, model: Model, *, failure: str = "exit",
           status: CapabilityStatus = CapabilityStatus.ALLOWED) -> tuple[
               DirectRuntime, Backend, SharedGateway, Policy,
           ]:
    backend = Backend(tmp_path, ctx, failure=failure)
    policy = Policy(ctx, status)
    shared = SharedGateway(backend, policy)
    runtime = DirectRuntime(
        model, logical_model="main", capability_gateway=backend, harness_tool_gateway=shared,
    )
    return runtime, backend, shared, policy


def diagnostic_context() -> TaskContext:
    ctx = context()
    return ctx.model_copy(update={"routing_decision": {
        **ctx.routing_decision, "engineering_diagnostic_reads_version": 1,
        "runtime_timeout_source": "project_scale_soft_budget",
        "runtime_timeout_soft_seconds": 60, "runtime_timeout_absolute_seconds": 120,
        "runtime_token_soft_base_tokens": 50_000, "runtime_token_absolute_tokens": 100_000,
        "critical_path_complexity_units": 20,
        "requested_permissions": ["workspace.read", "workspace.write", "command.run"],
    }})


def diagnostic_read_batch(*, path: str = "main.py", summary: str = "Inspect the failed implementation.") -> str:
    return json.dumps({"workspace_batch": {
        "files": {}, "read_paths": [path], "complete": False,
        "continuation": "Read the source before changing the failed implementation.",
    }, "summary": summary})


def capture_delivery_deadline(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from agent_hub.runtime import direct
    from agent_hub.runtime.adaptive_budget import deadline_from_routing

    deadlines: list[Any] = []
    create = deadline_from_routing

    def capture(*args: Any, **kwargs: Any) -> Any:
        budget = create(*args, **kwargs)
        deadlines.append(budget)
        return budget

    monkeypatch.setattr(direct, "deadline_from_routing", capture)
    return deadlines


async def test_direct_diagnostic_read_repairs_known_failure_before_verified_bundle(tmp_path: Path) -> None:
    ctx = diagnostic_context()
    model = Model(batch({"main.py": BROKEN, "README.md": "Keep me.\n"}),
                  diagnostic_read_batch(), batch({"main.py": FIXED}))
    runtime, backend, shared, policy = runner(tmp_path, ctx, model)
    events = [event async for event in runtime.run(ctx)]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert backend.calls == [
        "workspace.write_text", "workspace.write_text", "workspace.write_text",
        "project.verify", "read_context", "workspace.write_text", "workspace.write_text",
        "project.verify", "workspace.bundle",
    ]
    assert [request.tool_name for request in shared.requests] == ["project.verify", "read_context", "project.verify"]
    assert [result.status for result in shared.results] == ["failed", "succeeded", "succeeded"]
    read = shared.requests[1]
    assert read.run_id == ctx.run_id and read.actor == "main_agent" and read.sandbox == "workspace_write"
    assert read.arguments == {"path": "main.py"}
    assert dict(shared.results[1].payload) == {"path": "main.py", "text": BROKEN, "truncated": False}
    assert len(model.requests) == len(policy.calls) == 3
    assert policy.calls[1].capability == "file" and policy.calls[1].operation == "read"
    assert policy.calls[1].resource == "workspace/main.py"
    feedback = "\n".join(cast(str, message.content) for message in model.requests[-1].messages
                         if message.role == "user")
    assert "UNTRUSTED_PROJECT_VERIFICATION_JSON" in feedback and "F01_EXPECTED_2_GOT_1" in feedback
    assert "UNTRUSTED_WORKSPACE_READ_JSON" in feedback and BROKEN.strip() in feedback
    assert [result.exit_code for result in backend.process_results] == [3, 0]
    assert (backend.root / "README.md").read_text() == "Keep me.\n"
    delivery = cast(Mapping[str, JsonValue], events[-1].payload["workspace_delivery"])
    assert cast(Mapping[str, JsonValue], delivery["verification"])["status"] == "passed"
    archives = list((tmp_path / "generated").rglob("*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        assert archive.read("main.py").decode() == FIXED
        assert archive.read("README.md") == b"Keep me.\n"


@pytest.mark.parametrize("first_batch_read", [False, True])
async def test_direct_diagnostic_observations_preserve_budget_and_completed_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_batch_read: bool,
) -> None:
    from agent_hub.runtime import direct

    ctx = diagnostic_context()
    deadlines = capture_delivery_deadline(monkeypatch)
    token_limit = direct._workspace_delivery_token_limit
    token_observations = []
    read_observations: list[tuple[Any, ...]] = []

    def record_token_limit(*args: Any, **kwargs: Any) -> int:
        limit = token_limit(*args, **kwargs)
        token_observations.append((kwargs["completed_files"], kwargs["completed_batches"], limit))
        return limit

    def budget_state() -> tuple[Any, ...]:
        budget = deadlines[0]
        return (budget.deadline, budget.absolute_deadline, budget.progress_units, token_observations[-1])

    class ObservationModel(Model):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if read_observations:
                assert budget_state() == read_observations[-1], "observations must not earn time, tokens or batches"
            return await super().complete_with_context(request)

    texts = [diagnostic_read_batch(), diagnostic_read_batch(path="README.md"), batch({"main.py": FIXED})]
    if not first_batch_read:
        texts.insert(0, batch({"main.py": BROKEN}))
    model = ObservationModel(*texts)
    runtime, backend, shared, policy = runner(tmp_path, ctx, model)
    backend.root.mkdir(parents=True)
    (backend.root / "main.py").write_text(BROKEN, encoding="utf-8", newline="\n")
    assert (backend.root / "main.py").read_bytes() == BROKEN.encode("utf-8")
    # Exercise the real scoped reader's truncation without inventing receipt fields.
    (backend.root / "README.md").write_text("x" * 70_000 + "UNREAD_TAIL", encoding="utf-8")
    invoke = shared.invoke

    async def record_read(*args: Any, **kwargs: Any) -> HarnessToolCallResult:
        request = args[1]
        if request.tool_name == "read_context":
            read_observations.append(budget_state())
        return await invoke(*args, **kwargs)

    monkeypatch.setattr(direct, "_workspace_delivery_token_limit", record_token_limit)
    monkeypatch.setattr(shared, "invoke", record_read)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(deadlines) == 1 and len(read_observations) == 2
    assert read_observations[0] == read_observations[1]
    assert read_observations[0][-1][1] == (0 if first_batch_read else 1)
    assert read_observations[0][0] < read_observations[0][1]
    assert [request.tool_name for request in shared.requests] == (
        ([] if first_batch_read else ["project.verify"]) + ["read_context", "read_context", "project.verify"])
    reads = [result for result in shared.results if result.tool_name == "read_context"]
    assert reads[0].payload == {"path": "main.py", "text": BROKEN, "truncated": False}
    assert reads[1].status == "succeeded" and reads[1].payload["truncated"] is True
    assert "UNREAD_TAIL" not in cast(str, reads[1].payload["text"])
    final_prompt = "\n".join(cast(str, message.content) for message in model.requests[-1].messages)
    assert "UNTRUSTED_WORKSPACE_READ_JSON" in final_prompt
    assert '"truncated":true' in final_prompt and '"reader_truncated":true' in final_prompt
    assert backend.calls.count("workspace.bundle") == 1
    assert [result.exit_code for result in backend.process_results] == ([0] if first_batch_read else [3, 0])
    assert len(model.requests) == (3 if first_batch_read else 4)
    assert all(call.tenant_id == ctx.tenant_id and call.run_id == ctx.run_id
               and call.user_id == ctx.actor_id and call.agent_id == "main_agent" for call in policy.calls)


@pytest.mark.parametrize("changed_summary", [False, True])
async def test_direct_diagnostic_repeated_read_does_not_extend_soft_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_summary: bool,
) -> None:
    ctx = diagnostic_context()
    read = diagnostic_read_batch()
    repeated = diagnostic_read_batch(summary="New summary is not new evidence.") if changed_summary else read
    model = Model(batch({"main.py": BROKEN}), read, repeated, repeated, batch({"main.py": FIXED}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    deadlines = capture_delivery_deadline(monkeypatch)
    invoke = shared.invoke
    read_budgets = []

    async def record_read(*args: Any, **kwargs: Any) -> HarnessToolCallResult:
        request = args[1]
        if request.tool_name == "read_context":
            budget = deadlines[0]
            read_budgets.append((budget.deadline, budget.absolute_deadline, budget.progress_units))
        return await invoke(*args, **kwargs)

    monkeypatch.setattr(shared, "invoke", record_read)
    with pytest.raises(RuntimeExecutionError, match="no progress"):
        _ = [event async for event in runtime.run(ctx)]
    assert len(deadlines) == 1 and len(read_budgets) == 3
    budget = deadlines[0]
    assert (budget.deadline, budget.absolute_deadline, budget.progress_units) == read_budgets[-1]
    assert budget.deadline < budget.absolute_deadline
    assert [request.tool_name for request in shared.requests] == ["project.verify", *(["read_context"] * 3)]
    assert shared.results[1].payload == shared.results[2].payload == shared.results[3].payload
    assert len(model.requests) == 4 and len(model.texts) == 1
    assert any('"validation_error":"no_progress"' in str(m.content) for m in model.requests[-1].messages)
    assert backend.calls.count("project.verify") == 1 and "workspace.bundle" not in backend.calls
    assert (backend.root / "main.py").read_text() == BROKEN


async def test_direct_diagnostic_failed_read_does_not_extend_soft_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = diagnostic_context()
    model = Model(batch({"main.py": BROKEN}), diagnostic_read_batch(), batch({"main.py": FIXED}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    deadlines = capture_delivery_deadline(monkeypatch)
    execute = backend.execute
    read_budgets = []

    async def deny_read(*, name: str, **kwargs: Any) -> Any:
        if name == "read_context":
            backend.calls.append(name)
            budget = deadlines[0]
            read_budgets.append((budget.deadline, budget.absolute_deadline, budget.progress_units))
            raise RuntimeCapabilityError("workspace access is not authorized")
        return await execute(name=name, **kwargs)

    monkeypatch.setattr(backend, "execute", deny_read)
    with pytest.raises(RuntimeExecutionError, match="workspace read failed"):
        _ = [event async for event in runtime.run(ctx)]
    assert len(deadlines) == len(read_budgets) == 1
    budget = deadlines[0]
    assert (budget.deadline, budget.absolute_deadline, budget.progress_units) == read_budgets[0]
    assert budget.deadline < budget.absolute_deadline
    assert [request.tool_name for request in shared.requests] == ["project.verify", "read_context"]
    assert shared.results[-1].status == "failed"
    assert len(model.requests) == 2 and len(model.texts) == 1
    assert backend.calls.count("project.verify") == 1 and "workspace.bundle" not in backend.calls
    assert (backend.root / "main.py").read_text() == BROKEN


@pytest.mark.parametrize("marker", [None, True, 1.0, "1"])
async def test_direct_diagnostic_read_without_typed_marker_keeps_legacy_rejection(
    tmp_path: Path, marker: object,
) -> None:
    ctx = context()
    assert "engineering_diagnostic_reads_version" not in ctx.routing_decision
    if marker is not None:
        ctx = ctx.model_copy(update={"routing_decision": {
            **ctx.routing_decision, "engineering_diagnostic_reads_version": marker,
        }})
    model = Model(batch({"main.py": BROKEN}), diagnostic_read_batch(), batch({"main.py": FIXED}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    with pytest.raises(RuntimeExecutionError, match="workspace batch is invalid"):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == 2 and len(shared.requests) == 1
    assert shared.requests[0].tool_name == "project.verify" and shared.results[0].status == "failed"
    assert "read_context" not in backend.calls and "workspace.bundle" not in backend.calls


async def test_direct_diagnostic_read_approval_cold_resume_does_not_replay_old_effects(tmp_path: Path) -> None:
    from agent_hub.runtime.action_direct import ActionApprovedDirectRuntime
    from agent_hub.runtime.contracts import Artifact, RunEvent, RuntimeCheckpoint
    from agent_hub.runtime.worker_execution import worker_runtime_events
    from tests.unit.capabilities.test_verification_cold_recovery import Fixture
    from tests.unit.runtime.test_action_admission_helper import Policy as ApprovalPolicy

    fixture = Fixture(tmp_path, failed=True)
    fixture.db.row.routing_decision.update({"project_scale": "small", "project_delivery": "workspace",
        "artifact_strategy": "workspace_bundle", "engineering_diagnostic_reads_version": 1})
    policy = ApprovalPolicy()
    policy.allowed.update({"write", "run", "create"})
    broken, fixed = "Initial failing implementation.\n", "Repaired implementation.\n"
    executions: list[tuple[str, str]] = []
    read_results: list[HarnessToolCallResult] = []
    published: dict[Any, Artifact] = {}

    class RepairModel(Model):
        async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
            if len(self.requests) == 2:
                fixture.executor.failed = False
            return await super().complete_with_context(request)

    class RecordingBackend(RuntimeCapabilityGateway):
        async def execute(self, *, name: str, idempotency_key: str, **kwargs: Any) -> Any:
            executions.append((name, idempotency_key))
            return await super().execute(name=name, idempotency_key=idempotency_key, **kwargs)

    class RecordingShared(HarnessToolGateway):
        async def invoke(self, tenant_id: Any, request: HarnessToolCallRequest,
                         **kwargs: Any) -> HarnessToolCallResult:
            result = await super().invoke(tenant_id, request, **kwargs)
            if request.tool_name == "read_context":
                read_results.append(result)
            return result

    model = RepairModel(batch({"README.md": broken}), diagnostic_read_batch(path="README.md"),
                        batch({"README.md": fixed}))
    runtimes = []
    gateways = []

    async def step(checkpoint: RuntimeCheckpoint | None) -> list[RunEvent]:
        backend = RecordingBackend(skill_store_dir=tmp_path / "skills", project_workspace_dir=fixture.root,
            generated_artifact_dir=tmp_path / "generated", run_repository=fixture.runs,
            project_executor=fixture.executor, receipt_repository=fixture.db.repository(),
            artifact_repository=fixture.artifacts)
        runtime = ActionApprovedDirectRuntime(model, logical_model="main", capability_gateway=backend,
            artifact_repository=fixture.artifacts, harness_tool_gateway=RecordingShared(
                backend, policy_gateway=policy, require_actor_identity=True))
        runtimes.append(runtime)
        gateways.append(backend)
        ctx = fixture.context().model_copy(update={"checkpoint": checkpoint, "token_budget": 50_000,
                                                  "artifacts": tuple(published.values())})
        if checkpoint is not None:
            await runtime.restore_checkpoint(checkpoint)
        async with worker_runtime_events(runtime, ctx, worker_id=fixture.db.row.worker_id) as stream:
            events = [event async for event in stream]
        for event in events:
            if event.artifact is not None:
                published[event.artifact.id] = event.artifact
        return events

    waiting = await step(None)
    checkpoint = waiting[-1].checkpoint
    assert checkpoint is not None and checkpoint.runtime_type == "direct_action"
    assert checkpoint.state["tool_name"] == "read_context" and checkpoint.state["approval_id"] == "approval_read"
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in waiting)
    assert len(model.requests) == 2 and len(fixture.executor.calls) == 1
    assert [name for name, _ in executions] == ["workspace.write_text", "workspace.write_text", "project.verify"]
    assert len(read_results) == 1 and read_results[0].status == "failed"
    assert read_results[0].failure_reason == "capability requires approval"
    prior_executions = list(executions)
    prior_requests = list(model.requests)
    policy.allowed.add("read")
    fixture.db.new_lease()
    completed = await step(checkpoint)
    assert completed[-1].kind is EventKind.RUNTIME_COMPLETED
    assert runtimes[0] is not runtimes[1] and gateways[0] is not gateways[1]
    assert model.requests[:2] == prior_requests and len(model.requests) == 3
    assert len(fixture.executor.calls) == 2
    assert executions[:len(prior_executions)] == prior_executions
    assert [name for name, _ in executions[len(prior_executions):]] == [
        "read_context", "workspace.write_text", "workspace.write_text", "project.verify", "workspace.bundle"]
    assert len(executions) == len(set(executions)), "no already executed action may reach the backend twice"
    assert len(read_results) == 2 and read_results[-1].status == "succeeded"
    assert read_results[-1].payload == {"path": "README.md", "text": broken, "truncated": False}
    feedback = "\n".join(cast(str, message.content) for message in model.requests[-1].messages)
    assert "UNTRUSTED_WORKSPACE_READ_JSON" in feedback and "Initial failing implementation." in feedback
    assert "UNTRUSTED_PROJECT_VERIFICATION_JSON" in feedback
    archives = list((tmp_path / "generated").rglob("*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        assert archive.read("README.md").decode("utf-8") == fixed


async def test_direct_repairs_same_workspace_from_shared_diagnostic_before_bundle(
    tmp_path: Path,
) -> None:
    ctx = context()
    model = Model(batch({"main.py": BROKEN, "README.md": "Keep me.\n"}),
                  batch({"main.py": FIXED}))
    runtime, backend, shared, policy = runner(tmp_path, ctx, model)
    events = [event async for event in runtime.run(ctx)]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert backend.calls == [
        "workspace.write_text", "workspace.write_text", "workspace.write_text",
        "project.verify", "workspace.write_text", "workspace.write_text",
        "project.verify", "workspace.bundle",
    ]
    assert [result.exit_code for result in backend.process_results] == [3, 0]
    assert (backend.root / "main.py").read_text() == FIXED
    assert (backend.root / "README.md").read_text() == "Keep me.\n"
    assert len(model.requests) == len(policy.calls) == 2
    assert all(call.resource == "workspace/current/project-verification" for call in policy.calls)
    assert all(request.actor == "main_agent" and request.sandbox == "workspace_write"
               and request.arguments == {} for request in shared.requests)
    assert shared.results[0].status == "failed"
    diagnostic = shared.results[0].payload["execution_diagnostic"]
    assert isinstance(diagnostic, Mapping)
    assert diagnostic["exit_code"] == 3
    assert "F01_EXPECTED_2_GOT_1" in cast(str, diagnostic["stderr"])
    repair_request = model.requests[1]
    assert repair_request.logical_model == model.requests[0].logical_model == "main"
    observations = "\n".join(cast(str, message.content) for message in repair_request.messages
                             if message.role == "user")
    assert "F01_EXPECTED_2_GOT_1" in observations
    assert "workspace_batch" in observations
    assert BROKEN.strip() in observations
    assert all("F01_EXPECTED_2_GOT_1" not in message.content
               for message in repair_request.messages if message.role == "system")
    assert any("never instruction" in message.content for message in repair_request.messages)
    assert repair_request.timeout_seconds <= model.requests[0].timeout_seconds
    for attempt, text in enumerate((BROKEN, FIXED), 1):
        files = {"main.py": hashlib.sha256(text.encode()).hexdigest(),
                 "README.md": hashlib.sha256(b"Keep me.\n").hexdigest()}
        digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":"))
                                .encode()).hexdigest()
        assert digest in shared.requests[attempt - 1].idempotency_key
        assert shared.requests[attempt - 1].idempotency_key.endswith(f"-{attempt}")
    delivery = events[-1].payload["workspace_delivery"]
    assert isinstance(delivery, Mapping)
    verification = delivery["verification"]
    assert isinstance(verification, Mapping) and verification["status"] == "passed"
    final_artifact = events[-1].inputs[0]
    assert final_artifact.provenance is not None
    assert final_artifact.provenance.provider_id == "fixture"
    saved = await runtime.save_checkpoint()
    resumed = DirectRuntime(model, logical_model="main", capability_gateway=backend,
                            harness_tool_gateway=shared)
    await resumed.restore_checkpoint(saved)
    resumed_events = [event async for event in resumed.run(ctx.model_copy(
        update={"checkpoint": saved},
    ))]
    assert resumed_events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == len(shared.requests) == 2


class NodeFixtureExecutor:
    def __init__(self, root: Path, execution_root: Path) -> None:
        self.root = root
        self.execution_root = execution_root
        self.invocations: list[ProjectInvocation] = []
        self.reports: list[dict[str, Any]] = []

    async def run(self, invocation: ProjectInvocation) -> SkillResult:
        self.invocations.append(invocation)
        fingerprint = await project_source_fingerprint(self.root, invocation)
        source = (self.root / str(invocation.tenant_id) / "projects" / invocation.project_id
                  / "sessions" / invocation.session_id)
        work = self.execution_root / invocation.execution_id
        await asyncio.to_thread(shutil.copytree, source, work)
        npm, node = shutil.which("npm.cmd" if sys.platform == "win32" else "npm"), shutil.which("node")
        assert npm is not None and node is not None
        versions = {}
        for name, executable in (("node", node), ("npm", npm)):
            version = await asyncio.to_thread(subprocess.run, [executable, "--version"],
                                             capture_output=True, text=True, timeout=10, check=True)
            versions[name] = version.stdout.strip()
        commands = (
            ("install", [npm, "install", "--offline", "--ignore-scripts", "--no-audit", "--no-fund"]),
            ("build", [node, "build.cjs"]), ("test", [node, "main.cjs"]),
        )
        checks = []
        for phase, argv in commands:
            completed = await asyncio.to_thread(
                subprocess.run, argv, cwd=work, capture_output=True, text=True,
                timeout=20, check=False,
            )
            checks.append({
                "phase": phase, "status": "passed" if completed.returncode == 0 else "failed",
                "exit_code": completed.returncode, "timed_out": False,
                "stdout": completed.stdout, "stderr": completed.stderr,
            })
        report = {
            "schema_version": 1,
            "status": "passed" if all(check["status"] == "passed" for check in checks) else "failed",
            "source_sha256": fingerprint, "checks": checks,
            "runtime": versions,
        }
        self.reports.append(report)
        return SkillResult(exit_code=0, stdout=json.dumps(report), stderr="", timed_out=False)


async def test_direct_node_delivery_passes_real_source_receipt_and_bundle_gate(tmp_path: Path) -> None:
    ctx = context()
    root = tmp_path / "workspace"
    executor = NodeFixtureExecutor(root, tmp_path / "executions")
    calls: list[str] = []

    class RealBackend(RuntimeCapabilityGateway):
        async def execute(self, *, name: str, **kwargs: Any) -> Mapping[str, JsonValue]:
            calls.append(name)
            return await super().execute(name=name, **kwargs)

    backend = RealBackend(
        skill_store_dir=tmp_path / "skills", project_workspace_dir=root,
        generated_artifact_dir=tmp_path / "generated", project_executor=executor,
        run_repository=FakeRunRepository(stored_run(
            tenant=ctx.tenant_id, run=ctx.run_id, actor_id=ctx.actor_id,
            project_id="direct-node", session="same-task", execution_backend="systemd",
            requested_permissions=["workspace.read", "workspace.write", "command.run", "network.read"],
        )),
    )
    broken = "require('node:assert/strict').equal(1, 2, 'F01_NODE_FAILURE');\n"
    fixed = "require('node:assert/strict').equal(2, 2); console.log('F01_NODE_PASS');\n"
    model = Model(batch({
        "package.json": json.dumps({"name": "direct-fixture", "version": "1.0.0",
                                    "scripts": {"build": "node build.cjs", "test": "node main.cjs"}}),
        "build.cjs": "console.log('F01_BUILD_PASS');\n", "main.cjs": broken,
    }), batch({"main.cjs": fixed}))
    policy = Policy(ctx, CapabilityStatus.ALLOWED)
    shared = HarnessToolGateway(backend, policy_gateway=policy, require_actor_identity=True)
    runtime = DirectRuntime(model, logical_model="main", capability_gateway=backend,
                            harness_tool_gateway=shared)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert calls.count("project.verify") == len(executor.invocations) == len(model.requests) == 2
    assert calls.count("workspace.bundle") == 1 and calls[-1] == "workspace.bundle"
    assert [report["status"] for report in executor.reports] == ["failed", "passed"]
    assert any("F01_NODE_FAILURE" in message.content for message in model.requests[1].messages)
    invocation = executor.invocations[-1]
    assert invocation.tenant_id == ctx.tenant_id and invocation.run_id == ctx.run_id
    delivery = cast(Mapping[str, JsonValue], events[-1].payload["workspace_delivery"])
    verification = cast(Mapping[str, JsonValue], delivery["verification"])
    assert verification["status"] == "passed"
    assert verification["source_sha256"] == await project_source_fingerprint(root, invocation)
    assert "artifact_id" in delivery
    archives = list((tmp_path / "generated").rglob("*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        assert archive.read("main.cjs").decode("utf-8") == fixed
    source = root / str(ctx.tenant_id) / "projects" / "direct-node" / "sessions" / "same-task"
    (source / "DELIVERY_MANIFEST.json").write_text("{}", encoding="utf-8")
    # Every delivered file, including metadata, is bound to the verified source.
    with pytest.raises(RuntimeCapabilityError, match="project verification required"):
        await backend.execute(tenant_id=ctx.tenant_id, run_id=ctx.run_id, actor="main_agent",
                              name="workspace.bundle", arguments={}, idempotency_key="metadata-bundle")
    (source / "main.cjs").write_text(broken, encoding="utf-8")
    with pytest.raises(RuntimeCapabilityError, match="project verification required"):
        await backend.execute(tenant_id=ctx.tenant_id, run_id=ctx.run_id, actor="main_agent",
                              name="workspace.bundle", arguments={}, idempotency_key="stale-bundle")


@pytest.mark.parametrize("failure", ["backend", "untyped", "unknown", "failed_payload",
                                    "missing", "boolean"])
async def test_direct_unknown_and_nondiagnostic_failure_do_not_replay_or_bundle(
    tmp_path: Path, failure: str,
) -> None:
    ctx, model = context(), Model(batch({"main.py": BROKEN}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model, failure=failure)
    with pytest.raises(RuntimeExecutionError, match="project verification"):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == len(shared.requests) == 1
    assert "workspace.bundle" not in backend.calls
    with pytest.raises(RuntimeExecutionError, match="no completed runtime boundary"):
        await runtime.save_checkpoint()


async def test_direct_repair_can_take_more_than_two_model_rounds(tmp_path: Path) -> None:
    ctx = context()
    model = Model(batch({"main.py": BROKEN}), batch({"main.py": BROKEN + "# first fix\n"}),
                  batch({"main.py": BROKEN + "# second fix\n"}), batch({"main.py": FIXED}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == len(shared.requests) == 4
    assert [result.exit_code for result in backend.process_results] == [3, 3, 3, 0]
    assert backend.calls.count("workspace.bundle") == 1


async def test_direct_repair_cycle_stops_before_replaying_failed_content(tmp_path: Path) -> None:
    ctx = context()
    model = Model(batch({"main.py": BROKEN}), batch({"main.py": BROKEN + "# attempt\n"}),
                  batch({"main.py": BROKEN}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    with pytest.raises(RuntimeExecutionError, match="no progress"):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == 3 and len(shared.requests) == 2
    assert "workspace.bundle" not in backend.calls


async def test_direct_manifest_only_repair_is_not_source_progress(tmp_path: Path) -> None:
    ctx = context()
    model = Model(batch({"main.py": BROKEN}), batch({"DELIVERY_MANIFEST.json": "{}"}),
                  batch({"main.py": BROKEN}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    with pytest.raises(RuntimeExecutionError, match="no progress"):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == 3 and len(shared.requests) == 1
    assert "workspace.bundle" not in backend.calls


@pytest.mark.parametrize("available", [False, None, 1, "yes", "error"])
async def test_direct_verification_requires_explicit_capability_availability(
    tmp_path: Path, available: object,
) -> None:
    ctx, model = context(), Model(batch({"main.py": FIXED}))
    runtime, backend, shared, policy = runner(tmp_path, ctx, model)
    backend.availability = available
    backend.require_verified_bundle = False
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not shared.requests and not policy.calls
    assert backend.calls.count("workspace.bundle") == 1


async def test_direct_without_injected_shared_gateway_keeps_legacy_workspace_delivery(
    tmp_path: Path,
) -> None:
    ctx, model = context(), Model(batch({"main.py": FIXED}))
    backend = Backend(tmp_path, ctx)
    backend.require_verified_bundle = False
    runtime = DirectRuntime(model, logical_model="main", capability_gateway=backend)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert "project.verify" not in backend.calls and "workspace.bundle" in backend.calls


async def test_direct_chat_and_hybrid_final_synthesis_do_not_gain_verification_tools(
    tmp_path: Path,
) -> None:
    ctx, model = context(routing_decision={}), Model("A plain final answer.")
    runtime, backend, shared, policy = runner(tmp_path, ctx, model)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert not backend.calls and not shared.requests and not policy.calls
    assert all(not request.tools for request in model.requests)


async def test_direct_html_fallback_uses_same_repair_rounds_before_real_bundle(tmp_path: Path) -> None:
    ctx = context()
    model = Model("<!doctype html><html><body>Initial view</body></html>",
                  batch({"main.py": FIXED, "preview.html": "<!doctype html><main>Fixed</main>"}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    # A real file exists independently of the model's HTML response and fails the executor.
    backend.root.mkdir(parents=True)
    (backend.root / "main.py").write_text(BROKEN, encoding="utf-8")
    ctx = ctx.model_copy(update={"routing_decision": {
        **ctx.routing_decision, "website_preview_required": True,
    }})
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(model.requests) == len(shared.requests) == 2
    assert backend.calls[-1] == "workspace.bundle"


@pytest.mark.parametrize("status", [CapabilityStatus.DENIED, CapabilityStatus.WAITING_APPROVAL])
async def test_direct_verification_policy_blocks_repair_and_bundle(
    tmp_path: Path, status: CapabilityStatus,
) -> None:
    ctx, model = context(), Model(batch({"main.py": BROKEN}))
    runtime, backend, shared, policy = runner(tmp_path, ctx, model, status=status)
    with pytest.raises(RuntimeExecutionError, match="project verification"):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == len(policy.calls) == len(shared.requests) == 1
    assert "project.verify" not in backend.calls
    assert "workspace.bundle" not in backend.calls


async def test_direct_unchanged_repair_stops_without_reverification_or_bundle(tmp_path: Path) -> None:
    ctx, model = context(), Model(batch({"main.py": BROKEN}), batch({"main.py": BROKEN}),
                                 batch({"main.py": BROKEN}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    with pytest.raises(RuntimeExecutionError, match="no progress"):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == 3 and len(shared.requests) == 1
    assert "workspace.bundle" not in backend.calls


async def test_direct_exhausted_total_tokens_prevents_model_repair_and_bundle(tmp_path: Path) -> None:
    ctx = context(token_budget=5_000)
    model = Model(batch({"main.py": BROKEN}), usage=4_950)
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    with pytest.raises(RuntimeExecutionError, match="budget.*exhausted"):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == len(shared.requests) == 1
    assert "workspace.bundle" not in backend.calls


async def test_direct_usage_is_accumulated_across_repair_model_rounds(tmp_path: Path) -> None:
    ctx = context(token_budget=5_000)
    model = Model(batch({"main.py": BROKEN}), batch({"main.py": FIXED}), usage=3_000)
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    with pytest.raises(RuntimeExecutionError, match="budget exceeds runtime limit"):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == 2 and len(shared.requests) == 1
    assert (backend.root / "main.py").read_text() == BROKEN
    assert "workspace.bundle" not in backend.calls


async def test_direct_verification_unavailable_after_failure_cannot_disable_delivery_gate(
    tmp_path: Path,
) -> None:
    ctx, model = context(), Model(batch({"main.py": BROKEN}), batch({"main.py": FIXED}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model, failure="unavailable_after_failure")
    with pytest.raises(RuntimeExecutionError, match="project verification failed"):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == len(shared.requests) == 2
    assert "workspace.bundle" not in backend.calls


async def test_direct_verification_uses_remaining_task_deadline_and_drains_timeout(
    tmp_path: Path,
) -> None:
    ctx, model = context(timeout_seconds=0.5), Model(batch({"main.py": BROKEN}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model, failure="blocking")
    with pytest.raises(RuntimeExecutionError, match="project verification failed"):
        async with asyncio.timeout(5):
            _ = [event async for event in runtime.run(ctx)]
    assert backend.started.is_set() and backend.cancelled.is_set()
    assert len(model.requests) == len(shared.requests) == 1
    assert "workspace.bundle" not in backend.calls
    with pytest.raises(RuntimeExecutionError, match="no completed runtime boundary"):
        await runtime.save_checkpoint()


async def test_direct_cancel_during_shared_verification_drains_task_without_checkpoint(
    tmp_path: Path,
) -> None:
    ctx, model = context(), Model(batch({"main.py": BROKEN}))
    runtime, backend, _, _ = runner(tmp_path, ctx, model, failure="blocking")
    async def consume() -> None:
        _ = [event async for event in runtime.run(ctx)]

    pending = asyncio.create_task(consume())
    await asyncio.wait_for(backend.started.wait(), timeout=5)
    await runtime.cancel()
    with pytest.raises((asyncio.CancelledError, StopAsyncIteration)):
        await pending
    assert backend.cancelled.is_set()
    assert "workspace.bundle" not in backend.calls
    with pytest.raises(RuntimeExecutionError, match="no completed runtime boundary"):
        await runtime.save_checkpoint()
