"""Historical Python proof uses service metadata, never a new execution grant."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.project_source_context import original_source_context
from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext
from tests.unit.capabilities.test_python_project_service import configured, observed, request


def api() -> Any:
    name = "agent_hub.runtime.python_project_history"
    assert importlib.util.find_spec(name) is not None, "pure Python history contract missing"
    return importlib.import_module(name)


async def history_case(
    tmp_path: Path, *, failure_index: int = 2
) -> tuple[Any, Any, Any, Any, TaskContext]:
    verifier, repository, executor, invocation, _ = await configured(tmp_path)
    exact = await verifier.authorization_request(request(repository, invocation))
    repository.approvals.add(capability_approval_scope(exact))
    executor.failure_index = failure_index
    baseline = await observed(verifier, repository, invocation)
    original = await verifier.python_original_context(invocation.tenant_id, invocation.run_id)
    assert original is not None
    from agent_hub.runs.project_inputs import python_original_source_index

    artifact = Artifact(
        id=uuid4(),
        type="original_project_observation",
        producer="original_project_preparer",
        content={
            "schema_version": 1,
            "original_source_sha256": original.stored.reference.input_source_sha256,
            "protected_sha256": original.inputs.contract.protected_sha256,
            "editable_paths": original.inputs.contract.editable_paths,
            "observation": baseline,
            "python_source_index": python_original_source_index(
                original.stored, source_limit_bytes=invocation.source_limit_bytes
            ),
            "source_context": cast(
                Mapping[str, JsonValue], original_source_context(original.stored, byte_budget=96000)
            ),
        },
    )
    metadata = api().build_python_history(original, artifact)
    context = TaskContext(
        run_id=invocation.run_id,
        tenant_id=invocation.tenant_id,
        mode=TaskMode.DISCUSS,
        request="repair",
        artifacts=(artifact,),
        routing_decision={
            **repository.record.routing_decision,
            "project_id": invocation.project_id,
            "workspace_session_id": invocation.session_id,
            "python_project_history": metadata,
        },
    )
    return verifier, repository, executor, original, context


async def test_metadata_is_path_free_bounded_and_has_no_install_authority(tmp_path: Path) -> None:
    _, _, _, original, context = await history_case(tmp_path)
    metadata = context.routing_decision["python_project_history"]
    raw = json.dumps(metadata, default=dict)
    assert str(original.stored.snapshot) not in raw and "install_allowed" not in raw
    assert api().python_history_selected(context)
    assert api().python_original_failed(context)
    assert len(raw) < 32768


@pytest.mark.parametrize(
    "defect", ["missing", "marker", "scope", "artifact", "observation", "runtime", "ref"]
)
async def test_damaged_or_cross_scope_history_never_credits_original_failure(
    tmp_path: Path, defect: str
) -> None:
    _, _, _, _, context = await history_case(tmp_path)
    route = json.loads(json.dumps(context.routing_decision, default=dict))
    metadata = route["python_project_history"]
    if defect == "missing":
        route.pop("python_project_history")
    elif defect == "marker":
        route["project_input_preparation"]["python_recipe"]["recipe_sha256"] = "0" * 64
    elif defect == "scope":
        metadata["scope"]["run_id"] = str(uuid4())
    elif defect == "artifact":
        metadata["artifact_sha256"] = "0" * 64
    elif defect == "observation":
        route["project_input_preparation"]["observation"]["verification"]["checks"][-1][
            "execution_diagnostic"
        ]["exit_code"] = 0
    elif defect == "runtime":
        metadata["runtime"]["binary_sha256"] = "0" * 64
    else:
        metadata["environment_references"][0]["environment_sha256"] = "0" * 64
    assert not api().python_original_failed(
        context.model_copy(update={"routing_decision": route}).validated_internal_clone()
    )


async def test_candidate_receipt_requires_independent_context_and_durable_environment(
    tmp_path: Path,
) -> None:
    verifier, _, executor, _, context = await history_case(tmp_path)
    executor.failure_index = None
    result = await verifier.verify(
        tenant_id=context.tenant_id, run_id=context.run_id, actor="engineer"
    )
    source = api().python_success_source(context, result)
    assert source == result["verification"]["source_sha256"]
    assert (
        api().python_success_source(context.model_copy(update={"routing_decision": {}}), result)
        is None
    )
    for key in ("recipe_sha256", "environment_input_sha256", "environment_sha256", "source_sha256"):
        receipt = {**result["verification"], key: "0" * 64}
        assert api().python_success_source(context, {**result, "verification": receipt}) is None


@pytest.mark.parametrize("mode", ["crew", "autogen"])
async def test_modes_forward_only_real_python_failure_diagnostic(tmp_path: Path, mode: str) -> None:
    _, _, _, original, context = await history_case(tmp_path)
    observation = original.observation
    payload = {
        "python_verification_failure": observation["verification"],
        "execution_diagnostic": observation["execution_diagnostic"],
    }
    module = importlib.import_module(f"agent_hub.runtime.{mode}.adapter")
    feedback = module._execution_failure_result(payload, tool_name="project.verify")
    assert (
        feedback is not None
        and feedback["python_verification_failure"] == observation["verification"]
    )
    assert api().python_failure_current(context, feedback)
    bad = {**payload, "execution_diagnostic": {**payload["execution_diagnostic"], "exit_code": 0}}
    assert module._execution_failure_result(bad, tool_name="project.verify") is None
    assert module._execution_failure_result(payload, tool_name="run_safe_command") is None


@pytest.mark.parametrize("kind", ["same", "size-only", "manifest", "readme", "source"])
async def test_retry_progress_is_productive_bytes_not_receipt_or_document_identity(
    tmp_path: Path, kind: str
) -> None:
    _, _, _, _, context = await history_case(tmp_path)
    known = api().python_initial_files(context)
    path, original = next(iter(known.items()))
    item = (path, original[1], original[2])
    if kind == "size-only":
        item = (path, original[1], original[2] + 1)
    elif kind == "manifest":
        item = ("DELIVERY_MANIFEST.json", "0" * 64, 10)
    elif kind == "readme":
        item = ("src/README.md", "0" * 64, 10)
    elif kind == "source":
        item = (path, hashlib.sha256(b"real repair").hexdigest(), 11)
    assert api().python_productive_change(context, item, known) is (kind == "source")


@pytest.mark.parametrize("recreated", [False, True])
async def test_new_root_sibling_proves_fresh_environment_without_self_expected(
    tmp_path: Path, recreated: bool
) -> None:
    verifier, _, executor, _, context = await history_case(
        tmp_path, failure_index=2 if recreated else 0
    )
    executor.origin = None
    executor.failure_index = None
    result = await verifier.verify(
        tenant_id=context.tenant_id, run_id=context.run_id, actor="engineer"
    )
    assert (
        api().python_success_source(
            context,
            {key: value for key, value in result.items() if key != "python_environment_reference"},
        )
        is None
    )
    trusted = await verifier.python_original_context(context.tenant_id, context.run_id)
    assert trusted is not None
    root = trusted.environment_references[-1]
    assert result["python_environment_reference"] == root.model_dump(mode="json")
    sibling = {**result, "python_environment_reference": root.model_dump(mode="json")}
    assert api().python_success_source(context, sibling) == result["verification"]["source_sha256"]
    bad = {**root.model_dump(mode="json"), "recipe_sha256": "0" * 64}
    assert (
        api().python_success_source(context, {**sibling, "python_environment_reference": bad})
        is None
    )
    assert api().python_retry_digest(context, {}) == api().python_retry_digest(
        context, {"DELIVERY_MANIFEST.json": "0" * 64}
    )


async def test_history_projects_service_128_reference_budget_without_issuance_noise(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    _, _, _, original, context = await history_case(tmp_path)
    reference = original.environment_references[0]
    refs = tuple(
        reference.model_copy(update={"origin_execution_id": f"candidate-{index}"})
        for index in range(128)
    )
    history = api().build_python_history(
        replace(original, environment_references=refs), context.artifacts[0]
    )
    assert len(history["environment_references"]) == 128
    with pytest.raises(ValueError, match="python_project_history_budget_exceeded"):
        api().build_python_history(
            replace(original, environment_references=(*refs, reference)), context.artifacts[0]
        )


@pytest.mark.parametrize("mode", ["crew", "autogen"])
@pytest.mark.parametrize(
    "defect", [None, "scope", "recipe", "bundle-source", "bundle-receipt", "artifact", "order"]
)
async def test_python_delivery_requires_trusted_causal_root_and_bundle(
    tmp_path: Path, mode: str, defect: str | None
) -> None:
    verifier, _, executor, _, context = await history_case(tmp_path, failure_index=0)
    executor.failure_index = None
    value = await verifier.verify(
        tenant_id=context.tenant_id, run_id=context.run_id, actor="engineer"
    )
    trusted = await verifier.python_original_context(context.tenant_id, context.run_id)
    ref = trusted.environment_references[-1].model_dump(mode="json")
    if defect == "scope":
        ref["scope"]["run_id"] = str(uuid4())
    elif defect == "recipe":
        ref["recipe_sha256"] = "0" * 64
    value = {**value, "python_environment_reference": ref}
    receipt = value["verification"]
    digest = hashlib.sha256(
        json.dumps(
            receipt, default=dict, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()
    if mode == "crew":
        from agent_hub.runtime.crew.engineering_delivery import engineering_delivery_ready
        from tests.unit.runtime.crew.test_engineering_delivery_gate import change_result, fixture

        data = fixture()
        change_result(data, "1", value)
        bundle = {
            **cast(Mapping[str, JsonValue], data[1]["2"].content["result"]),
            "original_check_contract": value["original_check_contract"],
            "verification_source_sha256": receipt["source_sha256"],
            "verification_receipt_sha256": digest,
        }
        if defect == "bundle-source":
            bundle["verification_source_sha256"] = "0" * 64
        elif defect == "bundle-receipt":
            bundle["verification_receipt_sha256"] = "0" * 64
        change_result(data, "2", bundle)
        if defect == "artifact":
            data[0]["1"] = {**data[0]["1"], "sha256": "0" * 64}
        elif defect == "order":
            data[0]["1"] = {**data[0]["1"], "round": 3}
        ready = engineering_delivery_ready(
            step_id="build", actor="implementer", states=data[0], artifacts=data[1], context=context
        )
    else:
        from agent_hub.runtime.autogen.engineering_delivery import (
            engineering_delivery_ready as native_ready,
        )
        from tests.unit.runtime.autogen.test_engineering_delivery import (
            TOOLS,
            replace_result,
        )
        from tests.unit.runtime.autogen.test_engineering_delivery import (
            fixture as native_fixture,
        )

        rows, artifacts = native_fixture()
        for row in rows:
            row["idempotency_key"] = hashlib.sha256(
                f"{context.run_id}:engineer:{row['position']}:{row['name']}:{'c' * 64}".encode()
            ).hexdigest()
        replace_result(rows, artifacts, 1, value)
        bundle = {
            **cast(Mapping[str, JsonValue], artifacts[2].content["result"]),
            "original_check_contract": value["original_check_contract"],
            "verification_source_sha256": receipt["source_sha256"],
            "verification_receipt_sha256": digest,
        }
        if defect == "bundle-source":
            bundle["verification_source_sha256"] = "0" * 64
        elif defect == "bundle-receipt":
            bundle["verification_receipt_sha256"] = "0" * 64
        replace_result(rows, artifacts, 2, bundle)
        if defect == "artifact":
            rows[1]["artifact_sha256"] = "0" * 64
        elif defect == "order":
            rows.reverse()
        ready = native_ready(
            run_id=context.run_id,
            tool_entries=rows,
            artifacts=artifacts,
            allowed_tools=TOOLS,
            context=context,
        )
    assert ready is (defect is None)


@pytest.mark.parametrize("mode", ["crew", "autogen"])
@pytest.mark.parametrize("kind", ["same", "manifest", "readme", "source", "new"])
async def test_python_modes_do_not_clear_failed_credit_for_cosmetic_write(
    tmp_path: Path, mode: str, kind: str
) -> None:
    _, _, _, _, context = await history_case(tmp_path)
    initial = next(iter(api().python_initial_files(context).values()))
    item = {"path": initial[0], "sha256": initial[1], "size_bytes": initial[2]}
    if kind != "same":
        item.update(sha256="0" * 64, size_bytes=10)
        item["path"] = {"manifest": "DELIVERY_MANIFEST.json", "readme": "src/README.md"}.get(
            kind, initial[0]
        )
        if kind == "new":
            item["path"] = "src/new_helper.py"
    result = {"workspace_files": (item,)}
    digest = "c" * 64
    artifact = Artifact(
        id=uuid4(),
        type="tool_result",
        producer="engineer",
        source_ids=(str(uuid4()),),
        content={
            "agent_id": "engineer",
            "tool_name": "workspace.write_text",
            "arguments_sha256": digest,
            "result": result,
        },
    )
    if mode == "crew":
        from agent_hub.runtime.crew.engineering_delivery import engineering_action_progress

        progress = engineering_action_progress(
            context=context,
            actor="engineer",
            step_id="build",
            attempt=0,
            round_index=1,
            states={
                "w": {
                    "attempt": 0,
                    "round": 0,
                    "tool_index": 0,
                    "step_id": "build",
                    "status": "succeeded",
                    "name": "workspace.write_text",
                    "arguments_sha256": digest,
                    "artifact_id": str(artifact.id),
                    "sha256": artifact.content_sha256,
                }
            },
            artifacts={"w": artifact},
        )
    else:
        from agent_hub.runtime.autogen.engineering_delivery import engineering_progress

        progress = engineering_progress(
            context,
            tool_entries=(
                {
                    "position": 0,
                    "actor": "engineer",
                    "name": "workspace.write_text",
                    "arguments_hash": digest,
                    "status": "succeeded",
                    "idempotency_key": hashlib.sha256(
                        f"{context.run_id}:engineer:0:workspace.write_text:{digest}".encode()
                    ).hexdigest(),
                    "artifact_id": str(artifact.id),
                    "artifact_sha256": artifact.content_sha256,
                },
            ),
            artifacts=(artifact,),
            allowed_tools={"engineer": ("workspace.write_text",)},
        )
    assert not progress.uncertain
    assert progress.verification_failed_current is (kind not in {"source", "new"})


async def test_preparer_attaches_independent_bounded_history_without_new_execution(
    tmp_path: Path,
) -> None:
    from tests.unit.runs.test_python_original_preparation import configured

    preparer, repository, context, executor, authorizer, _, _ = await configured(tmp_path)
    authorizer.allowed = True
    prepared = await preparer(context)
    assert api().python_original_failed(prepared)
    assert len(executor.calls) == 1
    resumed = await preparer(
        context.model_copy(update={"routing_decision": repository.row.routing_decision})
    )
    assert api().python_original_failed(resumed)
    assert len(executor.calls) == 1


@pytest.mark.parametrize("outcome", ["passed", "failed", "no-proof", "wrong-call", "truncated"])
async def test_direct_consumes_only_bound_python_tool_result(tmp_path: Path, outcome: str) -> None:
    import asyncio

    from agent_hub.harness.types import HarnessToolCallResult
    from agent_hub.runtime.direct import (
        DirectRuntime,
        _WorkspaceVerificationError,
        _WorkspaceVerificationFailure,
    )

    verifier, _, executor, original, context = await history_case(tmp_path)
    executor.failure_index = None
    payload = await verifier.verify(
        tenant_id=context.tenant_id, run_id=context.run_id, actor="engineer"
    )
    if outcome in {"failed", "no-proof"}:
        payload = {
            "python_verification_failure": original.observation["verification"],
            "execution_diagnostic": original.observation["execution_diagnostic"],
        }
        if outcome == "no-proof":
            payload.pop("python_verification_failure")

    class Gateway:
        async def invoke(self, tenant: Any, request: Any, **kwargs: Any) -> HarnessToolCallResult:
            return HarnessToolCallResult(
                call_id="wrong" if outcome == "wrong-call" else request.call_id,
                tool_name="project.verify",
                status="failed" if outcome in {"failed", "no-proof"} else "succeeded",
                payload=payload,
                failure_reason="python_project_checks_failed" if outcome == "failed" else None,
                truncated=outcome == "truncated",
            )

    runtime = DirectRuntime(
        cast(Any, object()), logical_model="fixture", harness_tool_gateway=cast(Any, Gateway())
    )
    call = runtime._verify_workspace(
        context, known_files={}, attempt=1, deadline=asyncio.get_running_loop().time() + 10
    )
    if outcome == "passed":
        assert (await call)["schema_version"] == 2
    else:
        with pytest.raises(
            _WorkspaceVerificationFailure if outcome == "failed" else _WorkspaceVerificationError
        ):
            await call


async def test_direct_semantic_retry_ignores_docs_same_bytes_and_root_issuance(
    tmp_path: Path,
) -> None:
    from agent_hub.runtime.direct import _workspace_source_digest

    _, _, _, _, context = await history_case(tmp_path)
    known = {path: item[1] for path, item in api().python_initial_files(context).items()}
    baseline = _workspace_source_digest({}, context)
    assert baseline == _workspace_source_digest({**known, "src/README.md": "0" * 64}, context)
    assert baseline == _workspace_source_digest(
        {**known, "DELIVERY_MANIFEST.json": "0" * 64}, context
    )
    route = json.loads(json.dumps(context.routing_decision, default=dict))
    route["python_project_history"]["environment_references"][0]["origin_execution_id"] = (
        "new-root-issuance"
    )
    assert baseline == _workspace_source_digest(
        known, context.model_copy(update={"routing_decision": route}).validated_internal_clone()
    )
    known[next(iter(known))] = "0" * 64
    assert baseline != _workspace_source_digest(known, context)


@pytest.mark.parametrize("mode", ["crew", "autogen"])
async def test_fresh_candidate_later_failure_keeps_independent_root_evidence(
    tmp_path: Path, mode: str
) -> None:
    from agent_hub.capabilities.project_verification import ProjectVerificationError

    verifier, _, executor, _, context = await history_case(tmp_path, failure_index=0)
    executor.failure_index = 2
    with pytest.raises(ProjectVerificationError) as caught:
        await verifier.verify(tenant_id=context.tenant_id, run_id=context.run_id, actor="engineer")
    failure = caught.value.python_project_failure
    assert (
        failure is not None
        and failure.environment_reference is not None
        and failure.diagnostic is not None
    )
    payload = {
        "python_verification_failure": failure.to_payload(),
        "execution_diagnostic": failure.diagnostic.to_payload(),
    }
    assert not api().python_failure_current(context, payload)
    payload["python_environment_reference"] = failure.environment_reference.model_dump(mode="json")
    feedback = importlib.import_module(
        f"agent_hub.runtime.{mode}.adapter"
    )._execution_failure_result(payload, tool_name="project.verify")
    assert feedback is not None and api().python_failure_current(context, feedback)
    assert feedback["python_environment_reference"] == payload["python_environment_reference"]


@pytest.mark.parametrize("defect", [None, "receipt", "source", "root"])
async def test_direct_terminal_fresh_receipt_keeps_root_sibling_and_bundle_binding(
    tmp_path: Path, defect: str | None
) -> None:
    from agent_hub.runtime.direct import _original_project_terminal_artifact_matches

    verifier, _, executor, _, context = await history_case(tmp_path, failure_index=0)
    executor.failure_index = None
    result = await verifier.verify(
        tenant_id=context.tenant_id, run_id=context.run_id, actor="engineer"
    )
    receipt = result["verification"]
    delivery = {
        **result,
        "presentation": "final_attachment",
        "file": {"filename": "python.zip"},
        "verification_source_sha256": receipt["source_sha256"],
        "verification_receipt_sha256": hashlib.sha256(
            json.dumps(
                receipt, default=dict, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
    }
    if defect == "receipt":
        delivery["verification_receipt_sha256"] = "0" * 64
    elif defect == "source":
        delivery["verification_source_sha256"] = "0" * 64
    elif defect == "root":
        delivery.pop("python_environment_reference")
    artifact = Artifact(
        id=uuid4(), type="tool_result", producer="main", content={"workspace_delivery": delivery}
    )
    assert _original_project_terminal_artifact_matches(context, artifact) is (defect is None)


@pytest.mark.parametrize("mode", ["crew", "autogen"])
async def test_python_long_progress_tail_reaches_repair_provider_without_second_prefix_clip(
    tmp_path: Path,
    mode: str,
) -> None:
    """Transport proof only: fake provider/backend, no environment or code execution."""
    from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
    from agent_hub.capabilities.runtime import RuntimeCapabilityError
    from agent_hub.runtime.contracts import EventKind
    from agent_hub.skills.sandbox.base import SkillResult
    from tests.unit.capabilities.test_python_project_reports import decode, fail, fixture
    from tests.unit.runtime.crew.test_execution_diagnostic_continuation import (
        Policy,
        ProcessBackend,
        RepairModel,
        crew,
    )
    from tests.unit.runtime.crew.test_execution_diagnostic_continuation import (
        context as transport_context,
    )

    text = (
        "progress: processing wheel\n" * 900
        + "token=TAIL_SECRET\nActualFailure: expected 12 got 6\n"
    )
    native = SkillResult(exit_code=1, timed_out=False, stdout="", stderr=text)
    prefix = ExecutionDiagnostic(backend="systemd", result=native).to_payload()
    assert "ActualFailure" not in cast(str, prefix["stderr"])
    captured = fixture()
    fail(captured[3], 2, stderr=text, stderr_observed_bytes=len(text.encode()))
    report = decode(
        importlib.import_module("agent_hub.capabilities.python_project_reports"), captured
    )
    diagnostic = report.diagnostic
    assert diagnostic is not None
    retained = diagnostic.to_payload()
    assert retained["stderr_metadata"] == prefix["stderr_metadata"]
    assert "ActualFailure" in retained["stderr"] and "TAIL_SECRET" not in retained["stderr"]
    assert len(retained["stderr"].encode()) <= 4096
    failure_payload = {
        "python_verification_failure": report.to_payload(),
        "execution_diagnostic": retained,
    }
    assert api().python_failure_feedback(failure_payload, tool_name="project.verify") is not None
    assert (
        api().python_failure_feedback(
            {
                **failure_payload,
                "execution_diagnostic": {
                    **retained,
                    "stderr": retained["stderr"].replace("[truncated]", "[discarded]"),
                },
            },
            tool_name="project.verify",
        )
        is None
    )
    assert (
        api().python_failure_feedback(
            {
                **failure_payload,
                "execution_diagnostic": {**retained, "stderr": retained["stderr"][:-1]},
            },
            tool_name="project.verify",
        )
        is None
    )

    class MemoryBackend(ProcessBackend):
        def __init__(self) -> None:
            self.root, self.calls, self.results = tmp_path, [], []

        async def execute(self, *, name: str, arguments: Mapping[str, Any], **kwargs: Any) -> Any:
            self.calls.append(name)
            if name == "workspace.read":
                return {"path": "main.py", "text": "fixture source", "truncated": False}
            if name == "workspace.write_text":
                return {"path": "main.py", "summary": "fixture write acknowledged"}
            assert name == "project.verify" and arguments == {}
            if not self.results:
                self.results.append(native)
                raise RuntimeCapabilityError(
                    "python_project_checks_failed",
                    execution_diagnostic=diagnostic,
                    python_project_failure=report,
                )
            return {"verified": True}

    class TailModel(RepairModel):
        async def complete_with_context(self, request: Any) -> Any:
            selector = self.autogen and request.max_output_tokens == 512
            if self.stage == 1 and not selector:
                observations = "\n".join(
                    message.content
                    for message in request.messages
                    if message.role in {"user", "tool"} and isinstance(message.content, str)
                )
                assert "ActualFailure: expected 12 got 6" in observations
                assert "python_verification_failure" in observations
                assert "TAIL_SECRET" not in observations
            return await super().complete_with_context(request)

    model, backend = TailModel(autogen=mode == "autogen"), MemoryBackend()
    context = transport_context(TaskMode.DISCUSS if mode == "autogen" else TaskMode.DISPATCH)
    policy = Policy(context)
    if mode == "autogen":
        from tests.unit.runtime.autogen.test_execution_diagnostic_continuation import runtime

        runner = runtime(model, backend, policy)
    else:
        runner = crew(model, backend, policy)
    events = [event async for event in runner.run(context)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert model.stage == 5
    assert backend.calls == [
        "project.verify",
        "workspace.read",
        "workspace.write_text",
        "project.verify",
    ]


async def test_acknowledged_new_editable_python_file_is_semantic_progress(tmp_path: Path) -> None:
    _, _, _, _, context = await history_case(tmp_path)
    known = api().python_initial_files(context)
    item = ("src/new_helper.py", hashlib.sha256(b"def helper(): return 12\n").hexdigest(), 24)
    assert api().python_productive_change(context, item, known)
    baseline = api().python_retry_digest(context, {})
    assert api().python_retry_digest(context, {item[0]: item[1]}) != baseline
    assert not api().python_productive_change(context, item, {**known, item[0]: item})


async def test_prompt_omission_does_not_erase_trusted_baseline_source_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_hub.runs.project_inputs as preparation
    from tests.unit.runs.test_python_original_preparation import configured

    preparer, _, context, _, authorizer, _, _ = await configured(tmp_path)
    authorizer.allowed = True
    monkeypatch.setattr(
        preparation,
        "original_source_context",
        lambda stored, **kwargs: {
            "files": (),
            "file_count": stored.reference.file_count,
            "included_count": 0,
            "truncated": True,
        },
    )
    prepared = await preparer(context)
    initial = api().python_initial_files(prepared)
    assert initial and "src/billing/__init__.py" in initial
    item = initial["src/billing/__init__.py"]
    assert not api().python_productive_change(prepared, item, {})
    assert api().python_retry_digest(prepared, {item[0]: item[1]}) == api().python_retry_digest(
        prepared, {}
    )


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "src/../outside.py",
        "/host.py",
        "src\\alias.py",
        "src2/outside.py",
        "tests/protected.py",
        "src/README.md",
        "DELIVERY_MANIFEST.json",
        "src/.hidden.py",
    ],
)
async def test_new_file_progress_does_not_accept_arbitrary_model_paths(
    tmp_path: Path, path: str
) -> None:
    _, _, _, _, context = await history_case(tmp_path)
    item = (path, "0" * 64, 10)
    assert not api().python_productive_change(context, item, {})
    assert api().python_retry_digest(context, {path: item[1]}) == api().python_retry_digest(
        context, {}
    )


async def test_new_file_is_recorded_as_repair_in_direct_trace(tmp_path: Path) -> None:
    from agent_hub.harness.types import HarnessToolCallResult
    from agent_hub.runtime.direct import _WorkspaceVerificationTrace

    verifier, _, executor, original, context = await history_case(tmp_path)
    trace = _WorkspaceVerificationTrace(context=context)
    failure = HarnessToolCallResult(
        call_id="first",
        tool_name="project.verify",
        status="failed",
        payload={
            "python_verification_failure": original.observation["verification"],
            "execution_diagnostic": original.observation["execution_diagnostic"],
        },
        failure_reason="python_project_checks_failed",
    )
    trace.record(failure, known_files={}, attempt=1)
    executor.failure_index = None
    passed = await verifier.verify(
        tenant_id=context.tenant_id, run_id=context.run_id, actor="engineer"
    )
    trace.record(
        HarnessToolCallResult(
            call_id="second", tool_name="project.verify", status="succeeded", payload=passed
        ),
        known_files={"src/new_helper.py": "0" * 64},
        attempt=2,
    )
    assert trace.repair_observed
    assert cast(Mapping[str, JsonValue], trace.attempts[-1])["written_file_count"] == 1


async def test_source_index_uses_captured_hashes_and_explicit_budget(tmp_path: Path) -> None:
    from dataclasses import replace

    from agent_hub.runs.project_inputs import python_original_source_index

    _, _, _, original, context = await history_case(tmp_path)
    with pytest.raises(
        RuntimeError, match="original project preparation is unavailable or changed"
    ):
        python_original_source_index(original.stored, source_limit_bytes=1)
    bad = dict(original.stored.source_files)
    path = next(path for path in bad if path.endswith(".py"))
    bad[path] = "0" * 64
    from agent_hub.capabilities.project_baselines import _fingerprint

    reference = replace(original.stored.reference, input_source_sha256=_fingerprint(bad))
    changed = replace(original.stored, reference=reference, source_files=bad)
    with pytest.raises(
        RuntimeError, match="original project preparation is unavailable or changed"
    ):
        python_original_source_index(
            changed, source_limit_bytes=original.invocation.source_limit_bytes
        )
    artifact = context.artifacts[0]
    missing = Artifact(
        id=artifact.id,
        type=artifact.type,
        producer=artifact.producer,
        content={
            key: value for key, value in artifact.content.items() if key != "python_source_index"
        },
    )
    with pytest.raises(
        ValueError, match="python_project_history_invalid|python_project_report_invalid"
    ):
        api().build_python_history(original, missing)
