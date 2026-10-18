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
    model = Model(batch({"main.py": BROKEN}), batch({"DELIVERY_MANIFEST.json": "{}"}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    with pytest.raises(RuntimeExecutionError, match="no progress"):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == 2 and len(shared.requests) == 1
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
    ctx, model = context(), Model(batch({"main.py": BROKEN}), batch({"main.py": BROKEN}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    with pytest.raises(RuntimeExecutionError, match="no progress"):
        _ = [event async for event in runtime.run(ctx)]
    assert len(model.requests) == 2 and len(shared.requests) == 1
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
