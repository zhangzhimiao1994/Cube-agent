"""Offline final-report boundaries through native verification, bundles and Crew parser."""
from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, ToolCall
from agent_hub.runtime import direct, direct_delivery_report, hybrid_delivery_report
from agent_hub.runtime.artifacts import InMemoryArtifactRepository
from agent_hub.runtime.contracts import EventKind
from agent_hub.runtime.crew.adapter import RuntimeExecutionError as CrewRuntimeExecutionError
from agent_hub.runtime.crew.delivery_report import final_evidence
from agent_hub.runtime.direct import DirectRuntime, RuntimeExecutionError
from agent_hub.runtime.hybrid import HybridRuntime
from tests.unit.capabilities.test_project_verification import Executor
from tests.unit.runtime.autogen.test_delivery_report import fixture as discussion_fixture
from tests.unit.runtime.autogen.test_delivery_report import replace_model
from tests.unit.runtime.crew.test_adapter_failure_reason import WorkspaceDeliverySequenceGateway
from tests.unit.runtime.crew.test_delivery_report import context as crew_context
from tests.unit.runtime.crew.test_delivery_report import responses, subject
from tests.unit.runtime.crew.test_engineering_delivery_execution import ActualCapabilityHarness
from tests.unit.runtime.crew.test_framework_parser_boundary import (
    installed_parser,  # noqa: F401
    offline_only,  # noqa: F401
)
from tests.unit.runtime.test_direct_final_report import report_context
from tests.unit.runtime.test_direct_project_verification import FIXED, Model, batch, runner
from tests.unit.runtime.test_hybrid import MultiArtifactRuntime, artifact

PLAIN = "已交付项目 ZIP。验证已通过，使用方法见 README；部署尚未验证。请反馈使用情况。"
FENCED = PLAIN + '\n\nJSON 输入示例：\n```json\n{"name": "张三", "count": 2}\n```\n\n此处仅演示用户数据格式。'
INLINE = PLAIN + '\n\nJSON 输入示例：`{"name": "张三", "count": 2}`。'
READ_EXAMPLE = PLAIN + '\n\n只读协议示例：\n```json\n{"read_paths":["README.md"]}\n```'
BATCH_EXAMPLE = PLAIN + '\n\n批次协议示例：\n```json\n' + json.dumps({
    "workspace_batch": {"files": {"example.txt": "Do not write this file."},
                        "read_paths": ["README.md"], "complete": False,
                        "continuation": "Only an example."},
}) + '\n```'
CATALOG_EXAMPLE = PLAIN + '\n\n目录协议示例：`{"python_catalog":{"query":"example"}}`。'
BUNDLE_EXAMPLE = PLAIN + '\n\n文件协议示例：\n```json\n' + json.dumps({
    "workspace_bundle": {"files": {"example.txt": "Do not write this file."}},
}) + '\n```'
CASES = [
    pytest.param(PLAIN, True, id="plain-markdown"),
    pytest.param(FENCED, True, id="chinese-with-fenced-json-example"),
    pytest.param(INLINE, True, id="chinese-with-inline-json-example"),
    pytest.param('{"result": "done"}', False, id="whole-body-json-object"),
    pytest.param('```json\n{"result": "done"}\n```', False, id="whole-body-fenced-json"),
    pytest.param('[{"result": "done"}]', False, id="whole-body-json-array"),
    pytest.param('```json\n{"name":"张三"}\n```\n\n' + PLAIN,
                 True, id="opening-json-fence-followed-by-prose"),
    pytest.param('[使用说明](README.md)\n\n' + PLAIN, True, id="opening-markdown-link"),
    pytest.param(READ_EXAMPLE, True, id="read-paths-are-only-an-example"),
    pytest.param(BATCH_EXAMPLE, True, id="workspace-batch-is-only-an-example"),
    pytest.param(CATALOG_EXAMPLE, True, id="python-catalog-is-only-an-example"),
    pytest.param(BUNDLE_EXAMPLE, True, id="workspace-bundle-is-only-an-example"),
    pytest.param('[]', False, id="whole-body-empty-json-array"),
    pytest.param('```\n[1,2]\n```', False, id="whole-body-unlabelled-json-fence"),
]


@pytest.mark.parametrize("text,accepted", CASES)
def test_discussion_current_report_and_cold_binding_use_whole_body_rule(text, accepted):
    evidence = discussion_fixture()
    replace_model(evidence, {"text": text})
    if not accepted:
        with pytest.raises(ValueError, match="final report policy or text is invalid"):
            evidence.metadata(text=text)
        return
    metadata = evidence.metadata(text=text)
    report = evidence.report(metadata=metadata, text=text)
    assert evidence.valid(type(report).from_payload(report.to_payload()))


@pytest.mark.parametrize("defect", [None, "path", "source", "execution", "size"])
def test_final_model_receives_actual_product_name_not_private_storage(defect):
    product = {"filename": "receipt-1.0-py3-none-any.whl", "sha256": "a" * 64,
               "size": 123, "source_sha256": "b" * 64, "execution_id": "python_build"}
    verification = {"source_sha256": "b" * 64, "execution_id": "python_build"}
    delivery = {"python_product": product, "python_product_path": "dist/" + product["filename"],
                "private_storage_key": "DO_NOT_EXPOSE", "arbitrary": "IGNORE"}
    if defect == "path":
        delivery["python_product_path"] = "/host/private.whl"
    elif defect in {"source", "execution"}:
        product["source_sha256" if defect == "source" else "execution_id"] = "changed"
    elif defect == "size":
        product["size"] = True
    if defect:
        with pytest.raises(ValueError):
            direct_delivery_report.public_product_evidence(delivery, verification)
    else:
        observed = direct_delivery_report.public_product_evidence(delivery, verification)
        assert observed == {"python_product": {key: value for key, value in product.items()
            if key in {"filename", "sha256", "size"}} | {"archive_path": delivery["python_product_path"]}}
        assert "DO_NOT_EXPOSE" not in json.dumps(observed)


def assert_zip(folder: Path):
    archives = list(folder.rglob("*.zip"))
    assert archives, "must reach real workspace.bundle, not a parser-only mirror"
    for path in archives:
        with zipfile.ZipFile(path) as bundle:
            assert bundle.testzip() is None
            assert bundle.namelist()


@pytest.mark.parametrize("text,accepted", CASES)
async def test_direct_native_verified_bundle_report(tmp_path, text, accepted):
    ctx = report_context()
    model = Model(batch({"main.py": FIXED, "README.md": "Run main.py.\n"}), text)
    runtime, backend, shared, _ = runner(tmp_path, ctx, model)
    events, error = [], None
    try:
        async for event in runtime.run(ctx):
            events.append(event)
    except RuntimeExecutionError as caught:
        error = str(caught)
    # These preconditions must pass BEFORE an expected RED report assertion.
    assert backend.calls[-2:] == ["project.verify", "workspace.bundle"]
    assert backend.process_results[-1].exit_code == 0
    assert len(backend.process_results) == 1
    assert "F01_PASS" in backend.process_results[-1].stdout
    assert shared.results[-1].status == "succeeded"
    assert len(model.requests) == 2 and not model.texts
    request = model.requests[-1]
    assert not request.tools and request.required_tool is None and request.response_schema is None
    assert request.timeout_seconds <= ctx.timeout_seconds
    assert request.max_output_tokens <= ctx.token_budget - model.usage
    inputs = "\n".join(str(message.content) for message in request.messages)
    evidence = json.loads(inputs.split("<UNTRUSTED_VERIFIED_DELIVERY_JSON>", 1)[1].split("</", 1)[0])
    assert evidence["verification"]["status"] == "passed"
    assert len(evidence["verification"]["source_sha256"]) == 64
    assert_zip(tmp_path / "generated")
    print(f"DIRECT native verify+zip, calls=2, report_error={error!r}")
    if not accepted:
        assert error == "direct final report is invalid (reason=json_only)"
        assert not any(e.kind is EventKind.RUNTIME_COMPLETED for e in events)
        return
    assert error is None, f"Markdown report rejected AFTER native verification+bundle: {error}"
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    report = next(e.artifact for e in reversed(events) if e.artifact is not None and e.artifact.producer == "main")
    assert report.content["text"] == text and direct_delivery_report.valid(ctx, report)


@pytest.mark.parametrize("mode", ["direct", "hybrid"])
@pytest.mark.parametrize("text", [READ_EXAMPLE, BATCH_EXAMPLE, CATALOG_EXAMPLE, BUNDLE_EXAMPLE],
                         ids=["read-paths", "workspace-batch", "python-catalog", "workspace-bundle"])
async def test_bound_main_never_parses_or_executes_example_instructions(
    tmp_path, installed_parser, monkeypatch, mode, text,  # noqa: F811
):
    def forbidden(*args, **kwargs):
        pytest.fail("bound final report reached generation parsing or a capability")

    def files():
        return {path.relative_to(tmp_path): path.read_bytes()
                for path in tmp_path.rglob("*") if path.is_file()}

    class BoundaryModel(Model):
        async def complete_with_context(self, request):
            completion = await super().complete_with_context(request)
            if not self.texts:
                self.before = files()
                # Install only after native generation/verify/bundle. No final
                # report may re-enter these parsers, even without a gateway.
                for name in ("_json_mapping_from_model_text", "_workspace_batch_from_model_text",
                             "_project_scale_workspace_bundle_from_model_text",
                             "_website_preview_workspace_bundle_from_model_text"):
                    monkeypatch.setattr(direct, name, forbidden)
                monkeypatch.setattr(backend, "execute", forbidden)
            return completion

    if mode == "direct":
        ctx = report_context()
        reporter = BoundaryModel(batch({"main.py": FIXED}), text)
        runtime, backend, _, _ = runner(tmp_path, ctx, reporter)
    else:
        ctx = crew_context()
        repository = InMemoryArtifactRepository()
        harness = ActualCapabilityHarness(tmp_path, Executor())
        crew = subject(WorkspaceDeliverySequenceGateway(responses()), harness, repository, installed_parser)
        created = [event.artifact async for event in crew.run(ctx) if event.artifact is not None]
        _, required = hybrid_delivery_report.delivery(ctx, created, output_ids={item.id for item in created})
        ctx = ctx.model_copy(update={"mode": TaskMode.DIRECT, "artifacts": required,
            "routing_decision": {**ctx.routing_decision, "engineering_hybrid_report_version": 1,
                hybrid_delivery_report.INPUT: {str(item.id): item.content_sha256 for item in required}}})
        backend = harness.backend
        reporter = BoundaryModel(text)
        runtime = DirectRuntime(reporter, logical_model="main")
    events = [event async for event in runtime.run(ctx)]
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(reporter.requests) == (2 if mode == "direct" else 1)
    assert not reporter.requests[-1].tools
    assert reporter.before == files()
    report = next(event.artifact for event in reversed(events)
                  if event.artifact is not None and event.artifact.producer == "main")
    assert report.content["text"] == text and direct_delivery_report.valid(ctx, report)


@pytest.mark.parametrize("text,accepted", CASES)
async def test_crew_native_verified_bundle_report(tmp_path, installed_parser, text, accepted):  # noqa: F811
    ctx = crew_context()
    executor = Executor()
    harness = ActualCapabilityHarness(tmp_path, executor)
    model = WorkspaceDeliverySequenceGateway(responses(text))
    runtime = subject(model, harness, InMemoryArtifactRepository(), installed_parser)
    events, error = [], None
    try:
        async for event in runtime.run(ctx):
            events.append(event)
    except CrewRuntimeExecutionError as caught:
        error = str(caught)
    assert [c.tool_name for c in harness.calls][-2:] == ["project.verify", "workspace.bundle"]
    assert len(executor.calls) == 1 and len(model.requests) == 5
    assert not model.requests[-1].tools
    assert_zip(tmp_path / "artifacts")
    print(f"CREW native verify+zip, calls=5, report_error={error!r}")
    if not accepted:
        assert error and "delivery report" in error
        assert not any(e.kind is EventKind.RUNTIME_COMPLETED for e in events)
        return
    assert error is None, f"Crew rejected Markdown example: {error}"
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    created = tuple(e.artifact for e in events if e.artifact is not None)
    report = next(a for a in created if a.type == "text" and a.producer == "reporter")
    assert report.content["text"] == text
    facts = final_evidence(report, ctx, created)
    assert facts["verification"]["verification"]["status"] == "passed"
    assert facts["bundle"]["presentation"] == "final_attachment"


@pytest.mark.parametrize("text,accepted", CASES)
async def test_hybrid_native_verified_bundle_main_report(tmp_path, installed_parser, text, accepted):  # noqa: F811
    base = crew_context()
    ctx = base.model_copy(update={"mode": TaskMode.HYBRID, "routing_decision": {
        **base.routing_decision, "engineering_delivery_contract_version": 1,
        "engineering_hybrid_report_version": 1}})
    repo = InMemoryArtifactRepository()
    executor = Executor()
    harness = ActualCapabilityHarness(tmp_path, executor)
    crew_model = WorkspaceDeliverySequenceGateway(responses())
    reporter = Model(text)
    runtime = HybridRuntime(
        subject(crew_model, harness, repo, installed_parser),
        MultiArtifactRuntime(TaskMode.DISCUSS, (artifact("reviewer", "Reviewed."),)),
        DirectRuntime(reporter, logical_model="main"), artifact_repository=repo)
    events = [event async for event in runtime.run(ctx)]
    assert [c.tool_name for c in harness.calls][-2:] == ["project.verify", "workspace.bundle"]
    assert len(executor.calls) == 1 and len(crew_model.requests) == 5
    assert len(reporter.requests) == 1 and not reporter.texts
    assert not reporter.requests[0].tools
    assert "UNTRUSTED_VERIFIED_DELIVERY_JSON" in "\n".join(str(m.content) for m in reporter.requests[0].messages)
    assert_zip(tmp_path / "artifacts")
    failed = [e for e in events if e.kind is EventKind.RUNTIME_FAILED]
    print(f"HYBRID native verify+zip, final calls=1, failure={[e.reason for e in failed]}")
    if not accepted:
        assert failed and failed[-1].reason == (
            "hybrid direct failed: direct final report is invalid (reason=json_only)")
        assert not any(e.kind is EventKind.RUNTIME_COMPLETED for e in events)
        return
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED, f"Hybrid rejected Markdown after native verify+bundle: {[e.reason for e in failed]}"
    report = next(e.artifact for e in reversed(events) if e.artifact is not None and e.artifact.producer == "main")
    assert report.content["text"] == text and direct_delivery_report.valid(ctx, report)


@pytest.mark.parametrize("reason", ["completion_missing", "tool_calls", "empty_text", "json_only"])
async def test_direct_final_rejection_has_finite_reason_without_retry(tmp_path, reason):
    class RejectedReport(Model):
        async def complete_with_context(self, request):
            completion = await super().complete_with_context(request)
            if len(self.requests) != 2:
                return completion
            if reason == "completion_missing":
                return None
            response = completion.response
            if reason == "tool_calls":
                response = replace(response, tool_calls=(ToolCall(
                    id="private-key", name="read_context", arguments={"path": "private-path"}),))
            elif reason == "empty_text":
                response = replace(response, text=" \n\t")
            else:
                response = replace(response, text='{"private-key":"private-provider-text"}')
            return replace(completion, response=response)

    ctx = report_context()
    model = RejectedReport(batch({"main.py": FIXED}), PLAIN)
    runtime, backend, _, _ = runner(tmp_path, ctx, model)
    events = []
    with pytest.raises(RuntimeExecutionError) as error:
        async for event in runtime.run(ctx):
            events.append(event)
    assert str(error.value) == f"direct final report is invalid (reason={reason})"
    assert len(model.requests) == 2 and not model.texts
    assert backend.calls[-2:] == ["project.verify", "workspace.bundle"]
    assert len(backend.process_results) == 1
    assert not any(event.kind is EventKind.RUNTIME_COMPLETED for event in events)


def test_binding_rejection_uses_finite_reason():
    ctx = report_context()
    delivery = {"verification": {"status": "passed"}, "file": {"filename": "project.zip"},
                "artifact_id": "native-bundle"}
    request = ModelRequest(logical_model="main", messages=())
    completion = GatewayCompletion(response=ModelResponse(text=""), deployment_id="fixture",
                                   logical_model="main", provider_id="fixture",
                                   provider_model="fixture/model")
    with pytest.raises(ValueError) as error:
        direct_delivery_report.binding(ctx, delivery, request, completion)
    assert str(error.value) == "direct final report is invalid (reason=binding_invalid)"
