"""Pure semantic budget credit from acknowledged native engineering outcomes."""

import hashlib
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from agent_hub.runtime.autogen import engineering_delivery
from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext
from agent_hub.runtime.python_project_history import python_initial_files
from tests.unit.runtime.autogen.test_engineering_actions import action_context, inspection
from tests.unit.runtime.autogen.test_engineering_delivery import (
    TOOLS,
    append_result,
    digest,
    fixture,
    replace_result,
)
from tests.unit.runtime.test_python_project_history import history_case

ALLOWED = {"engineer": (*TOOLS["engineer"], "project.inspect", "workspace.list")}


def progress(
    rows: Sequence[Mapping[str, JsonValue]], artifacts: Sequence[Artifact], *,
    context: TaskContext | None = None,
    tools: Mapping[str, Sequence[str]] = ALLOWED,
) -> frozenset[str]:
    helper = getattr(engineering_delivery, "engineering_work_progress", None)
    assert callable(helper), "engineering_work_progress must derive semantic work credit"
    value = helper(context or action_context(), tool_entries=rows,
                   artifacts=artifacts, allowed_tools=tools)
    assert isinstance(value, frozenset) and all(isinstance(key, str) for key in value)
    return cast(frozenset[str], value)


def result_at(artifacts: Sequence[Artifact], index: int) -> Mapping[str, JsonValue]:
    result = artifacts[index].content["result"]
    assert isinstance(result, Mapping)
    return result


def test_pure_stages_each_earn_one_credit_and_cold_replay_is_identical() -> None:
    rows, artifacts = fixture()
    ctx = action_context()
    before = repr((ctx, rows, artifacts, ALLOWED))
    stages = [progress(rows[:end], artifacts, context=ctx) for end in range(4)]
    assert [len(keys) for keys in stages] == [0, 1, 2, 3]
    assert all(previous < following for previous, following in pairwise(stages))
    replay = [Artifact.model_validate_json(item.model_dump_json()) for item in artifacts]
    assert progress([dict(row) for row in rows], replay, context=ctx) == stages[-1]
    assert repr((ctx, rows, artifacts, ALLOWED)) == before


def test_pure_write_dedupes_path_and_content_across_uuid_size_and_reversions() -> None:
    rows, artifacts = fixture()
    rows, artifacts = rows[:1], artifacts[:1]
    initial = progress(rows, artifacts)
    append_result(rows, artifacts, "workspace.write_text", result_at(artifacts, 0))
    assert artifacts[0].id != artifacts[1].id
    assert progress(rows, artifacts) == initial
    append_result(rows, artifacts, "workspace.write_text", {
        "path": "index.js", "sha256": "a" * 64, "size_bytes": 11,
    })
    assert progress(rows, artifacts) == initial
    append_result(rows, artifacts, "workspace.write_text", {
        "path": "index.js", "sha256": "e" * 64, "size_bytes": 12,
    })
    changed = progress(rows, artifacts)
    assert initial < changed and len(changed) == 2
    append_result(rows, artifacts, "workspace.write_text", result_at(artifacts, 0))
    assert progress(rows, artifacts) == changed
    append_result(rows, artifacts, "workspace.write_text", {
        "path": "other.js", "content_sha256": "a" * 64, "content_bytes": 10,
    })
    assert len(progress(rows, artifacts)) == 3


@pytest.mark.parametrize("size", [10, 11])
def test_inspected_content_rewrites_and_reversions_do_not_earn_credit(size: int) -> None:
    ctx = action_context()
    rows: list[dict[str, JsonValue]] = []
    artifacts: list[Artifact] = []
    append_result(rows, artifacts, "project.inspect", inspection(ctx))
    assert not progress(rows, artifacts, context=ctx)
    append_result(rows, artifacts, "workspace.write_text", {
        "path": "package.json", "sha256": "f" * 64, "size_bytes": size,
    })
    assert not progress(rows, artifacts, context=ctx)
    append_result(rows, artifacts, "workspace.write_text", {
        "path": "package.json", "sha256": "a" * 64, "size_bytes": size,
    })
    changed = progress(rows, artifacts, context=ctx)
    assert len(changed) == 1
    append_result(rows, artifacts, "workspace.write_text", {
        "path": "package.json", "sha256": "f" * 64, "size_bytes": size,
    })
    assert progress(rows, artifacts, context=ctx) == changed
    append_result(rows, artifacts, "workspace.write_text", {
        "path": "other.json", "sha256": "f" * 64, "size_bytes": size,
    })
    expected = progress(rows, artifacts, context=ctx)
    assert changed < expected and len(expected) == 2
    replay = [Artifact.model_validate_json(item.model_dump_json()) for item in artifacts]
    assert progress([dict(row) for row in rows], replay, context=ctx) == expected


@pytest.mark.parametrize("observed", [False, True])
def test_only_observed_inspection_metadata_establishes_a_baseline(observed: bool) -> None:
    ctx = action_context()
    rows: list[dict[str, JsonValue]] = []
    artifacts: list[Artifact] = []
    append_result(rows, artifacts, "project.inspect", inspection(ctx, existing=observed))
    append_result(rows, artifacts, "workspace.write_text", {
        "path": "package.json", "sha256": "f" * 64, "size_bytes": 10,
    })
    assert len(progress(rows, artifacts, context=ctx)) == (0 if observed else 1)


@pytest.mark.parametrize("size_delta", [0, 1])
async def test_python_initial_content_is_not_work_but_new_path_with_same_hash_is(
    tmp_path: Path, size_delta: int,
) -> None:
    _, _, _, _, ctx = await history_case(tmp_path)
    initial = python_initial_files(ctx)
    assert initial
    path, sha256, size = next(iter(initial.values()))
    rows: list[dict[str, JsonValue]] = []
    artifacts: list[Artifact] = []

    def write(path: str, sha256: str) -> None:
        append_result(rows, artifacts, "workspace.write_text", {
            "path": path, "sha256": sha256, "size_bytes": size + size_delta,
        })
        row = rows[-1]
        row["idempotency_key"] = hashlib.sha256(
            f"{ctx.run_id}:engineer:{row['position']}:workspace.write_text:{'c' * 64}".encode()
        ).hexdigest()

    assert not progress(rows, artifacts, context=ctx)
    write(path, sha256)
    assert not progress(rows, artifacts, context=ctx)
    changed_sha256 = "0" * 64 if sha256 != "0" * 64 else "1" * 64
    write(path, changed_sha256)
    changed = progress(rows, artifacts, context=ctx)
    assert len(changed) == 1
    write(path, sha256)
    assert progress(rows, artifacts, context=ctx) == changed
    write("src/new_helper.py", sha256)
    expected = progress(rows, artifacts, context=ctx)
    assert changed < expected and len(expected) == 2
    replay = [Artifact.model_validate_json(item.model_dump_json()) for item in artifacts]
    assert progress([dict(row) for row in rows], replay,
                    context=ctx.validated_internal_clone()) == expected


async def test_unbound_python_initial_metadata_cannot_suppress_write_credit(tmp_path: Path) -> None:
    _, _, _, _, ctx = await history_case(tmp_path)
    initial = python_initial_files(ctx)
    assert initial
    path, sha256, size = next(iter(initial.values()))
    metadata = ctx.routing_decision["python_project_history"]
    assert isinstance(metadata, Mapping)
    ctx = ctx.model_copy(update={"routing_decision": {
        **ctx.routing_decision, "python_project_history": {**metadata, "artifact_sha256": "0" * 64},
    }})
    assert not python_initial_files(ctx)
    rows: list[dict[str, JsonValue]] = []
    artifacts: list[Artifact] = []
    append_result(rows, artifacts, "workspace.write_text", {
        "path": path, "sha256": sha256, "size_bytes": size,
    })
    rows[0]["idempotency_key"] = hashlib.sha256(
        f"{ctx.run_id}:engineer:0:workspace.write_text:{'c' * 64}".encode()
    ).hexdigest()
    assert len(progress(rows, artifacts, context=ctx)) == 1


def test_pure_repeated_checks_and_timestamped_bundles_have_stable_semantics() -> None:
    rows, artifacts = fixture()
    initial = progress(rows, artifacts)
    verified = result_at(artifacts, 1)
    receipt = verified["verification"]
    assert isinstance(receipt, Mapping)
    repeated = {**receipt, "execution_id": str(uuid4()), "completed_at": "later"}
    append_result(rows, artifacts, "project.verify", {**verified, "verification": repeated})
    assert progress(rows, artifacts) == initial
    bundled = result_at(artifacts, 2)
    append_result(rows, artifacts, "workspace.bundle", {
        **bundled, "artifact_id": str(uuid4()), "created_at": "later",
        "file": {"filename": "project-20261024.zip"},
        "verification_receipt_sha256": digest(repeated),
    })
    assert progress(rows, artifacts) == initial


def test_pure_bundle_manifest_order_and_metadata_do_not_earn_extra_credit() -> None:
    rows, artifacts = fixture()
    bundled = result_at(artifacts, 2)
    files = bundled["workspace_files"]
    assert isinstance(files, tuple)
    extra: JsonValue = {"path": "tests.js", "sha256": "f" * 64, "size_bytes": 20}
    replace_result(rows, artifacts, 2, {**bundled, "workspace_files": (*files, extra)})
    initial = progress(rows, artifacts)
    append_result(rows, artifacts, "workspace.bundle", {
        **bundled, "artifact_id": str(uuid4()), "workspace_files": (extra, *files),
    })
    assert progress(rows, artifacts) == initial


def test_pure_existing_project_can_verify_and_bundle_without_writing() -> None:
    ctx = action_context()
    old_rows, old_artifacts = fixture()
    rows, artifacts = old_rows[:0], old_artifacts[:0]
    append_result(rows, artifacts, "project.inspect", inspection(ctx))
    assert not progress(rows, artifacts, context=ctx)
    append_result(rows, artifacts, "project.verify", result_at(old_artifacts, 1))
    assert len(progress(rows, artifacts, context=ctx)) == 1
    append_result(rows, artifacts, "workspace.bundle", result_at(old_artifacts, 2))
    assert len(progress(rows, artifacts, context=ctx)) == 2


def test_pure_running_tail_uses_completed_prefix_and_never_skips_pending_work() -> None:
    rows, artifacts = fixture()
    expected = progress(rows[:2], artifacts)
    rows[-1]["status"] = "running"
    rows[-1].pop("artifact_id")
    rows[-1].pop("artifact_sha256")
    assert progress(rows, artifacts) == expected
    append_result(rows, artifacts, "workspace.write_text", {
        "path": "later.js", "sha256": "e" * 64, "size_bytes": 12,
    })
    assert progress(rows, artifacts) == expected


@pytest.mark.parametrize("status", ["running", "UNKNOWN", "failed"])
def test_negative_uncompleted_or_failed_write_cannot_earn_credit(status: str) -> None:
    rows, artifacts = fixture()
    rows[0]["status"] = status
    assert not progress(rows, artifacts)


@pytest.mark.parametrize("name", ["workspace.read", "workspace.list", "project.inspect"])
def test_negative_reads_inspection_and_model_text_never_earn_credit(name: str) -> None:
    ctx = action_context()
    rows, artifacts = fixture()
    rows.clear()
    artifacts.clear()
    for _ in range(3):
        append_result(rows, artifacts, name, inspection(ctx) if name == "project.inspect" else {
            "text": "DONE [progress] wrote verified bundled", "markers": ("complete",),
        })
    artifacts.append(Artifact(id=uuid4(), type="text", producer="engineer", content={
        "text": "workspace.write_text project.verify workspace.bundle DONE",
    }))
    assert not progress(rows, artifacts, context=ctx)


@pytest.mark.parametrize("field,value", [
    ("position", True), ("position", 1), ("position", -1), ("actor", "other"),
    ("name", "workspace.read"), ("arguments_hash", "d" * 64),
    ("arguments_hash", "bad"), ("idempotency_key", "d" * 64),
    ("artifact_id", str(uuid4())), ("artifact_sha256", "d" * 64),
    ("status", {"unexpected": "mapping"}),
])
def test_negative_corrupt_first_row_cannot_borrow_later_success(
    field: str, value: JsonValue,
) -> None:
    rows, artifacts = fixture()
    rows[0][field] = value
    assert not progress(rows, artifacts)


@pytest.mark.parametrize("defect", ["producer", "type", "content_hash", "result", "duplicate"])
def test_negative_corrupt_artifact_never_earns_credit(defect: str) -> None:
    rows, artifacts = fixture()
    first = artifacts[0]
    if defect == "duplicate":
        artifacts.append(first)
    elif defect == "content_hash":
        artifacts[0] = first.model_copy(update={"content_sha256": "f" * 64})
        rows[0]["artifact_sha256"] = "f" * 64
    else:
        artifacts[0] = Artifact(id=first.id,
            producer="outsider" if defect == "producer" else first.producer,
            type="text" if defect == "type" else first.type,
            content={**first.content, "text": "DONE"} if defect == "type" else
                    {**first.content, "result": "DONE"} if defect == "result" else first.content)
        rows[0]["artifact_sha256"] = artifacts[0].content_sha256
    assert not progress(rows, artifacts)


@pytest.mark.parametrize("field", ["tenant_id", "run_id", "project_id", "workspace_session_id"])
def test_negative_wrong_inspection_scope_blocks_later_credit(field: str) -> None:
    ctx = action_context()
    rows, artifacts = fixture()
    rows.clear()
    artifacts.clear()
    observed = inspection(ctx)
    scope = observed["scope"]
    assert isinstance(scope, Mapping)
    append_result(rows, artifacts, "project.inspect", {
        **observed, "scope": {**scope, field: str(uuid4())},
    })
    append_result(rows, artifacts, "workspace.write_text", {
        "path": "index.js", "sha256": "a" * 64, "size_bytes": 10,
    })
    assert not progress(rows, artifacts, context=ctx)


@pytest.mark.parametrize("defect", ["allowlist", "run", "reused_artifact"])
def test_negative_unscoped_or_reused_ledger_cannot_gain_credit(defect: str) -> None:
    rows, artifacts = fixture()
    if defect == "allowlist":
        assert not progress(rows, artifacts, tools={"engineer": ("workspace.read",)})
    elif defect == "run":
        assert not progress(rows, artifacts, context=action_context().model_copy(
            update={"run_id": uuid4()}))
    else:
        append_result(rows, artifacts, "workspace.write_text", result_at(artifacts, 0))
        rows[-1]["artifact_id"] = rows[0]["artifact_id"]
        rows[-1]["artifact_sha256"] = rows[0]["artifact_sha256"]
        assert progress(rows, artifacts) == progress(rows[:3], artifacts)


@pytest.mark.parametrize("defect", ["failed", "status", "source", "diagnostic", "binding"])
def test_negative_verification_must_pass_existing_trust_checks(defect: str) -> None:
    rows, artifacts = fixture()
    passed = result_at(artifacts, 1)
    receipt = passed["verification"]
    assert isinstance(receipt, Mapping)
    ctx = action_context()
    if defect == "failed":
        rows[1]["status"] = "failed"
    elif defect == "binding":
        ctx = ctx.model_copy(update={"routing_decision": {
            **ctx.routing_decision, "project_input_contract_version": 1,
        }})
    else:
        changed: dict[str, JsonValue] = dict(receipt)
        if defect == "status":
            changed["status"] = "failed"
        elif defect == "source":
            changed["source_sha256"] = "bad"
        else:
            changed["checks"] = ({"phase": "test", "status": "passed"},)
        replace_result(rows, artifacts, 1, {**passed, "verification": changed})
    assert progress(rows, artifacts, context=ctx) == progress(rows[:1], artifacts, context=ctx)


@pytest.mark.parametrize("field,value", [
    ("verification_source_sha256", "d" * 64),
    ("verification_receipt_sha256", "d" * 64),
    ("workspace_files", []), ("workspace_files", [
        {"path": "index.js", "sha256": "f" * 64, "size_bytes": 10},
    ]), ("presentation", "intermediate"), ("artifact_id", "bad"),
])
def test_negative_bundle_requires_current_verified_delivery(
    field: str, value: JsonValue,
) -> None:
    rows, artifacts = fixture()
    replace_result(rows, artifacts, 2, {**result_at(artifacts, 2), field: value})
    assert progress(rows, artifacts) == progress(rows[:2], artifacts)


@pytest.mark.parametrize("name", ["workspace.write_text", "run_safe_command", "project.verify"])
def test_negative_later_mutation_or_failed_check_cannot_reuse_old_bundle(name: str) -> None:
    rows, artifacts = fixture()
    before = progress(rows, artifacts)
    append_result(rows, artifacts, name, result_at(artifacts, 0 if name == "workspace.write_text" else 1),
                  status="failed" if name == "project.verify" else "succeeded")
    append_result(rows, artifacts, "workspace.bundle", result_at(artifacts, 2))
    assert progress(rows, artifacts) == before
