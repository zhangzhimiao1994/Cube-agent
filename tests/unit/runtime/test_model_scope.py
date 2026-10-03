import copy
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelMessage, ModelRequest, ModelResponse
from agent_hub.runtime import model_scope
from agent_hub.runtime.contracts import Artifact
from agent_hub.runtime.model_scope import ModelScopeTracker, validate_model_scope_artifact
from tests.unit.test_real_user_four_scale_acceptance_script import load_script


def receive(tracker: ModelScopeTracker, *, foreign: bool = False) -> None:
    index = tracker.begin()
    tracker.received(index, ModelRequest(logical_model="primary", messages=(
        ModelMessage(role="user", content="private prompt"),
    )), GatewayCompletion(
        response=ModelResponse(text="private body"), logical_model="primary",
        deployment_id="primary", provider_id="provider", provider_model="provider/primary",
        attempted_logical_models=("primary", "foreign", "primary") if foreign else ("primary",),
    ))


def test_successful_multi_call_history_is_preserved() -> None:
    run = uuid4()
    tracker = ModelScopeTracker(run_id=run, tenant_id=uuid4(), logical_model="primary")
    receive(tracker, foreign=True)
    receive(tracker)
    artifacts = tracker.artifacts()
    assert artifacts
    assert artifacts[0].content["call_count"] == 2
    assert "foreign" in str(artifacts[0].content)


def test_many_successful_calls_are_sharded_not_silently_lost() -> None:
    run = uuid4()
    tracker = ModelScopeTracker(run_id=run, tenant_id=uuid4(), logical_model="primary")
    for _ in range(350):
        receive(tracker)
    artifacts = tracker.artifacts()
    assert len(artifacts) > 1
    contents = [validate_model_scope_artifact(artifact, str(run)) for artifact in artifacts]
    assert sum(len(cast(list[dict[str, Any]], content["calls"])) for content in contents) == 350
    assert all(content["call_count"] == 350 and content["part_count"] == len(artifacts)
               for content in contents)
    assert not tracker.incomplete


def test_unknown_call_cannot_disappear_behind_success() -> None:
    tracker = ModelScopeTracker(run_id=uuid4(), tenant_id=uuid4(), logical_model="primary")
    tracker.failed(tracker.begin(), RuntimeError("private body"))
    receive(tracker)
    assert tracker.incomplete


def test_scope_metadata_memory_is_bounded_without_stopping_calls() -> None:
    tracker = ModelScopeTracker(run_id=uuid4(), tenant_id=uuid4(), logical_model="primary")
    for _ in range(20_000):
        receive(tracker)
    assert tracker.incomplete
    assert tracker.call_count == 20_000
    assert len(tracker._calls) < 20_000


@pytest.mark.parametrize("variant", ["valid", "missing", "duplicate", "gap", "duplicate_call", "scope_replay"])
def test_scope_collector_requires_all_linked_parts(variant: str) -> None:
    module = load_script()
    run_id = str(uuid4())
    tracker = ModelScopeTracker(run_id=uuid4(), tenant_id=uuid4(), logical_model="primary")
    run_id = str(tracker._run_id)
    for _ in range(350):
        receive(tracker)
    artifacts = list(tracker.artifacts())
    assert len(artifacts) > 1
    if variant == "missing":
        artifacts.pop()
    elif variant in {"duplicate", "gap", "duplicate_call"}:
        raw = copy.deepcopy(artifacts[-1].to_payload())
        content = cast(dict[str, Any], raw["content"])
        if variant == "duplicate":
            content["part_index"] = 1
        elif variant == "gap":
            content["call_offset"] -= 1
        else:
            content["calls"][0]["receipt"]["call_id"] = cast(dict[str, Any],
                artifacts[0].to_payload()["content"])["calls"][0]["receipt"]["call_id"]
        raw.pop("content_sha256")
        artifacts[-1] = Artifact.from_payload(raw)
    elif variant == "scope_replay":
        scope_id = str(uuid4())
        extra = []
        for artifact in artifacts:
            raw = copy.deepcopy(artifact.to_payload())
            raw["id"] = str(uuid4())
            cast(dict[str, Any], raw["content"])["scope_id"] = scope_id
            raw.pop("content_sha256")
            extra.append(Artifact.from_payload(raw))
        artifacts.extend(extra)
    events: list[dict[str, Any]] = [{"kind": "model.started", "run_id": run_id,
        "sequence": 1, "actor": "main_agent", "payload": {"logical_model": "primary"}}]
    for artifact in artifacts:
        payload = {"artifact_id": str(artifact.id), "logical_model": "primary",
                   "requested_logical_model": "primary", "attempted_logical_models": ["primary"],
                   "deployment": "primary", "provider": "provider", "upstream_model": "provider/primary"}
        event = {"kind": "artifact.created", "run_id": run_id, "sequence": len(events) + 1,
                 "actor": "main_agent", "artifact": artifact.to_payload(), "payload": payload}
        event["model_artifact"] = module._public_model_artifact(event)
        events.append(event)
        events.append({"kind": "model.failure_receipt", "run_id": run_id,
                       "sequence": len(events) + 1, "actor": "main_agent", "payload": payload})
    assert module._has_failed_attempt_scope(events) is (variant == "valid")
    assert not module._has_direct_model_completion(events)


def scope_parts() -> list[dict[str, object]]:
    run_id = uuid4()
    tracker = ModelScopeTracker(run_id=run_id, tenant_id=uuid4(), logical_model="primary")
    for _ in range(35):
        receive(tracker)
    return [validate_model_scope_artifact(artifact, str(run_id)) for artifact in tracker.artifacts()]


@pytest.mark.parametrize("container", [list, tuple])
def test_shared_parts_validation_accepts_complete_shuffled_parts_without_mutation(container: Any) -> None:
    validate = getattr(model_scope, "validate_model_scope_parts", None)
    assert callable(validate), "shared scope parts validator is missing"
    parts = list(reversed(scope_parts()))
    before = copy.deepcopy(parts)
    validate(container(parts))
    assert parts == before
    validate(container())


@pytest.mark.parametrize("variant", [
    "missing", "duplicate_part", "offset_gap", "count", "part_count", "tenant", "run",
    "model", "duplicate_call", "scope_replay", "extra", "incomplete", "unknown_call",
    "foreign_history", "call_id", "bool_count", "bad_provider",
])
def test_shared_parts_validation_rejects_incomplete_or_replayed_scope(variant: str) -> None:
    validate = getattr(model_scope, "validate_model_scope_parts", None)
    assert callable(validate), "shared scope parts validator is missing"
    parts = cast(list[dict[str, Any]], scope_parts())
    if variant == "missing":
        parts.pop()
    elif variant == "duplicate_part":
        parts.append(copy.deepcopy(parts[-1]))
    elif variant == "offset_gap":
        parts[-1]["call_offset"] -= 1
    elif variant in {"count", "part_count"}:
        key = "call_count" if variant == "count" else "part_count"
        parts[-1][key] += 1
    elif variant in {"tenant", "run"}:
        parts[-1][f"{variant}_id"] = str(uuid4())
    elif variant == "model":
        parts[-1]["requested_logical_model"] = "foreign"
    elif variant == "duplicate_call":
        parts[-1]["calls"][0]["receipt"]["call_id"] = parts[0]["calls"][0]["receipt"]["call_id"]
    elif variant == "scope_replay":
        replay = copy.deepcopy(parts)
        scope_id = str(uuid4())
        for part in replay:
            part["scope_id"] = scope_id
        parts.extend(replay)
    elif variant == "extra":
        parts[-1]["private_body"] = "Authorization: private input"
    elif variant == "incomplete":
        parts[-1]["history_complete"] = False
    elif variant == "unknown_call":
        parts[-1]["calls"][0]["outcome"] = "unknown"
    elif variant == "foreign_history":
        parts[-1]["calls"][0]["receipt"]["attempted_logical_models"] = ["foreign", "primary"]
    elif variant == "call_id":
        parts[-1]["calls"][0]["receipt"]["call_id"] = "Authorization: private input"
    elif variant == "bool_count":
        parts[-1]["call_count"] = True
    elif variant == "bad_provider":
        parts[-1]["calls"][0]["receipt"]["provenance"]["provider_model"] = "private body"
    with pytest.raises(ValueError, match="^model attempt scope parts are invalid or incomplete$"):
        validate(parts)


def test_shared_parts_validation_accepts_distinct_complete_child_scopes() -> None:
    validate = getattr(model_scope, "validate_model_scope_parts", None)
    assert callable(validate), "shared scope parts validator is missing"
    validate([*scope_parts(), *scope_parts()])
