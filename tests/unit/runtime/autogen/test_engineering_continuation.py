"""Opted-in writer hints derive from scoped causal evidence, never new authority."""

import hashlib
from collections.abc import Mapping
from typing import cast

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.autogen.engineering_continuation import (
    ENGINEERING_CONTINUATION_POLICY,
    ENGINEERING_CONTINUATION_VERSION,
    draft_action,
    engineering_continuation_selected,
    writer_can_continue,
)
from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext
from tests.unit.runtime.autogen.test_engineering_delivery import RUN, append_result
from tests.unit.runtime.test_generated_draft_autogen import EMPTY_HASH, context, rejection

TOOLS = {"engineer": ("project.inspect", "workspace.list", "workspace.read",
                       "workspace.write_text", "project.verify", "workspace.bundle")}


def ctx() -> TaskContext:
    original = context()
    return original.model_copy(update={"run_id": RUN, "routing_decision": {
        **original.routing_decision, "engineering_action_version": 1,
        "engineering_delivery_contract_version": 1, ENGINEERING_CONTINUATION_POLICY: 1,
    }})


def metadata(path: str = "src/lib/intsum.cjs", digest: str = "a", size: int = 10) -> dict[str, JsonValue]:
    return {"path": path, "sha256": digest * 64, "size_bytes": size}


def inspected(*, existing: bool = False):
    current = ctx()
    rows, artifacts = [], []
    append_result(rows, artifacts, "project.inspect", {
        "schema_version": 1, "scope": {"tenant_id": str(current.tenant_id), "run_id": str(RUN),
            "project_id": "draft-project", "workspace_session_id": "draft-session"},
        "manifests": ({"path": "package.json", "status": "observed", "sha256": "f" * 64,
                       "observed_bytes": 10},) if existing else (),
        "operations": {"build": "declared", "test": "declared"} if existing else {}, "issues": (),
    })
    return current, rows, artifacts


def listed(rows, artifacts, *files) -> None:
    append_result(rows, artifacts, "workspace.list", {"workspace_files": files})


def written(rows, artifacts, file) -> None:
    append_result(rows, artifacts, "workspace.write_text", {"workspace_files": (file,)})


def draft(current, rows, artifacts, *, observation="a") -> None:
    value = rejection(current, actor="engineer", observation=observation * 64)
    value.update(reason="generated_acceptance_manifest_required", issues=("manifest_missing",))
    append_result(rows, artifacts, "project.verify", {
        "status": "failed", "untrusted": True, "generated_draft_rejection": value,
    }, status="failed")
    old = artifacts[-1]
    changed = Artifact(id=old.id, type=old.type, producer=old.producer,
        content={**old.content, "arguments_sha256": EMPTY_HASH, "call_id": f"draft-{len(rows)}"})
    artifacts[-1] = changed
    rows[-1].update(arguments_hash=EMPTY_HASH, artifact_sha256=changed.content_sha256,
        idempotency_key=hashlib.sha256(
            f"{RUN}:engineer:{len(rows)-1}:project.verify:{EMPTY_HASH}".encode()).hexdigest())


@pytest.mark.parametrize("field,value", [
    ("engineering_action_version", None), ("engineering_action_version", True),
    ("generated_project_contract_version", 0), ("generated_project_contract_version", True),
    ("engineering_writer_continuation_version", None), ("engineering_writer_continuation_version", True),
    ("engineering_writer_continuation_version", "1"), ("engineering_writer_continuation_version", 2),
])
def test_only_exact_new_policy_is_selected(field, value) -> None:
    current, rows, artifacts = inspected()
    current = current.model_copy(update={"routing_decision": {**current.routing_decision, field: value}})
    assert not engineering_continuation_selected(current)
    assert draft_action(current, rows, artifacts, TOOLS) is None
    assert not writer_can_continue(current, "engineer", rows, artifacts, TOOLS)


def test_selected_requires_discuss_and_exports_stable_policy() -> None:
    assert ENGINEERING_CONTINUATION_POLICY == "engineering_writer_continuation_version"
    assert ENGINEERING_CONTINUATION_VERSION == 1
    assert engineering_continuation_selected(ctx())
    assert not engineering_continuation_selected(ctx().model_copy(update={"mode": TaskMode.DIRECT}))


def test_initial_list_then_missing_acceptance_only_until_observed_or_written() -> None:
    current, rows, artifacts = inspected()
    before = repr((current, rows, artifacts))
    assert draft_action(current, rows, artifacts, TOOLS) == "workspace.list"
    assert repr((current, rows, artifacts)) == before
    listed(rows, artifacts, metadata())
    assert draft_action(current, rows, artifacts, TOOLS) == "workspace.write_text"
    written(rows, artifacts, metadata(digest="b"))
    assert draft_action(current, rows, artifacts, TOOLS) == "workspace.write_text"
    written(rows, artifacts, metadata("ACCEPTANCE.json"))
    assert draft_action(current, rows, artifacts, TOOLS) is None


def test_latest_list_is_authoritative_and_never_overwrites_existing_acceptance() -> None:
    current, rows, artifacts = inspected()
    listed(rows, artifacts, metadata("ACCEPTANCE.json"))
    assert draft_action(current, rows, artifacts, TOOLS) is None
    listed(rows, artifacts)
    assert draft_action(current, rows, artifacts, TOOLS) == "workspace.write_text"


@pytest.mark.parametrize("state", ["uninspected", "existing", "verification", "prepared"])
def test_no_forced_draft_after_existing_project_capture_or_verification(state) -> None:
    current, rows, artifacts = inspected(existing=state == "existing")
    if state == "uninspected":
        rows, artifacts = [], []
    elif state == "verification":
        draft(current, rows, artifacts)
    elif state == "prepared":
        current = current.model_copy(update={"routing_decision": {
            **current.routing_decision, "generated_project_preparation": {"phase": "captured"}}})
    assert draft_action(current, rows, artifacts, TOOLS) is None


def test_writer_continuation_counts_semantics_not_artifact_ids_or_summaries() -> None:
    current, rows, artifacts = inspected()
    assert writer_can_continue(current, "engineer", rows, artifacts, TOOLS)
    listed(rows, artifacts, metadata())
    assert writer_can_continue(current, "engineer", rows, artifacts, TOOLS)
    listed(rows, artifacts, metadata())
    assert not writer_can_continue(current, "engineer", rows, artifacts, TOOLS)
    written(rows, artifacts, metadata())
    assert not writer_can_continue(current, "engineer", rows, artifacts, TOOLS)
    written(rows, artifacts, metadata(digest="b"))
    assert writer_can_continue(current, "engineer", rows, artifacts, TOOLS)
    written(rows, artifacts, metadata(digest="b"))
    assert not writer_can_continue(current, "engineer", rows, artifacts, TOOLS)
    append_result(rows, artifacts, "workspace.read", {"path": "src/lib/intsum.cjs", "text": "contents"})
    assert writer_can_continue(current, "engineer", rows, artifacts, TOOLS)
    append_result(rows, artifacts, "workspace.read", {
        "path": "src/lib/intsum.cjs", "text": "contents", "summary": "new request ID"})
    assert not writer_can_continue(current, "engineer", rows, artifacts, TOOLS)
    append_result(rows, artifacts, "workspace.read", {"path": "src/lib/intsum.cjs", "text": "changed"})
    assert writer_can_continue(current, "engineer", rows, artifacts, TOOLS)


def test_typed_draft_can_correct_once_per_new_observation_and_restore_is_derived() -> None:
    current, rows, artifacts = inspected()
    draft(current, rows, artifacts)
    assert writer_can_continue(current, "engineer", rows, artifacts, TOOLS)
    assert writer_can_continue(current.model_copy(), "engineer", [dict(row) for row in rows],
                               list(artifacts), TOOLS)
    draft(current, rows, artifacts)
    assert not writer_can_continue(current, "engineer", rows, artifacts, TOOLS)
    draft(current, rows, artifacts, observation="b")
    assert writer_can_continue(current, "engineer", rows, artifacts, TOOLS)


def test_bundle_ends_and_other_or_multiple_writers_do_not_continue() -> None:
    current, rows, artifacts = inspected()
    assert not writer_can_continue(current, "reviewer", rows, artifacts, TOOLS)
    assert not writer_can_continue(current, "engineer", rows, artifacts,
                                   {**TOOLS, "other": TOOLS["engineer"]})
    append_result(rows, artifacts, "workspace.bundle", {"summary": "not delivery credit"})
    assert not writer_can_continue(current, "engineer", rows, artifacts, TOOLS)


@pytest.mark.parametrize("file", [
    {"path": "../ACCEPTANCE.json", "sha256": "a" * 64, "size_bytes": 1},
    {"path": "ACCEPTANCE.json", "sha256": "bad", "size_bytes": 1},
    {"path": "ACCEPTANCE.json", "sha256": "a" * 64, "size_bytes": True},
    {"path": "ACCEPTANCE.json", "sha256": "a" * 64, "size_bytes": -1},
    "ACCEPTANCE.json",
])
def test_invalid_inventory_is_fail_closed(file) -> None:
    current, rows, artifacts = inspected()
    listed(rows, artifacts, file)
    for call in (lambda: draft_action(current, rows, artifacts, TOOLS),
                 lambda: writer_can_continue(current, "engineer", rows, artifacts, TOOLS)):
        with pytest.raises(ValueError):
            call()


@pytest.mark.parametrize("defect", ["running", "UNKNOWN", "position", "hash", "scope", "duplicate"])
def test_malformed_or_uncertain_prefix_never_yields_an_action(defect) -> None:
    current, rows, artifacts = inspected()
    if defect in {"running", "UNKNOWN"}:
        rows[-1]["status"] = defect
    elif defect == "position":
        rows[-1]["position"] = 1
    elif defect == "hash":
        rows[-1]["artifact_sha256"] = "f" * 64
    elif defect == "scope":
        result = cast(Mapping[str, JsonValue], artifacts[-1].content["result"])
        scope = cast(Mapping[str, JsonValue], result["scope"])
        changed = Artifact(id=artifacts[-1].id, type="tool_result", producer="engineer",
            content={**artifacts[-1].content, "result": {**result, "scope": {**scope, "run_id": "other"}}})
        artifacts[-1] = changed
        rows[-1]["artifact_sha256"] = changed.content_sha256
    else:
        artifacts.append(artifacts[-1])
    with pytest.raises(ValueError):
        draft_action(current, rows, artifacts, TOOLS)
    with pytest.raises(ValueError):
        writer_can_continue(current, "engineer", rows, artifacts, TOOLS)


@pytest.mark.parametrize("result", [
    {}, {"workspace_files": "missing"},
    {"workspace_files": (metadata(), metadata())},
    {"workspace_files": (), "truncated": True},
    {"workspace_files": (), "truncated": 0},
])
def test_inventory_must_be_complete_unique_path_metadata(result) -> None:
    current, rows, artifacts = inspected()
    append_result(rows, artifacts, "workspace.list", result)
    with pytest.raises(ValueError):
        draft_action(current, rows, artifacts, TOOLS)
    with pytest.raises(ValueError):
        writer_can_continue(current, "engineer", rows, artifacts, TOOLS)


def test_unselected_policy_never_reads_malformed_evidence() -> None:
    current = ctx()
    route = dict(current.routing_decision)
    del route[ENGINEERING_CONTINUATION_POLICY]
    current = current.model_copy(update={"routing_decision": route})
    malformed = [{"status": "UNKNOWN"}]
    assert draft_action(current, malformed, [], TOOLS) is None
    assert not writer_can_continue(current, "engineer", malformed, [], TOOLS)


def test_future_artifacts_are_not_observations_until_their_ledger_prefix_is_consumed() -> None:
    current, rows, artifacts = inspected()
    prefix = len(rows)
    listed(rows, artifacts, metadata("ACCEPTANCE.json"))
    assert draft_action(current, rows[:prefix], artifacts, TOOLS) == "workspace.list"
    assert draft_action(current, rows, artifacts, TOOLS) is None
    assert not writer_can_continue(current, "reviewer", rows, artifacts,
                                   {**TOOLS, "reviewer": ("workspace.list",)})


def test_missing_list_permission_and_multiple_writers_cannot_add_tool_authority() -> None:
    current, rows, artifacts = inspected()
    no_list = {"engineer": tuple(name for name in TOOLS["engineer"] if name != "workspace.list")}
    assert draft_action(current, rows, artifacts, no_list) is None
    assert draft_action(current, rows, artifacts, {**TOOLS, "other": TOOLS["engineer"]}) is None


def test_changed_inventory_and_other_writer_tail_are_distinct() -> None:
    current, rows, artifacts = inspected()
    listed(rows, artifacts, metadata())
    listed(rows, artifacts, metadata(digest="b"))
    assert writer_can_continue(current, "engineer", rows, artifacts, TOOLS)
    tools = {**TOOLS, "reviewer": ("workspace.list",)}
    old = artifacts[-1]
    changed = Artifact(id=old.id, type=old.type, producer="reviewer", content=old.content)
    artifacts[-1] = changed
    rows[-1].update(actor="reviewer", artifact_sha256=changed.content_sha256,
        idempotency_key=hashlib.sha256(
            f"{RUN}:reviewer:{len(rows)-1}:workspace.list:{'c' * 64}".encode()).hexdigest())
    assert not writer_can_continue(current, "engineer", rows, artifacts, tools)


@pytest.mark.parametrize("result", [{"path": "src/a.cjs"}, {"path": "src/a.cjs", "text": 1}])
def test_malformed_read_result_is_rejected_consistently(result) -> None:
    current, rows, artifacts = inspected()
    append_result(rows, artifacts, "workspace.read", result)
    with pytest.raises(ValueError):
        draft_action(current, rows, artifacts, TOOLS)
    with pytest.raises(ValueError):
        writer_can_continue(current, "engineer", rows, artifacts, TOOLS)
