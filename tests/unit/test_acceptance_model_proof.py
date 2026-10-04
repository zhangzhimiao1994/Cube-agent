from __future__ import annotations

import copy
import importlib.util
import json
from dataclasses import fields
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote
from uuid import UUID, uuid4

import pytest

from agent_hub.harness.project_scale_runner import ProjectScaleCaseResult
from agent_hub.models.failure_receipt import (
    GatewayFailureAttempt,
    GatewayFailureReceipt,
    _attach_gateway_failure_receipt,
)
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelMessage, ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.contracts import Artifact, GatewayProvenance
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.model_scope import ModelScopeTracker


def _load_script() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts/real_user_four_scale_acceptance.py"
    spec = importlib.util.spec_from_file_location("acceptance_model_proof_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _provenance(logical_model: str) -> GatewayProvenance:
    return GatewayProvenance(
        logical_model=logical_model,
        deployment_id="deepseek-deployment",
        provider_id="deepseek",
        provider_model="deepseek/deepseek-chat",
    )


def direct_model_events(
    run_id: str,
    logical_model: str = "deepseek",
    *,
    requested_model: str | None = None,
    attempted_models: tuple[str, ...] | None = None,
    artifact_type: str = "text",
) -> list[dict[str, Any]]:
    """Public DirectRuntime shape, including its distinct actor and producer."""
    requested = requested_model or logical_model
    attempts = attempted_models or (
        (requested, logical_model) if requested != logical_model else (logical_model,)
    )
    artifact = Artifact(
        id=uuid4(), type=artifact_type, producer="main",
        content={"text": "offline model answer"} if artifact_type == "text" else {
            "workspace_delivery": {"artifact_id": str(uuid4())},
            "artifact_origin": "incremental_workspace_delivery",
        },
        provenance=_provenance(logical_model),
    )
    linkage = {
        "artifact_id": str(artifact.id),
        "requested_logical_model": requested,
        "logical_model": logical_model,
        "attempted_logical_models": list(attempts),
    }
    common = {"run_id": run_id, "actor": "main_agent"}
    return [
        {**common, "kind": "model.started", "sequence": 1,
         "payload": {"logical_model": requested}},
        {**common, "kind": "artifact.created", "sequence": 2,
         "payload": {**linkage, "deployment": "deepseek-deployment", "provider": "deepseek",
                     "upstream_model": "deepseek/deepseek-chat"},
         "artifact": artifact.to_payload()},
        {**common, "kind": "runtime.completed", "sequence": 4, "payload": dict(linkage)},
    ]


def crew_model_events(
    run_id: str, logical_model: str = "deepseek", *, requested_model: str | None = None,
) -> list[dict[str, Any]]:
    """Crew publishes actual gateway output with only agent_id in the event payload."""
    requested = requested_model or logical_model
    fallback = requested != logical_model
    artifact = Artifact(
        id=uuid4(), type="model_response", producer="writer",
        content={
            "text": "offline model answer", "tool_calls": (),
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            "cost_usd": None, "fallback_used": fallback,
            "fallback_from_logical_model": requested if fallback else None,
            "fallback_reason": "transport_error" if fallback else None,
            "attempted_logical_models": (requested, logical_model) if fallback else (logical_model,),
            "provider_metadata": {},
        },
        provenance=_provenance(logical_model),
    )
    return [
        {"run_id": run_id, "actor": "writer", "kind": "model.started", "sequence": 2,
         "payload": {"logical_model": requested, "role": "writer"}},
        {"run_id": run_id, "actor": "writer", "kind": "artifact.created", "sequence": 3,
         "payload": {"agent_id": "writer"}, "artifact": artifact.to_payload()},
    ]


class _PublicEventsClient:
    def __init__(self, runs: dict[str, tuple[str, list[dict[str, Any]]]]) -> None:
        self.runs = runs

    def request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        assert method == "GET" and all(value is None for value in kwargs.values())
        for run_id, (status, events) in self.runs.items():
            root = f"/api/v1/runs/{quote(run_id, safe='')}"
            if path == root:
                return {"id": run_id, "status": status}
            if path == f"{root}/events":
                return {"items": copy.deepcopy(events)}
        raise AssertionError(f"unexpected request: {path}")


def model_scope_evidence(
    run_id: str,
    logical_model: str = "deepseek",
    *,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reusable verified proof fixture; no dependency on the legacy test module."""
    return _collect(
        {run_id: ("completed", events if events is not None else direct_model_events(
            run_id, logical_model,
        ))},
        original=run_id, result=run_id, selected=logical_model,
    )


def _collect(
    runs: dict[str, tuple[str, list[dict[str, Any]]]],
    *,
    original: str,
    result: str,
    selected: str | None,
    repairs: tuple[str, ...] = (),
) -> dict[str, Any]:
    module = _load_script()
    return cast(dict[str, Any], module._collect_model_scope_evidence(
        module.RealUserAcceptanceClient(_PublicEventsClient(runs)),
        logical_model=selected, submitted_run_ids=[original],
        accepted_repair_run_ids=list(repairs), result_run_id=result,
    ))


def _single(events: list[dict[str, Any]], selected: str | None) -> dict[str, Any]:
    run_id = events[0]["run_id"]
    return _collect({run_id: ("completed", events)}, original=run_id, result=run_id,
                    selected=selected)


def _bare_completion(run_id: str) -> dict[str, Any]:
    return {"run_id": run_id, "kind": "model.completed", "payload": {"logical_model": "deepseek"}}


@pytest.mark.parametrize("selected", [None, "deepseek"])
@pytest.mark.parametrize("delivery", ["direct_text", "direct_workspace", "crew"])
def test_actual_public_completion_works_with_or_without_selected_model(
    selected: str | None, delivery: str,
) -> None:
    run_id = str(uuid4())
    events = crew_model_events(run_id) if delivery == "crew" else direct_model_events(
        run_id, artifact_type="tool_result" if delivery == "direct_workspace" else "text",
    )
    evidence = _single(events, selected)
    assert evidence["ok"] is True, evidence["errors"]
    assert "offline model answer" not in json.dumps(evidence)


@pytest.mark.parametrize("selected", [None, "deepseek"])
@pytest.mark.parametrize("attempts", [None, [], ["deepseek"]])
def test_raw_completion_never_establishes_completion(
    selected: str | None, attempts: list[str] | None,
) -> None:
    event = _bare_completion(str(uuid4()))
    if attempts is not None:
        event["payload"]["attempted_logical_models"] = attempts
    evidence = _single([event], selected)
    assert evidence["ok"] is False
    assert any("no actual model completion" in error for error in evidence["errors"])


@pytest.mark.parametrize("selected", [None, "deepseek"])
@pytest.mark.parametrize("delivery", ["direct", "crew"])
def test_raw_marker_beside_valid_completion_is_not_itself_a_failure(
    selected: str | None, delivery: str,
) -> None:
    run_id = str(uuid4())
    events = direct_model_events(run_id) if delivery == "direct" else crew_model_events(run_id)
    evidence = _single([*events, _bare_completion(run_id)], selected)
    assert evidence["ok"] is True, evidence["errors"]


@pytest.mark.parametrize("missing", ["model.started", "artifact.created", "runtime.completed"])
def test_raw_marker_cannot_rescue_broken_direct_chain(missing: str) -> None:
    run_id = str(uuid4())
    events = [event for event in direct_model_events(run_id) if event["kind"] != missing]
    evidence = _single([*events, _bare_completion(run_id)], "deepseek")
    assert evidence["ok"] is False


@pytest.mark.parametrize("selected,passed", [(None, True), ("deepseek", False), ("backup", False)])
def test_direct_fallback_keeps_requested_start_and_actual_provenance_distinct(
    selected: str | None, passed: bool,
) -> None:
    evidence = _single(direct_model_events(
        str(uuid4()), "backup", requested_model="deepseek",
    ), selected)
    assert evidence["ok"] is passed, evidence["errors"]


@pytest.mark.parametrize("invalid", ["requested_start", "requested_attempt", "actual_attempt"])
def test_unselected_direct_fallback_still_requires_consistent_chain(invalid: str) -> None:
    events = direct_model_events(str(uuid4()), "backup", requested_model="deepseek")
    if invalid == "requested_start":
        events[0]["payload"]["logical_model"] = "other"
    else:
        attempts = ["other", "backup"] if invalid == "requested_attempt" else ["deepseek", "other"]
        for event in events[1:]:
            event["payload"]["attempted_logical_models"] = attempts
    assert _single(events, None)["ok"] is False


def _receipt_events(
    run_id: str, *, requested: str = "deepseek", actual: str = "deepseek",
    failed_model: str | None = None,
) -> list[dict[str, Any]]:
    """Use the real tracker and Direct event emitter without invoking a gateway."""
    provenance = _provenance(actual)
    fallback = requested != actual
    failed_models = (requested, failed_model) if failed_model is not None else (requested,)
    tracker = ModelScopeTracker(run_id=UUID(run_id), tenant_id=uuid4(), logical_model=requested)
    error = RuntimeError("offline empty response")
    _attach_gateway_failure_receipt(error, GatewayFailureReceipt(
        call_id=str(uuid4()), requested_logical_model=requested,
        allow_fallback=failed_model is not None,
        history_complete=True, attempted_logical_models=failed_models,
        attempts=tuple(GatewayFailureAttempt(
            ordinal=index, logical_model=model, deployment_id=provenance.deployment_id,
            provider_id=provenance.provider_id, provider_model=provenance.provider_model,
            outcome="empty_response", status_code=200, usage_status="known",
            usage=TokenUsage(prompt_tokens=2, completion_tokens=0, total_tokens=2),
        ) for index, model in enumerate(failed_models, 1)),
    ))
    tracker.failed(tracker.begin(), error)
    tracker.received(
        tracker.begin(),
        ModelRequest(logical_model=requested, messages=(
            ModelMessage(role="user", content="offline request"),
        )),
        GatewayCompletion(
            response=ModelResponse(text="offline answer"), logical_model=actual,
            deployment_id=provenance.deployment_id, provider_id=provenance.provider_id,
            provider_model=provenance.provider_model,
            attempted_logical_models=(requested, actual) if fallback else (actual,),
            fallback_used=fallback, fallback_from_logical_model=requested if fallback else None,
            fallback_reason="transport_error" if fallback else None,
        ),
    )
    runtime = DirectRuntime(cast(Any, object()), logical_model=requested)
    runtime._model_scope_tracker = tracker
    emitted = runtime._model_scope_events(run_id=UUID(run_id), sequence=2)
    assert len(emitted) == 2 and not tracker.incomplete
    return [
        {"run_id": run_id, "kind": "model.started", "sequence": 1,
         "actor": "main_agent", "payload": {"logical_model": requested}},
        *(event.to_payload() for event in emitted),
    ]


@pytest.mark.parametrize("selected,actual,failed_model,passed", [
    (None, "deepseek", None, True), ("deepseek", "deepseek", None, True),
    (None, "backup", None, True), ("deepseek", "backup", None, False),
    (None, "deepseek", "backup", True), ("deepseek", "deepseek", "backup", False),
])
def test_receipt_history_and_actual_recovery_completion_can_coexist(
    selected: str | None, actual: str, failed_model: str | None, passed: bool,
) -> None:
    run_id = str(uuid4())
    events = _receipt_events(run_id, actual=actual, failed_model=failed_model)
    completion = direct_model_events(run_id, actual, requested_model="deepseek")
    for event in completion[1:]:
        event["sequence"] += 2
    evidence = _single([*events, *completion[1:]], selected)
    assert evidence["ok"] is passed, evidence["errors"]
    if passed:
        retained = evidence["runs"][0]["model_events"][1]["model_artifact"]
        assert retained["scope_content"] == events[1]["artifact"]["content"]
        assert retained["provenance"]["logical_model"] == actual


@pytest.mark.parametrize("declared,entered,passed", [
    (("deepseek", "backup"), ("backup", "deepseek"), False),
    (("deepseek", "backup"), ("deepseek", "backup", "deepseek"), False),
    (("deepseek", "backup"), ("deepseek", "deepseek", "deepseek", "backup"), True),
    (("deepseek", "candidate", "backup"), ("deepseek", "backup"), True),
    (("deepseek", "backup"), ("backup",), True),
    (("deepseek", "backup"), ("deepseek",), True),
])
def test_failed_receipt_transport_order_allows_retries_and_skipped_candidates(
    declared: tuple[str, ...], entered: tuple[str, ...], passed: bool,
) -> None:
    run_id = str(uuid4())
    events = _receipt_events(run_id, actual="backup", failed_model="backup")
    envelope = events[1]["artifact"]
    receipt = envelope["content"]["calls"][0]["receipt"]
    template = receipt["attempts"][0]
    receipt["attempted_logical_models"] = list(declared)
    receipt["attempts"] = [
        {**copy.deepcopy(template), "ordinal": index + 1,
         "provenance": {**template["provenance"], "logical_model": model,
                        "deployment_id": f"deployment-{index // 2}"}}
        for index, model in enumerate(entered)
    ]
    # The shared receipt contract checks membership, not transport model ordering.
    GatewayFailureReceipt.from_payload(receipt)
    envelope.pop("content_sha256")
    events[1]["artifact"] = Artifact.from_payload(envelope).to_payload()
    for event in events[1:]:
        event["payload"]["attempted_logical_models"] = list(declared)
    completion = direct_model_events(run_id, "backup", requested_model="deepseek")
    for event in completion[1:]:
        event["sequence"] += 2
    evidence = _single([*events, *completion[1:]], None)
    assert evidence["ok"] is passed, evidence["errors"]


def test_unselected_crew_proof_cannot_disagree_with_existing_actor_start() -> None:
    events = crew_model_events(str(uuid4()))
    events[0]["payload"]["logical_model"] = "other"
    assert _single(events, None)["ok"] is False


@pytest.mark.parametrize("selected", [None, "deepseek"])
def test_crew_fallback_matches_requested_start_not_actual_model(selected: str | None) -> None:
    events = crew_model_events(str(uuid4()), requested_model="other")
    evidence = _single(events, selected)
    assert evidence["ok"] is (selected is None), evidence["errors"]


@pytest.mark.parametrize("selected", [None, "deepseek"])
@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("payload_shape", ["model_only", "empty"])
def test_typed_scope_cannot_hide_behind_missing_linkage(
    selected: str | None, unknown: bool, payload_shape: str,
) -> None:
    run_id = str(uuid4())
    scope_event = _receipt_events(run_id)[1]
    envelope = scope_event["artifact"]
    if unknown:
        envelope["content"]["calls"][0]["outcome"] = "unknown"
        envelope.pop("content_sha256")
        scope_event["artifact"] = Artifact.from_payload(envelope).to_payload()
    scope_event["payload"] = {} if payload_shape == "empty" else {
        "artifact_id": envelope["id"], "logical_model": "deepseek",
    }
    scope_event["sequence"] = 5
    assert _single([*direct_model_events(run_id), scope_event], selected)["ok"] is False


@pytest.mark.parametrize("invalid", [
    "requested", "first_attempt", "last_attempt", "allow_fallback", "provenance", "incomplete",
])
def test_unselected_fallback_receipt_requires_consistent_complete_history(invalid: str) -> None:
    run_id = str(uuid4())
    events = _receipt_events(run_id, actual="backup")
    envelope = events[1]["artifact"]
    receipt = envelope["content"]["calls"][-1]["receipt"]
    if invalid == "requested":
        receipt["requested_logical_model"] = "other"
    elif invalid == "first_attempt":
        receipt["attempted_logical_models"][0] = "other"
    elif invalid == "last_attempt":
        receipt["attempted_logical_models"][-1] = "other"
    elif invalid == "allow_fallback":
        receipt["allow_fallback"] = False
    elif invalid == "provenance":
        envelope["provenance"]["logical_model"] = "deepseek"
    else:
        envelope["content"]["history_complete"] = False
    envelope.pop("content_sha256")
    events[1]["artifact"] = Artifact.from_payload(envelope).to_payload()
    completion = direct_model_events(run_id, "backup", requested_model="deepseek")
    for event in completion[1:]:
        event["sequence"] += 2
    assert _single([*events, *completion[1:]], None)["ok"] is False


@pytest.mark.parametrize("selected", [None, "deepseek"])
@pytest.mark.parametrize("invalid", ["receipt_only", "incomplete", "unknown", "missing_receipt"])
def test_receipt_and_unknown_scope_fail_closed(selected: str | None, invalid: str) -> None:
    run_id = str(uuid4())
    events = _receipt_events(run_id)
    if invalid != "receipt_only":
        completion = direct_model_events(run_id)
        for event in completion[1:]:
            event["sequence"] += 2
        events.extend(completion[1:])
    if invalid == "incomplete":
        events.append({"run_id": run_id, "kind": "model.scope_incomplete",
                       "payload": {"logical_model": "deepseek"}})
    elif invalid == "unknown":
        envelope = events[1]["artifact"]
        envelope["content"]["calls"][0]["outcome"] = "unknown"
        envelope.pop("content_sha256")
        events[1]["artifact"] = Artifact.from_payload(envelope).to_payload()
    elif invalid == "missing_receipt":
        events = [event for event in events if event["kind"] != "model.failure_receipt"]
    assert _single(events, selected)["ok"] is False


@pytest.mark.parametrize("selected", [None, "deepseek"])
@pytest.mark.parametrize("broken", [None, "original", "repair", "result"])
def test_every_original_result_and_repair_run_needs_its_own_proof(
    selected: str | None, broken: str | None,
) -> None:
    ids = {key: str(uuid4()) for key in ("original", "repair", "result")}
    runs = {
        run_id: ("completed", [_bare_completion(run_id)] if key == broken else direct_model_events(run_id))
        for key, run_id in ids.items()
    }
    evidence = _collect(runs, original=ids["original"], result=ids["result"],
                        repairs=(ids["repair"],), selected=selected)
    assert evidence["ok"] is (broken is None), evidence["errors"]


def _rebuild_case(module: Any, case: dict[str, Any], selected: str | None) -> dict[str, Any]:
    result = ProjectScaleCaseResult(**{
        field.name: case["run"][field.name]
        for field in fields(ProjectScaleCaseResult) if field.name in case["run"]
    })
    return cast(dict[str, Any], module.build_case_report(
        scale=case["scale"], project=case["project"], conversation=case["conversation"],
        result=result, public_artifacts=case["public_artifacts"],
        dynamic_web_preview=case["dynamic_web_preview"], logical_model=selected,
        model_scope_evidence=case.get("model_scope_evidence"),
    ))


def _report(selected: str | None = None) -> dict[str, Any]:
    # Reuse only unrelated matrix/browser scaffolding; replace all model proof fixtures.
    from tests.unit.test_real_user_four_scale_acceptance_script import _pending_automated_report

    module = _load_script()
    report = _pending_automated_report(selected)
    for index, case in enumerate(report["cases"]):
        case["model_scope_evidence"] = model_scope_evidence(case["run"]["run_id"], selected or "deepseek")
        report["cases"][index] = _rebuild_case(module, case, selected)
    return report


@pytest.mark.parametrize("scope", [None, {"ok": False, "errors": ["unavailable"]}])
def test_unselected_build_cannot_skip_scope(scope: object) -> None:
    module = _load_script()
    case = _report()["cases"][0]
    case["model_scope_evidence"] = scope
    rebuilt = _rebuild_case(module, case, None)
    assert rebuilt["core_acceptance_ok"] is False
    assert rebuilt["success_basis"]["model_scope"] is False


@pytest.mark.parametrize("selected", [None, "deepseek"])
@pytest.mark.parametrize("tamper", [None, "missing", "bare", "flag_only", "other_run"])
def test_resume_and_finalizer_require_retained_proof_not_regenerated_flags(
    selected: str | None, tamper: str | None,
) -> None:
    from tests.unit.test_real_user_four_scale_acceptance_script import _real_device_evidence

    module = _load_script()
    report = _report(selected)
    case = report["cases"][0]
    assert module._has_complete_core_evidence(
        case, safe_execution_id="matrix-123", case_key="auto-small",
    ) is True
    if tamper == "missing":
        case.pop("model_scope_evidence", None)
    elif tamper == "flag_only":
        case["model_scope_evidence"] = {"ok": True, "errors": []}
    elif tamper in {"bare", "other_run"}:
        # Install evidence explicitly even on the legacy unscoped path.
        scope = model_scope_evidence(case["run"]["run_id"], selected or "deepseek")
        case["model_scope_evidence"] = scope
        scope["runs"][0]["model_events"] = [_bare_completion(case["run"]["run_id"])]
        if tamper == "other_run":
            scope["runs"][0]["model_events"] = direct_model_events("unrelated")
    if tamper is not None:
        case.update(core_acceptance_ok=True, automated_acceptance_complete=True,
                    status="pending_real_device")
    complete = module._has_complete_core_evidence(
        case, safe_execution_id="matrix-123", case_key="auto-small",
    )
    assert complete is (tamper is None)
    before = copy.deepcopy(report)
    device = _real_device_evidence("matrix-123", selected)
    if tamper is None:
        assert module.finalize_real_device_acceptance(report, device)["acceptance_complete"] is True
    else:
        with pytest.raises(ValueError, match="core"):
            module.finalize_real_device_acceptance(report, device)
    assert report == before


@pytest.mark.parametrize("delivery,passed", [("valid", True), ("bare", False), ("missing", False)])
def test_matrix_collects_completion_scope_without_model_selection(
    monkeypatch: pytest.MonkeyPatch, delivery: str, passed: bool,
) -> None:
    from tests.unit.test_real_user_four_scale_acceptance_script import matrix_harness, run_matrix

    module, delegate, plans, _ = cast(Any, matrix_harness).__wrapped__(monkeypatch)
    monkeypatch.setattr(module, "_ACCEPTANCE_CASES", module._ACCEPTANCE_CASES[:1])
    delegate.model_events["run-1"] = (
        direct_model_events("run-1") if delivery == "valid"
        else [_bare_completion("run-1")] if delivery == "bare" else []
    )
    report = run_matrix(module, delegate)
    case = report["cases"][0]
    assert "model_scope_evidence" in case
    assert case["model_scope_evidence"]["ok"] is passed
    assert case["core_acceptance_ok"] is passed
    assert "model_profile" not in report
    assert "direct_model" not in plans[0].requests[0].body
    assert "allowed_models" not in plans[0].requests[0].body
