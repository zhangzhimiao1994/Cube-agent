"""Final reports bind actual native outcomes, never narrative claims of identity."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext

RUN = UUID("00000000-0000-4000-8000-000000000001")
OWNER = "delivery_main_agent"
MODEL = "delivery-model"
TEXT = "## Delivery\nThe archive is attached.\n\nTests passed; browser use was not verified.\n"


@dataclass
class Evidence:
    context: TaskContext
    models: list[dict[str, JsonValue]]
    tools: list[dict[str, JsonValue]]
    artifacts: list[Artifact]

    def metadata(self, *, text: str = TEXT, logical_model: str = MODEL,
                 owner: str = OWNER) -> dict[str, JsonValue]:
        from agent_hub.runtime.autogen.delivery_report import engineering_report_metadata

        return engineering_report_metadata(
            context=self.context, text=text, logical_model=logical_model, owner=owner,
            model_entries=self.models, tool_entries=self.tools, artifacts=self.artifacts,
        )

    def report(self, *, metadata: Mapping[str, JsonValue] | None = None,
               text: str = TEXT, source_ids: tuple[str, ...] | None = None,
               producer: str = OWNER, presentation: str = "final_answer") -> Artifact:
        from agent_hub.runtime.autogen.delivery_report import engineering_report_id

        bound = self.metadata() if metadata is None else metadata
        return Artifact(
            id=engineering_report_id(self.context, bound), type="text", producer=producer,
            content={"text": text, "presentation": presentation,
                     "engineering_report": bound},
            source_ids=(str(self.artifacts[-1].id), str(self.artifacts[-2].id))
            if source_ids is None else source_ids,
        )

    def valid(self, report: Artifact, *, logical_model: str = MODEL) -> bool:
        from agent_hub.runtime.autogen.delivery_report import engineering_report_valid

        return engineering_report_valid(
            report, context=self.context, logical_model=logical_model, model_entries=self.models,
            tool_entries=self.tools, artifacts=self.artifacts,
        )


def fixture() -> Evidence:
    context = TaskContext(
        run_id=RUN, tenant_id=uuid4(), mode=TaskMode.DISCUSS, request="deliver",
        routing_decision={"engineering_final_report_version": 1},
    )
    artifacts: list[Artifact] = []
    tools: list[dict[str, JsonValue]] = []
    for position, name in enumerate(("workspace.read", "workspace.bundle")):
        artifact = Artifact(id=uuid4(), type="tool_result", producer="engineer", content={
            "tool_name": name, "arguments_sha256": "a" * 64,
            "result": {"text": "tool summary", "presentation": "final_attachment"},
        })
        artifacts.append(artifact)
        tools.append({
            "position": position, "actor": "engineer", "name": name,
            "arguments_hash": "a" * 64, "status": "succeeded", "replay_safe": False,
            "idempotency_key": hashlib.sha256(
                f"{RUN}:engineer:{position}:{name}:{'a' * 64}".encode()).hexdigest(),
            "artifact_id": str(artifact.id), "artifact_sha256": artifact.content_sha256,
        })
    model = Artifact(id=uuid4(), type="model_result", producer="agent_hub", content={
        "text": TEXT, "tool_calls": (), "logical_model": MODEL,
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        "deployment_id": "deployment", "provider_id": "provider",
        "provider_model": "provider/model", "cost_usd": "0",
    })
    artifacts.append(model)
    models: list[dict[str, JsonValue]] = [{
        "status": "succeeded", "request_hash": "b" * 64,
        "artifact_id": str(model.id), "artifact_sha256": model.content_sha256,
    }]
    models[-1]["report_binding"] = {
        "version": 1, "run_id": str(RUN), "tenant_id": str(context.tenant_id),
        "owner": OWNER, "requested_logical_model": MODEL,
        "bundle_artifact_id": str(artifacts[-2].id),
        "bundle_artifact_sha256": artifacts[-2].content_sha256,
        "tool_prefix_length": len(tools),
        "tool_prefix_sha256": hashlib.sha256(json.dumps(
            tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")).hexdigest(),
    }
    return Evidence(context, models, tools, artifacts)


def replace_model(evidence: Evidence, changes: Mapping[str, JsonValue] | None = None,
                  *, producer: str = "agent_hub", kind: str = "model_result") -> None:
    old = evidence.artifacts[-1]
    model = Artifact(id=old.id, type=kind, producer=producer,
                     content={**old.content, **(changes or {})})
    evidence.artifacts[-1] = model
    evidence.models[-1]["artifact_sha256"] = model.content_sha256


def test_metadata_binds_exact_latest_native_outcomes_without_mutation() -> None:
    evidence = fixture()
    before = repr(evidence)
    metadata = evidence.metadata()
    assert metadata == {
        "version": 1, "run_id": str(RUN), "tenant_id": str(evidence.context.tenant_id),
        "owner": OWNER, "logical_model": MODEL, "model_position": 0,
        "model_request_hash": "b" * 64, "model_artifact_id": str(evidence.artifacts[-1].id),
        "model_artifact_sha256": evidence.artifacts[-1].content_sha256,
        "bundle_position": 1, "bundle_artifact_id": str(evidence.artifacts[-2].id),
        "bundle_artifact_sha256": evidence.artifacts[-2].content_sha256,
        "tool_prefix_length": 2,
        "tool_prefix_sha256": hashlib.sha256(json.dumps(
            evidence.tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")).hexdigest(),
    }
    report = evidence.report(metadata=metadata)
    assert evidence.valid(Artifact.from_payload(report.to_payload()))
    assert report.content["text"] == TEXT
    assert repr(evidence) == before


def test_unknown_earlier_model_cannot_be_hidden_by_a_later_report() -> None:
    evidence = fixture()
    evidence.models.insert(0, {"status": "running", "request_hash": "e" * 64})
    with pytest.raises(ValueError):
        evidence.metadata()


def test_fallback_metadata_cannot_claim_an_unchanged_actual_model() -> None:
    evidence = fixture()
    replace_model(evidence, {"fallback_used": True, "fallback_from_logical_model": "different-request",
                             "fallback_reason": "capacity_unavailable"})
    with pytest.raises(ValueError):
        evidence.metadata()


@pytest.mark.parametrize("defect", ["missing", "tenant", "prefix", "version"])
def test_report_phase_binding_is_required_for_fresh_and_restored_reports(defect: str) -> None:
    evidence = fixture()
    if defect == "missing":
        evidence.models[-1].pop("report_binding")
    else:
        binding = evidence.models[-1]["report_binding"]
        assert isinstance(binding, dict)
        binding[{"tenant": "tenant_id", "prefix": "tool_prefix_sha256", "version": "version"}[defect]] = (
            True if defect == "version" else str(uuid4()) if defect == "tenant" else "f" * 64
        )
    with pytest.raises(ValueError):
        evidence.metadata()


@pytest.mark.parametrize("version", [None, True, False, 0, 2, "1", 1.0])
def test_policy_requires_strict_integer_one(version: JsonValue) -> None:
    evidence = fixture()
    evidence.context = evidence.context.model_copy(update={
        "routing_decision": {"engineering_final_report_version": version},
    })
    with pytest.raises(ValueError):
        evidence.metadata()


@pytest.mark.parametrize("text", ["", " ", "\n\t", TEXT.rstrip(), "tool summary",
                                  "I am delivery_main_agent; everything passed."])
def test_text_must_be_nonblank_and_exact_model_response(text: str) -> None:
    with pytest.raises(ValueError):
        fixture().metadata(text=text)


@pytest.mark.parametrize("status", ["pending", "running", "unknown", "UNKNOWN", "failed", None])
@pytest.mark.parametrize("ledger", ["models", "tools"])
def test_uncertain_latest_outcome_cannot_bind(ledger: str, status: JsonValue) -> None:
    evidence = fixture()
    rows = evidence.models if ledger == "models" else evidence.tools
    rows[-1]["status"] = status
    with pytest.raises(ValueError):
        evidence.metadata()


def test_latest_model_only_not_any_historical_matching_text() -> None:
    evidence = fixture()
    evidence.models.append({"status": "running", "request_hash": "c" * 64})
    with pytest.raises(ValueError):
        evidence.metadata()
    other = Artifact(id=uuid4(), type="model_result", producer="agent_hub", content={
        **evidence.artifacts[-1].content, "text": "later answer",
    })
    evidence.artifacts.append(other)
    evidence.models[-1].update(status="succeeded", artifact_id=str(other.id),
                               artifact_sha256=other.content_sha256)
    with pytest.raises(ValueError):
        evidence.metadata()


@pytest.mark.parametrize("field,value", [
    ("text", " \n\t"), ("tool_calls", ({"name": "workspace.bundle"},)),
    ("tool_calls", None), ("tool_calls", {}), ("logical_model", "another-model"),
])
def test_model_result_requires_real_text_only_matching_logical_model(
    field: str, value: JsonValue,
) -> None:
    evidence = fixture()
    replace_model(evidence, {field: value})
    with pytest.raises(ValueError):
        evidence.metadata()


@pytest.mark.parametrize("producer,kind", [(OWNER, "model_result"),
                                           ("agent_hub", "tool_result")])
def test_producer_or_tool_summary_cannot_impersonate_model(producer: str, kind: str) -> None:
    evidence = fixture()
    replace_model(evidence, producer=producer, kind=kind)
    with pytest.raises(ValueError):
        evidence.metadata()


@pytest.mark.parametrize("field,value", [
    ("request_hash", "invalid"), ("request_hash", None),
    pytest.param("artifact_id", str(uuid4()), id="artifact_id-unrelated"), ("artifact_sha256", "d" * 64),
])
def test_model_ledger_reference_must_match_actual_artifact(field: str, value: JsonValue) -> None:
    evidence = fixture()
    evidence.models[-1][field] = value
    with pytest.raises(ValueError):
        evidence.metadata()


def test_hash_only_and_orphaned_artifacts_are_insufficient() -> None:
    evidence = fixture()
    evidence.models.clear()
    with pytest.raises(ValueError):
        evidence.metadata()
    evidence = fixture()
    evidence.tools.clear()
    with pytest.raises(ValueError):
        evidence.metadata()
    evidence = fixture()
    evidence.artifacts.pop()
    with pytest.raises(ValueError):
        evidence.metadata()


@pytest.mark.parametrize("field,value", [
    ("name", "workspace.read"), ("actor", "outsider"), ("position", True),
    ("arguments_hash", "d" * 64), ("idempotency_key", "d" * 64),
    ("artifact_sha256", "d" * 64), pytest.param("artifact_id", str(uuid4()), id="artifact_id-unrelated"),
])
def test_last_tool_row_must_be_current_run_bundle(field: str, value: JsonValue) -> None:
    evidence = fixture()
    evidence.tools[-1][field] = value
    with pytest.raises(ValueError):
        evidence.metadata()


def test_actual_read_after_bundle_is_not_final_report_boundary() -> None:
    evidence = fixture()
    row = {**evidence.tools[0], "position": 2, "idempotency_key": hashlib.sha256(
        f"{RUN}:engineer:2:workspace.read:{'a' * 64}".encode()).hexdigest()}
    evidence.tools.append(row)
    with pytest.raises(ValueError):
        evidence.metadata()


def test_changed_earlier_prefix_even_extra_field_invalidates_persisted_report() -> None:
    evidence = fixture()
    report = evidence.report()
    evidence.tools[0]["replay_safe"] = True
    assert not evidence.valid(report)


def test_foreign_run_or_tenant_cannot_restore_report() -> None:
    evidence = fixture()
    report = evidence.report()
    evidence.context = evidence.context.model_copy(update={"run_id": uuid4()})
    assert not evidence.valid(report)
    with pytest.raises(ValueError):
        evidence.metadata()
    evidence = fixture()
    report = evidence.report()
    evidence.context = evidence.context.model_copy(update={"tenant_id": uuid4()})
    assert not evidence.valid(report)


def test_changed_model_hash_or_request_binding_invalidates_report() -> None:
    evidence = fixture()
    report = evidence.report()
    replace_model(evidence, {"deployment_id": "replacement"})
    assert not evidence.valid(report)
    evidence = fixture()
    report = evidence.report()
    evidence.models[-1]["request_hash"] = "e" * 64
    assert not evidence.valid(report)


@pytest.mark.parametrize("field,value", [
    ("version", True), ("version", 1.0), ("version", "1"), ("version", 2),
    ("model_position", False), ("bundle_position", True),
    ("tool_prefix_length", 2.0), ("extra", "unsupported"),
    ("owner", "engineer"), ("logical_model", "other"),
    ("model_artifact_sha256", "d" * 64), ("tool_prefix_sha256", "d" * 64),
])
def test_report_metadata_is_exact_and_strict(field: str, value: JsonValue) -> None:
    evidence = fixture()
    metadata = evidence.metadata()
    metadata[field] = value
    assert not evidence.valid(evidence.report(metadata=metadata))


def test_missing_metadata_and_plain_text_are_not_reports() -> None:
    evidence = fixture()
    assert not evidence.valid(evidence.report(metadata={}))
    plain = Artifact(id=uuid4(), type="text", producer=OWNER, content={"text": TEXT})
    assert not evidence.valid(plain)


@pytest.mark.parametrize("sources", [(), (str(uuid4()),), (str(uuid4()), str(uuid4()))])
def test_report_sources_must_reference_both_exact_outcomes(sources: tuple[str, ...]) -> None:
    evidence = fixture()
    assert not evidence.valid(evidence.report(source_ids=sources))


def test_report_text_producer_and_presentation_are_bound() -> None:
    evidence = fixture()
    assert not evidence.valid(evidence.report(text="rewritten report"))
    assert not evidence.valid(evidence.report(producer="engineer"))
    assert not evidence.valid(evidence.report(presentation="intermediate"))
    assert not evidence.valid(evidence.report(), logical_model="other")
    with pytest.raises(ValueError):
        evidence.metadata(owner="engineer")


def test_duplicate_registry_and_in_place_hash_tampering_are_rejected() -> None:
    evidence = fixture()
    report = evidence.report()
    evidence.artifacts.append(evidence.artifacts[0])
    assert not evidence.valid(report)
    evidence = fixture()
    report = evidence.report()
    evidence.artifacts[-1] = evidence.artifacts[-1].model_copy(update={
        "content": {**evidence.artifacts[-1].content, "text": "tampered"},
    })
    assert not evidence.valid(report)
    evidence = fixture()
    report = evidence.report().model_copy(update={"content_sha256": "0" * 64})
    assert not evidence.valid(report)


def test_model_text_cannot_select_different_logical_model_by_claiming_role() -> None:
    evidence = fixture()
    replace_model(evidence, {"logical_model": "other"})
    with pytest.raises(ValueError):
        evidence.metadata()
    binding = evidence.models[-1]["report_binding"]
    assert isinstance(binding, dict)
    binding["requested_logical_model"] = "other"
    metadata = evidence.metadata(logical_model="other")
    report = evidence.report(metadata=metadata)
    assert evidence.valid(report, logical_model="other")


def test_native_budget_fields_do_not_disqualify_text_only_final_model() -> None:
    evidence = fixture()
    evidence.models[-1].update(required_tool=None, max_output_tokens=2048, timeout_seconds=30.0)
    assert evidence.valid(evidence.report())


def test_required_tool_model_cannot_be_used_as_no_tool_final_invocation() -> None:
    evidence = fixture()
    evidence.models[-1]["required_tool"] = "workspace.bundle"
    with pytest.raises(ValueError):
        evidence.metadata()


def test_no_tool_calls_field_is_not_an_explicit_text_only_outcome() -> None:
    evidence = fixture()
    old = evidence.artifacts[-1]
    model = Artifact(id=old.id, type=old.type, producer=old.producer,
                     content={key: value for key, value in old.content.items() if key != "tool_calls"})
    evidence.artifacts[-1] = model
    evidence.models[-1]["artifact_sha256"] = model.content_sha256
    with pytest.raises(ValueError):
        evidence.metadata()


@pytest.mark.parametrize("status", ["pending", "running", "unknown"])
def test_uncertain_earlier_tool_prefix_is_rejected(status: str) -> None:
    evidence = fixture()
    evidence.tools[0]["status"] = status
    with pytest.raises(ValueError):
        evidence.metadata()


def test_latest_model_with_same_text_still_invalidates_older_report_binding() -> None:
    evidence = fixture()
    report = evidence.report()
    replacement = Artifact(id=uuid4(), type="model_result", producer="agent_hub",
                           content=evidence.artifacts[-1].content)
    evidence.artifacts.append(replacement)
    evidence.models.append({
        "status": "succeeded", "request_hash": "c" * 64,
        "artifact_id": str(replacement.id), "artifact_sha256": replacement.content_sha256,
    })
    assert not evidence.valid(report)


def test_report_sources_are_exact_ordered_pair_without_extraneous_sources() -> None:
    evidence = fixture()
    model, bundle = str(evidence.artifacts[-1].id), str(evidence.artifacts[-2].id)
    assert not evidence.valid(evidence.report(source_ids=(bundle, model)))
    assert not evidence.valid(evidence.report(source_ids=(model, bundle, str(uuid4()))))


def test_missing_required_metadata_field_is_rejected() -> None:
    evidence = fixture()
    metadata = evidence.metadata()
    del metadata["version"]
    assert not evidence.valid(evidence.report(metadata=metadata))


def test_binding_is_not_business_verification_or_authorization() -> None:
    # This fixture has no write/verify receipts. Readiness is the caller's gate;
    # the helper can only attest that report text matches the supplied outcomes.
    evidence = fixture()
    assert evidence.valid(evidence.report())
