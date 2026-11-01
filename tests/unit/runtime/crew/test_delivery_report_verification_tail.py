"""Post-writer verification uses current-run receipts, never a tool-name bypass.

The five-step graph and 38 ledger positions reproduce the observed relationship;
all bodies/identities are local fixtures, not production checkpoint contents.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import uuid4

import pytest
import pytest_asyncio

from agent_hub.capabilities.node_verification_recovery import (
    _validate_receipt as validate_node_receipt,
)
from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import (
    Artifact,
    GatewayProvenance,
    JsonValue,
    TaskContext,
    _mutable_json,
)
from agent_hub.runtime.crew.delivery_report import (
    BINDING,
    delivery_evidence,
    final_evidence,
    report_binding,
    validate_report,
)
from agent_hub.runtime.crew.engineering_delivery import (
    _successful_source,
    engineering_delivery_ready,
)
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from agent_hub.runtime.python_project_history import python_failure_feedback
from tests.unit.capabilities.test_project_original_verification import service as node_service
from tests.unit.runtime.delivery_report_typed_fixture import json_map, mapping, mutable, sequence
from tests.unit.runtime.test_python_project_history import history_case

WRITER = "implementer_step"
TESTER = "tester_step"
TAIL = "tester:0:0:1"
TEXT = "The attached ZIP contains the verified project. Review its usage and remaining limitations."


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, default=dict, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


@dataclass
class Case:
    context: TaskContext
    plan: DispatchPlan
    states: dict[str, Mapping[str, JsonValue]]
    artifacts: dict[str, Artifact]
    sources: dict[str, Artifact]

    @property
    def step(self) -> DispatchStep:
        return self.plan.steps[-1]

    def evidence(self) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
        return delivery_evidence(self.context, self.plan, self.step, self.states, self.artifacts)

    def replace_result(self, key: str, result: Mapping[str, object]) -> None:
        old = self.artifacts[key]
        new = Artifact(
            id=old.id,
            type=old.type,
            producer=old.producer,
            source_ids=old.source_ids,
            content=json_map({**old.content, "result": result}),
        )
        self.artifacts[key] = new
        self.states[key] = {**self.states[key], "sha256": new.content_sha256}


def plan() -> DispatchPlan:
    tools = (
        "workspace.write_text",
        "workspace.read",
        "workspace.list",
        "workspace.bundle",
        "project.inspect",
        "project.verify",
        "read_context",
    )
    actors = ("architect", "implementer", "tester", "security_reviewer", "final_synthesizer")
    agents = tuple(
        AgentSpec(
            id=actor,
            role=actor,
            goal="Synthetic regression only",
            logical_model="fixture",
            allowed_tools=tools,
            output_schema={} if actor == actors[-1] else {"summary": "string"},
        )
        for actor in actors
    )
    steps = (
        DispatchStep(id="architect_step", agent="architect", task="Plan"),
        DispatchStep(
            id=WRITER,
            agent="implementer",
            task="Repair and deliver",
            depends_on=("architect_step",),
            tools=tools[:-1],
            engineering_delivery_contract_version=1,
        ),
        DispatchStep(
            id=TESTER,
            agent="tester",
            task="Verify existing candidate",
            depends_on=("architect_step", WRITER),
            tools=("read_context", "project.verify"),
        ),
        DispatchStep(
            id="security_reviewer_step",
            agent="security_reviewer",
            task="Review",
            depends_on=("architect_step", WRITER),
            tools=("read_context",),
        ),
        DispatchStep(
            id="final_response_step",
            agent="final_synthesizer",
            task="Report",
            depends_on=tuple(actor + "_step" for actor in actors[:-1]),
            final_synthesizer=True,
        ),
    )
    return DispatchPlan(agents=agents, steps=steps, allowed_tools=tools)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def native_case(tmp_path_factory: pytest.TempPathFactory) -> Case:
    tmp_path = tmp_path_factory.mktemp("native-python-tail")
    verifier, _, executor, original, context = await history_case(tmp_path)
    source = (
        tmp_path
        / "workspaces"
        / str(context.tenant_id)
        / "projects"
        / original.invocation.project_id
        / "sessions"
        / original.invocation.session_id
    )
    with pytest.raises(ProjectVerificationError) as failed:
        await verifier.verify(
            tenant_id=context.tenant_id, run_id=context.run_id, actor="implementer"
        )
    failure = failed.value.python_project_failure
    assert failure is not None and failure.diagnostic is not None
    failed_result: dict[str, JsonValue] = {
        "status": "failed",
        "untrusted": True,
        "python_verification_failure": failure.to_payload(),
        "execution_diagnostic": failure.diagnostic.to_payload(),
    }
    if failure.environment_reference is not None:
        failed_result["python_environment_reference"] = failure.environment_reference.model_dump(
            mode="json"
        )
    assert python_failure_feedback(failed_result, tool_name="project.verify") == failed_result
    candidate = next(p for p in (source / "src").rglob("*.py"))
    # This is the pre-existing tiny local test fixture, never a user's project.
    candidate.write_bytes(candidate.read_bytes() + b"\n# synthetic repaired candidate\n")
    executor.failure_index = None
    writer_result = await verifier.verify(
        tenant_id=context.tenant_id, run_id=context.run_id, actor="implementer"
    )
    tester_result = await verifier.verify(
        tenant_id=context.tenant_id, run_id=context.run_id, actor="tester"
    )
    assert _successful_source(writer_result, context=context) == _successful_source(
        tester_result, context=context
    )
    assert (
        writer_result["verification"]["execution_id"]
        != tester_result["verification"]["execution_id"]
    )
    context = context.model_copy(
        update={
            "mode": TaskMode.DISPATCH,
            "routing_decision": {
                **context.routing_decision,
                "engineering_dispatch_report_version": 1,
            },
        }
    )
    result = Case(context, plan(), {}, {}, {})
    raw = candidate.read_bytes()
    file = {
        "path": candidate.relative_to(source).as_posix(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    bundle: dict[str, object] = {
        "artifact_id": str(uuid4()),
        "presentation": "final_attachment",
        "file": {"filename": "fixture.zip"},
        "workspace_files": [file],
        "verification_source_sha256": writer_result["verification"]["source_sha256"],
        "verification_receipt_sha256": digest(writer_result["verification"]),
        "original_check_contract": writer_result["original_check_contract"],
    }
    positions: list[tuple[str, str, int, int, int, str]] = []
    writer_names = (
        "project.inspect",
        "workspace.list",
        *("workspace.read",) * 9,
        "project.verify",
        "workspace.write_text",
        "workspace.read",
        "workspace.write_text",
        "workspace.read",
        "workspace.write_text",
        "project.verify",
        "workspace.list",
        "workspace.read",
        "workspace.bundle",
    )
    positions.extend(
        (WRITER, name, 0, round_, 0, "failed" if round_ == 11 else "succeeded")
        for round_, name in enumerate(writer_names)
    )
    positions.extend(
        (
            TESTER,
            "project.verify" if round_ == 0 and index == 1 else "read_context",
            0,
            round_,
            index,
            "succeeded",
        )
        for round_, indices in enumerate(((0, 1), (1, 2), (0, 1), (0, 1, 2), (0, 1, 2)))
        for index in indices
    )
    positions.append((TESTER, "read_context", 1, 0, 0, "succeeded"))
    positions.extend(
        ("security_reviewer_step", "read_context", 0, round_, index, "succeeded")
        for round_ in (0, 1)
        for index in (0, 1)
    )
    for step_id, name, attempt, round_, index, status in positions:
        actor = next(step.agent for step in result.plan.steps if step.id == step_id)
        key = ("tester" if step_id == TESTER else step_id) + f":{attempt}:{round_}:{index}"
        value: Mapping[str, object] = {"observed": True}
        if name == "workspace.write_text":
            value = {"file": file, "workspace_files": [file]}
        elif name == "workspace.bundle":
            value = bundle
        elif name == "project.verify":
            value = (
                failed_result
                if status == "failed"
                else writer_result
                if step_id == WRITER
                else tester_result
            )
        trigger = Artifact(
            id=uuid4(),
            type="model_response",
            producer=actor,
            content=json_map({"tool_calls": [{"id": key, "name": name, "arguments": {}}]}),
        )
        result.sources[str(trigger.id)] = trigger
        artifact = Artifact(
            id=uuid4(),
            type="tool_result",
            producer=actor,
            source_ids=(str(trigger.id),),
            content=json_map(
                {
                    "agent_id": actor,
                    "tool_name": name,
                    "arguments_sha256": digest({}),
                    "result": value,
                }
            ),
        )
        result.artifacts[key] = artifact
        result.states[key] = {
            "step_id": step_id,
            "name": name,
            "status": status,
            "attempt": attempt,
            "round": round_,
            "tool_index": index,
            "arguments_sha256": digest({}),
            "artifact_id": str(artifact.id),
            "sha256": artifact.content_sha256,
        }
    assert len(result.states) == 38
    return result


@pytest.fixture
def case(native_case: Case) -> Case:
    return Case(
        native_case.context,
        native_case.plan,
        dict(native_case.states),
        dict(native_case.artifacts),
        dict(native_case.sources),
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def native_node_case(native_case: Case, tmp_path_factory: pytest.TempPathFactory) -> Case:
    verifier, executor, source, invocation, _ = await node_service(
        tmp_path_factory.mktemp("native-node-tail"), failed=True
    )
    with pytest.raises(ProjectVerificationError) as failed:
        await verifier.verify(
            tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor="implementer"
        )
    assert failed.value.project_failure is not None and failed.value.diagnostic is not None
    failure: dict[str, JsonValue] = {
        "status": "failed",
        "untrusted": True,
        "verification_failure": failed.value.project_failure.to_payload(),
        "execution_diagnostic": failed.value.diagnostic.to_payload(),
    }
    candidate = source / "src/index.js"
    candidate.write_bytes(candidate.read_bytes() + b"\n// synthetic repaired candidate\n")
    executor.failed = False
    writer = await verifier.verify(
        tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor="implementer"
    )
    tester = await verifier.verify(
        tenant_id=invocation.tenant_id, run_id=invocation.run_id, actor="tester"
    )
    record = await verifier.repository.get(invocation.tenant_id, invocation.run_id)
    context = TaskContext(
        run_id=invocation.run_id,
        tenant_id=invocation.tenant_id,
        mode=TaskMode.DISPATCH,
        request="repair",
        routing_decision={**record.routing_decision, "engineering_dispatch_report_version": 1},
    )
    assert _successful_source(writer, context=context) == _successful_source(
        tester, context=context
    )
    for value in (writer, tester):
        validate_node_receipt(value["verification"], value["verification"]["source_sha256"])
    result = Case(
        context,
        native_case.plan,
        dict(native_case.states),
        dict(native_case.artifacts),
        dict(native_case.sources),
    )
    result.replace_result(WRITER + ":0:11:0", failure)
    result.replace_result(WRITER + ":0:17:0", writer)
    result.replace_result(TAIL, tester)
    raw = candidate.read_bytes()
    file = {
        "path": "src/index.js",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }
    for round_ in (12, 14, 16):
        result.replace_result(WRITER + f":0:{round_}:0", {"file": file, "workspace_files": [file]})
    bundle = mutable(_mutable_json(result.artifacts[WRITER + ":0:20:0"].content["result"]))
    bundle.update(
        verification_source_sha256=writer["verification"]["source_sha256"],
        verification_receipt_sha256=digest(writer["verification"]),
        original_check_contract=writer["original_check_contract"],
        workspace_files=[file],
    )
    result.replace_result(WRITER + ":0:20:0", bundle)
    return result


@pytest.fixture
def node_case(native_node_case: Case) -> Case:
    return Case(
        native_node_case.context,
        native_node_case.plan,
        dict(native_node_case.states),
        dict(native_node_case.artifacts),
        dict(native_node_case.sources),
    )


def report(case: Case) -> tuple[Artifact, dict[str, Mapping[str, JsonValue]], dict[str, Artifact]]:
    phase, _ = case.evidence()
    provenance = GatewayProvenance(
        logical_model="fixture",
        deployment_id="fixture",
        provider_id="fixture",
        provider_model="fixture/model",
    )
    model = Artifact(
        id=uuid4(),
        type="model_response",
        producer=case.step.agent,
        provenance=provenance,
        content=json_map({"text": TEXT, "tool_calls": []}),
    )
    rows: dict[str, Mapping[str, JsonValue]] = {
        "report": {
            "step_id": case.step.id,
            "purpose": "step",
            "status": "succeeded",
            BINDING: phase,
            "artifact_id": str(model.id),
            "sha256": model.content_sha256,
            "request_sha256": "f" * 64,
            "provenance": json_map(provenance.to_payload()),
        }
    }
    models = {"report": model}
    binding = report_binding(
        context=case.context,
        plan=case.plan,
        step=case.step,
        text=TEXT,
        states=case.states,
        artifacts=case.artifacts,
        models=rows,
        model_artifacts=models,
    )
    candidate = Artifact(
        id=uuid4(),
        type="text",
        producer=case.step.agent,
        provenance=provenance,
        source_ids=(str(model.id),),
        content={"text": TEXT, BINDING: binding},
    )
    return candidate, rows, models


def test_fixture_writer_and_bundle_are_valid_without_extra_verifier(case: Case) -> None:
    assert engineering_delivery_ready(
        step_id=WRITER,
        actor="implementer",
        states=case.states,
        artifacts=case.artifacts,
        context=case.context,
    )
    del case.states[TAIL]
    del case.artifacts[TAIL]
    phase, evidence = case.evidence()
    assert phase["bundle_artifact_id"] == evidence["bundle_artifact_id"]


def test_same_run_same_source_tester_receipt_in_38_terminal_prefix_is_accepted(case: Case) -> None:
    assert {row["status"] for row in case.states.values()} == {"succeeded", "failed"}
    before = repr(case)
    phase, _ = case.evidence()
    assert phase["verification_artifact_id"] == str(case.artifacts[WRITER + ":0:17:0"].id)
    assert phase["bundle_artifact_id"] == str(case.artifacts[WRITER + ":0:20:0"].id)
    expected = hashlib.sha256(
        json.dumps(
            _mutable_json(case.states),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert phase["tool_prefix_sha256"] == expected
    assert repr(case) == before


def test_report_keeps_full_tail_binding_and_rejects_changed_prefix(case: Case) -> None:
    candidate, rows, models = report(case)
    validate_report(
        candidate,
        context=case.context,
        plan=case.plan,
        step=case.step,
        states=case.states,
        artifacts=case.artifacts,
        models=rows,
        model_artifacts=models,
    )
    facts = final_evidence(
        candidate,
        case.context,
        (*case.artifacts.values(), *case.sources.values(), *models.values()),
    )
    assert facts["verification_artifact_id"] == str(case.artifacts[WRITER + ":0:17:0"].id)
    case.states[TAIL] = {**case.states[TAIL], "attempt": 1}
    with pytest.raises(ValueError, match="delivery report"):
        validate_report(
            candidate,
            context=case.context,
            plan=case.plan,
            step=case.step,
            states=case.states,
            artifacts=case.artifacts,
            models=rows,
            model_artifacts=models,
        )


def test_broader_unused_permissions_do_not_make_actual_verify_tail_a_write(case: Case) -> None:
    original = case.plan.steps[2]
    changed = original.model_copy(update={"tools": (*original.tools, "workspace.write_text")})
    case.plan = DispatchPlan.model_validate(
        {**case.plan.model_dump(), "steps": (*case.plan.steps[:2], changed, *case.plan.steps[3:])}
    )
    assert all(
        row["name"] in {"project.verify", "read_context"}
        for row in case.states.values()
        if row["step_id"] == TESTER
    )
    assert case.evidence()[0]["verification_artifact_id"] == str(
        case.artifacts[WRITER + ":0:17:0"].id
    )


def test_broader_permissions_with_actual_tester_write_remain_rejected(case: Case) -> None:
    original = case.plan.steps[2]
    changed = original.model_copy(update={"tools": (*original.tools, "workspace.write_text")})
    case.plan = DispatchPlan.model_validate(
        {**case.plan.model_dump(), "steps": (*case.plan.steps[:2], changed, *case.plan.steps[3:])}
    )
    key = "tester:0:1:1"
    args = {"path": "src/changed.py", "content": "synthetic change\n"}
    model = Artifact(
        id=uuid4(),
        type="model_response",
        producer="tester",
        content=json_map(
            {"tool_calls": [{"id": key, "name": "workspace.write_text", "arguments": args}]}
        ),
    )
    file = {
        "path": args["path"],
        "sha256": hashlib.sha256(args["content"].encode()).hexdigest(),
        "size_bytes": len(args["content"].encode()),
    }
    artifact = Artifact(
        id=uuid4(),
        type="tool_result",
        producer="tester",
        source_ids=(str(model.id),),
        content=json_map(
            {
                "agent_id": "tester",
                "tool_name": "workspace.write_text",
                "arguments_sha256": digest(args),
                "result": {"file": file, "workspace_files": [file]},
            }
        ),
    )
    case.sources[str(model.id)] = model
    case.artifacts[key] = artifact
    case.states[key] = {
        **case.states[key],
        "name": "workspace.write_text",
        "arguments_sha256": digest(args),
        "artifact_id": str(artifact.id),
        "sha256": artifact.content_sha256,
    }
    with pytest.raises(ValueError, match="delivery report"):
        case.evidence()


def test_node_original_fixture_without_extra_verifier_is_valid(node_case: Case) -> None:
    assert engineering_delivery_ready(
        step_id=WRITER,
        actor="implementer",
        states=node_case.states,
        artifacts=node_case.artifacts,
        context=node_case.context,
    )
    del node_case.states[TAIL]
    del node_case.artifacts[TAIL]
    assert node_case.evidence()[0]["bundle_artifact_id"] == str(
        node_case.artifacts[WRITER + ":0:20:0"].id
    )


def test_node_original_native_receipt_supports_same_source_tester_tail(node_case: Case) -> None:
    candidate, rows, models = report(node_case)
    validate_report(
        candidate,
        context=node_case.context,
        plan=node_case.plan,
        step=node_case.step,
        states=node_case.states,
        artifacts=node_case.artifacts,
        models=rows,
        model_artifacts=models,
    )
    assert mapping(candidate.content[BINDING])["verification_artifact_id"] == str(
        node_case.artifacts[WRITER + ":0:17:0"].id
    )


@pytest.mark.parametrize("defect", ["extra", "runtime", "diagnostic", "source", "contract"])
def test_node_tail_requires_original_scope_and_strict_native_receipt(
    node_case: Case, defect: str
) -> None:
    value = mutable(_mutable_json(node_case.artifacts[TAIL].content["result"]))
    receipt = mutable(value["verification"])
    if defect == "extra":
        receipt["kind"] = "python_project"
    elif defect == "runtime":
        receipt["runtime"] = {"go": "1.24"}
    elif defect == "diagnostic":
        checks = sequence(receipt["checks"])
        mutable(mutable(checks[0])["execution_diagnostic"])["backend"] = "forged"
    elif defect == "source":
        receipt["source_sha256"] = "0" * 64
        mutable(value["original_check_contract"])["candidate_source_sha256"] = "0" * 64
    else:
        mutable(value["original_check_contract"])["protected_sha256"] = "0" * 64
    node_case.replace_result(TAIL, value)
    with pytest.raises(ValueError, match="delivery report"):
        node_case.evidence()


@pytest.mark.parametrize(
    "defect",
    [
        "source",
        "recipe",
        "environment",
        "contract",
        "run",
        "checks",
        "missing_receipt",
        "sha",
        "arguments",
        "actor",
        "not_descendant",
        "write_tail",
        "UNKNOWN",
        "running",
        "failed",
    ],
)
def test_post_writer_verify_never_bypasses_tail_receipt_or_integrity(
    case: Case, defect: str
) -> None:
    artifact = case.artifacts[TAIL]
    value = mutable(_mutable_json(artifact.content["result"]))
    if defect in {"source", "recipe", "checks"}:
        receipt = mutable(value["verification"])
        receipt[
            {"source": "source_sha256", "recipe": "recipe_sha256", "checks": "checks"}[defect]
        ] = [] if defect == "checks" else "0" * 64
        if defect == "source":
            mutable(value["original_check_contract"])["candidate_source_sha256"] = "0" * 64
        case.replace_result(TAIL, value)
    elif defect == "contract":
        mutable(value["original_check_contract"])["protected_sha256"] = "0" * 64
        case.replace_result(TAIL, value)
    elif defect == "environment":
        mutable(value["verification"])["environment_sha256"] = "0" * 64
        mutable(value["python_environment_reference"])["environment_sha256"] = "0" * 64
        # Both independently valid receipts attest the same source, but a tail
        # under another root environment cannot extend this delivered prefix.
        assert _successful_source(json_map(value), context=case.context) is not None
        case.replace_result(TAIL, value)
    elif defect == "run":
        reference = mutable(value["python_environment_reference"])
        mutable(reference["scope"])["run_id"] = str(uuid4())
        case.replace_result(TAIL, value)
    elif defect == "missing_receipt":
        value.pop("verification")
        case.replace_result(TAIL, value)
    elif defect in {"sha", "arguments"}:
        case.states[TAIL] = {
            **case.states[TAIL],
            "sha256" if defect == "sha" else "arguments_sha256": "0" * 64,
        }
    elif defect == "actor":
        changed = Artifact(
            id=artifact.id,
            type=artifact.type,
            producer="implementer",
            source_ids=artifact.source_ids,
            content={**artifact.content, "agent_id": "implementer"},
        )
        case.artifacts[TAIL] = changed
        case.states[TAIL] = {**case.states[TAIL], "sha256": changed.content_sha256}
    elif defect == "not_descendant":
        changed_step = case.plan.steps[2].model_copy(update={"depends_on": ("architect_step",)})
        case.plan = DispatchPlan.model_validate(
            {
                **case.plan.model_dump(),
                "steps": (*case.plan.steps[:2], changed_step, *case.plan.steps[3:]),
            }
        )
    elif defect == "write_tail":
        key = WRITER + ":0:16:0"
        case.states[key] = {**case.states[key], "round": 21}
    else:
        case.states[TAIL] = {**case.states[TAIL], "status": defect}
    with pytest.raises(ValueError, match="delivery report"):
        case.evidence()
