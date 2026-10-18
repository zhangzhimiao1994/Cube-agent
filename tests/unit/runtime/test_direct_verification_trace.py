from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.runtime.contracts import Artifact, EventKind
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from tests.unit.capabilities.test_scoped_read import FakeRunRepository, stored_run
from tests.unit.runtime.test_direct_project_verification import (
    BROKEN,
    FIXED,
    Model,
    NodeFixtureExecutor,
    Policy,
    batch,
    context,
    runner,
)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def mutable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: mutable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [mutable(item) for item in value]
    return value


def receipt_sha(value: object) -> str:
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":")))


async def test_trace_proves_real_failure_model_write_and_success_bound_to_receipt(
    tmp_path: Path,
) -> None:
    ctx = context()
    model = Model(batch({"main.py": BROKEN}), batch({"main.py": FIXED}))
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    events = [event async for event in runtime.run(ctx)]
    assert [result.exit_code for result in backend.process_results] == [3, 0]
    delivery = mutable(events[-1].payload["workspace_delivery"])
    history = delivery["verification_history"]
    assert history["owner"] == "direct_runtime"
    assert history["schema_version"] == 1
    assert history["attempt_count"] == 2 and history["failed_attempt_count"] == 1
    assert history["repair_observed"] is True and history["truncated"] is False
    failed, passed = history["attempts"]
    assert [entry["status"] for entry in history["attempts"]] == ["failed", "passed"]
    for attempt, (entry, content) in enumerate(((failed, BROKEN), (passed, FIXED)), 1):
        assert entry["attempt"] == entry["model_round"] == attempt
        assert entry["call_id"] == shared.results[attempt - 1].call_id
        assert entry["written_files"] == [{"path_sha256": sha("main.py"),
                                           "content_sha256": sha(content)}]
        assert entry["written_file_count"] == 1
        assert "actual_source_sha256" not in entry
    assert failed["checks"][0]["status"] == "failed"
    diagnostic = failed["checks"][0]["execution_diagnostic"]
    assert diagnostic["exit_code"] == 3 and diagnostic["timed_out"] is False
    gateway_diagnostic = shared.results[0].payload["execution_diagnostic"]
    assert isinstance(gateway_diagnostic, Mapping)
    assert diagnostic["stderr_metadata"] == gateway_diagnostic["stderr_metadata"]
    assert passed["repairs_attempt"] == 1
    assert failed["repairs_attempt"] is None
    assert failed["runtime_written_files_sha256"] != passed["runtime_written_files_sha256"]
    assert history["final_receipt"] == {
        "attempt": 2, "call_id": passed["call_id"],
        "source_sha256": delivery["verification"]["source_sha256"],
        "receipt_sha256": receipt_sha(delivery["verification"]),
    }
    assert passed["receipt_sha256"] == history["final_receipt"]["receipt_sha256"]
    assert passed["source_sha256"] != passed["runtime_written_files_sha256"]
    # Neither code, paths nor process output text is added to public trace evidence.
    public = json.dumps(history)
    assert all(value not in public for value in (
        BROKEN.strip(), FIXED.strip(), "main.py", str(backend.root),
        "F01_EXPECTED_2_GOT_1", "F01_PASS",
    ))
    created = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED
                   and "workspace_delivery" in event.payload)
    assert mutable(created.payload["workspace_delivery"])["verification_history"] == history
    assert mutable(events[-1].inputs[0].content)["workspace_delivery"]["verification_history"] == history


async def test_trace_first_pass_explicitly_has_no_repair_despite_model_claim(
    tmp_path: Path,
) -> None:
    ctx = context()
    text = json.loads(batch({"main.py": FIXED}))
    text["verification_history"] = {"repair_observed": True, "attempt_count": 999}
    text["summary"] = "I repaired a failing test and verified everything."
    runtime, backend, _, _ = runner(tmp_path, ctx, Model(json.dumps(text)))
    events = [event async for event in runtime.run(ctx)]
    assert [result.exit_code for result in backend.process_results] == [0]
    history = mutable(events[-1].payload["workspace_delivery"])["verification_history"]
    assert history["repair_observed"] is False
    assert history["attempt_count"] == 1 and history["failed_attempt_count"] == 0
    assert history["attempts"][0]["status"] == "passed"
    assert history["attempts"][0]["repairs_attempt"] is None


async def test_trace_collects_acknowledged_writes_across_partial_repair_batches(
    tmp_path: Path,
) -> None:
    ctx = context()
    model = Model(batch({"main.py": BROKEN}),
                  batch({"main.py": FIXED}, complete=False),
                  batch({"README.md": "Ready.\n"}))
    runtime, backend, _, _ = runner(tmp_path, ctx, model)
    events = [event async for event in runtime.run(ctx)]
    assert [result.exit_code for result in backend.process_results] == [3, 0]
    history = mutable(events[-1].payload["workspace_delivery"])["verification_history"]
    passed = history["attempts"][-1]
    assert passed["model_round"] == 3 and passed["repairs_attempt"] == 1
    assert passed["written_file_count"] == 2
    assert {item["path_sha256"]: item["content_sha256"] for item in passed["written_files"]} == {
        sha("main.py"): sha(FIXED), sha("README.md"): sha("Ready.\n"),
    }


async def test_trace_is_bounded_preserves_first_failure_and_final_receipt(tmp_path: Path) -> None:
    ctx = context(token_budget=200_000)
    texts = [batch({"main.py": BROKEN + f"# revision {index}\n"}) for index in range(35)]
    runtime, backend, _, _ = runner(tmp_path, ctx, Model(*texts, batch({"main.py": FIXED})))
    events = [event async for event in runtime.run(ctx)]
    history = mutable(events[-1].payload["workspace_delivery"])["verification_history"]
    assert len(backend.process_results) == history["attempt_count"] == 36
    assert history["failed_attempt_count"] == 35 and history["repair_observed"] is True
    assert history["truncated"] is True
    assert 2 <= len(history["attempts"]) <= 32
    assert history["omitted_attempt_count"] == 36 - len(history["attempts"])
    assert history["attempts"][0]["attempt"] == 1
    assert history["attempts"][-1]["attempt"] == history["final_receipt"]["attempt"] == 36


async def test_trace_bounds_file_fingerprints_without_claiming_complete_snapshot(
    tmp_path: Path,
) -> None:
    ctx = context()
    files = {f"notes-{index}.txt": f"note {index}\n" for index in range(70)}
    files["main.py"] = FIXED
    runtime, _, _, _ = runner(tmp_path, ctx, Model(batch(files)))
    events = [event async for event in runtime.run(ctx)]
    history = mutable(events[-1].payload["workspace_delivery"])["verification_history"]
    entry = history["attempts"][0]
    assert entry["written_file_count"] == 71 and len(entry["written_files"]) <= 64
    assert entry["omitted_written_file_count"] == 71 - len(entry["written_files"])
    assert history["truncated"] is True and history["repair_observed"] is False


async def test_trace_does_not_require_or_fabricate_history_for_legacy_artifacts(
    tmp_path: Path,
) -> None:
    ctx = context()
    old = Artifact(id=uuid4(), type="tool_result", producer="main", content={
        "workspace_delivery": {"verification": {"schema_version": 1, "status": "passed"}},
    })
    model = Model("A plain answer.")
    ctx = ctx.model_copy(update={"routing_decision": {}, "artifacts": (old,)})
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert "verification_history" not in json.dumps(mutable(events[-1].payload))
    assert not backend.calls and not shared.requests


async def test_trace_exposes_only_redacted_metadata_for_real_traceback_and_secret_output(
    tmp_path: Path,
) -> None:
    broken = ("import sys\nprint('password=PRIVATE_SECRET', file=sys.stderr)\n"
              "raise ValueError('PRIVATE_SOURCE_MARKER')\n")
    ctx = context()
    runtime, backend, shared, _ = runner(
        tmp_path, ctx, Model(batch({"main.py": broken}), batch({"main.py": FIXED})),
    )
    events = [event async for event in runtime.run(ctx)]
    diagnostic = shared.results[0].payload["execution_diagnostic"]
    assert isinstance(diagnostic, Mapping)
    metadata, stderr = diagnostic["stderr_metadata"], diagnostic["stderr"]
    assert isinstance(metadata, Mapping) and isinstance(stderr, str)
    assert metadata["redacted"] is True
    assert str(backend.root) in stderr
    history = mutable(events[-1].payload["workspace_delivery"])["verification_history"]
    evidence = history["attempts"][0]["checks"][0]["execution_diagnostic"]
    assert evidence["stderr_metadata"] == diagnostic["stderr_metadata"]
    public = json.dumps(history)
    assert all(value not in public for value in (
        "PRIVATE_SECRET", "PRIVATE_SOURCE_MARKER", "main.py", str(backend.root), "Traceback",
    ))


@pytest.mark.parametrize("blocked", ["approval", "denied", "truncated"])
async def test_trace_cannot_publish_repair_success_after_verification_gate_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked: str,
) -> None:
    ctx = context()
    model = Model(batch({"main.py": BROKEN}), batch({"main.py": FIXED}))
    runtime, backend, shared, policy = runner(tmp_path, ctx, model)
    invoke = shared.invoke

    async def invoke_with_gate(
        tenant_id: Any, request: HarnessToolCallRequest, **kwargs: Any,
    ) -> HarnessToolCallResult:
        result = await invoke(tenant_id, request, **kwargs)
        if len(shared.requests) == 1 and blocked != "truncated":
            policy.status = (CapabilityStatus.WAITING_APPROVAL if blocked == "approval"
                             else CapabilityStatus.DENIED)
        if len(shared.requests) == 2 and blocked == "truncated":
            return replace(result, truncated=True)
        return result

    monkeypatch.setattr(shared, "invoke", invoke_with_gate)
    events = []
    with pytest.raises(RuntimeExecutionError, match="project verification"):
        async for event in runtime.run(ctx):
            events.append(event)
    assert len(model.requests) == len(shared.requests) == 2
    assert "workspace.bundle" not in backend.calls
    assert all(event.kind is not EventKind.RUNTIME_COMPLETED for event in events)
    with pytest.raises(RuntimeExecutionError, match="no completed runtime boundary"):
        await runtime.save_checkpoint()


async def test_trace_stays_within_event_contract_after_many_large_repair_batches(
    tmp_path: Path,
) -> None:
    ctx = context(token_budget=200_000)
    texts = [batch({"main.py": BROKEN + f"# revision {revision}\n", **{
        f"notes-{index}.txt": f"revision {revision}\n" for index in range(24)
    }}) for revision in range(12)]
    texts.append(batch({"main.py": FIXED}))
    runtime, backend, _, _ = runner(tmp_path, ctx, Model(*texts))
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    history = mutable(events[-1].payload["workspace_delivery"])["verification_history"]
    assert len(backend.process_results) == history["attempt_count"] == 13
    assert history["truncated"] is True
    assert history["final_receipt"]["attempt"] == 13


async def test_trace_records_real_install_build_test_failure_and_reverification_receipt(
    tmp_path: Path,
) -> None:
    ctx = context()
    executor = NodeFixtureExecutor(tmp_path / "workspace", tmp_path / "executions")
    backend = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills", project_workspace_dir=tmp_path / "workspace",
        generated_artifact_dir=tmp_path / "generated", project_executor=executor,
        run_repository=FakeRunRepository(stored_run(
            tenant=ctx.tenant_id, run=ctx.run_id, actor_id=ctx.actor_id,
            project_id="direct-trace", session="same-task", execution_backend="systemd",
            requested_permissions=["workspace.read", "workspace.write", "command.run", "network.read"],
        )),
    )
    broken = "require('node:assert/strict').equal(1, 2, 'PRIVATE_NODE_SOURCE');\n"
    fixed = "require('node:assert/strict').equal(2, 2);\n"
    model = Model(batch({
        "package.json": json.dumps({"name": "direct-trace", "version": "1.0.0",
                                    "scripts": {"build": "node build.cjs", "test": "node main.cjs"}}),
        "build.cjs": "console.log('ready');\n", "main.cjs": broken,
    }), batch({"main.cjs": fixed}))
    shared = HarnessToolGateway(backend, policy_gateway=Policy(ctx, CapabilityStatus.ALLOWED),
                                require_actor_identity=True)
    runtime = DirectRuntime(model, logical_model="main", capability_gateway=backend,
                            harness_tool_gateway=shared)
    events = [event async for event in runtime.run(ctx)]
    assert [report["status"] for report in executor.reports] == ["failed", "passed"]
    delivery = mutable(events[-1].payload["workspace_delivery"])
    history = delivery["verification_history"]
    failed, passed = history["attempts"]
    assert failed["checks"][0]["phase"] == "test"
    assert failed["checks"][0]["execution_diagnostic"]["exit_code"] == 1
    assert failed["source_sha256"] == executor.reports[0]["source_sha256"]
    assert [check["phase"] for check in passed["checks"]] == ["install", "build", "test"]
    assert all(check["status"] == "passed"
               and check["execution_diagnostic"]["exit_code"] == 0 for check in passed["checks"])
    assert passed["source_sha256"] == executor.reports[-1]["source_sha256"]
    assert history["final_receipt"]["receipt_sha256"] == receipt_sha(delivery["verification"])
    assert history["repair_observed"] is True and history["truncated"] is False
    assert "PRIVATE_NODE_SOURCE" not in json.dumps(history)


@pytest.mark.parametrize("failure", ["unknown", "failed_payload", "missing", "boolean"])
async def test_trace_never_turns_unknown_evidence_into_completed_delivery(
    tmp_path: Path, failure: str,
) -> None:
    ctx = context()
    runtime, backend, _, _ = runner(tmp_path, ctx, Model(batch({"main.py": FIXED})),
                                    failure=failure)
    events: list[Any] = []
    with pytest.raises(RuntimeExecutionError, match="project verification"):
        async for event in runtime.run(ctx):
            events.append(event)
    assert all(event.kind is not EventKind.RUNTIME_COMPLETED for event in events)
    assert "workspace.bundle" not in backend.calls
