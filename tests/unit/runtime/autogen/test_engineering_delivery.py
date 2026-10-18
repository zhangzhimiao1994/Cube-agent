"""AutoGen engineering proof uses its own causal ledger, never synthetic Crew rows."""

import hashlib
import json
from collections.abc import Mapping
from typing import cast
from uuid import UUID, uuid4

import pytest

from agent_hub.runtime.contracts import Artifact, JsonValue

RUN = UUID("00000000-0000-4000-8000-000000000001")
TOOLS = {"engineer": ("workspace.write_text", "project.verify", "workspace.bundle",
                      "workspace.read", "run_safe_command")}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, default=dict, sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def fixture() -> tuple[list[dict[str, JsonValue]], list[Artifact]]:
    metadata = {"path": "index.js", "sha256": "a" * 64, "size_bytes": 10}
    receipt = {"schema_version": 1, "status": "passed", "source_sha256": "b" * 64,
               "checks": [{"phase": phase, "status": "passed", "execution_diagnostic": {
                   "schema_version": 1, "backend": "systemd", "exit_code": 0, "timed_out": False,
               }} for phase in ("install", "build", "test")]}
    outcomes: list[tuple[str, object]] = [
        ("workspace.write_text", {"workspace_files": [metadata]}),
        ("project.verify", {"verification": receipt}),
        ("workspace.bundle", {"artifact_id": str(uuid4()), "presentation": "final_attachment",
                              "file": {"filename": "project.zip"}, "workspace_files": [metadata],
                              "verification_source_sha256": "b" * 64,
                              "verification_receipt_sha256": digest(receipt)}),
    ]
    rows: list[dict[str, JsonValue]] = []
    artifacts: list[Artifact] = []
    for position, (name, result) in enumerate(outcomes):
        artifact = Artifact(id=uuid4(), type="tool_result", producer="engineer", content={
            "tool_name": name, "arguments_sha256": "c" * 64, "result": cast(JsonValue, result),
        })
        artifacts.append(artifact)
        rows.append({"position": position, "actor": "engineer", "name": name,
                     "arguments_hash": "c" * 64, "status": "succeeded", "replay_safe": False,
                     "idempotency_key": hashlib.sha256(
                         f"{RUN}:engineer:{position}:{name}:{'c' * 64}".encode()).hexdigest(),
                     "artifact_id": str(artifact.id), "artifact_sha256": artifact.content_sha256})
    return rows, artifacts


def ready(rows: list[dict[str, JsonValue]], artifacts: list[Artifact]) -> bool:
    from agent_hub.runtime.autogen.engineering_delivery import engineering_delivery_ready

    return engineering_delivery_ready(run_id=RUN, tool_entries=rows, artifacts=artifacts,
                                      allowed_tools=TOOLS)


def replace_result(rows: list[dict[str, JsonValue]], artifacts: list[Artifact],
                   position: int, result: Mapping[str, JsonValue]) -> None:
    old = artifacts[position]
    artifacts[position] = Artifact(id=old.id, type=old.type, producer=old.producer,
                                   content={**old.content, "result": result})
    rows[position]["artifact_sha256"] = artifacts[position].content_sha256


def test_real_autogen_proof_without_source_ids_is_ready_and_read_only() -> None:
    rows, artifacts = fixture()
    before = repr((rows, artifacts))
    assert all(not artifact.source_ids for artifact in artifacts)
    assert ready(rows, artifacts)
    assert repr((rows, artifacts)) == before


@pytest.mark.parametrize("missing", (0, 1, 2))
def test_missing_stage_or_orphaned_historical_artifacts_cannot_deliver(missing: int) -> None:
    rows, artifacts = fixture()
    rows.pop(missing)
    assert not ready(rows, artifacts)
    assert not ready([], artifacts)


@pytest.mark.parametrize("field,value", [
    ("status", "running"), ("status", "UNKNOWN"), ("status", "failed"),
    ("status", {"unexpected": "mapping"}),
    ("artifact_sha256", "d" * 64), ("actor", "outsider"), ("position", True),
    ("arguments_hash", "d" * 64), ("idempotency_key", "d" * 64),
])
def test_invalid_or_uncertain_ledger_is_not_repaired_or_replayed(field: str, value: JsonValue) -> None:
    rows, artifacts = fixture()
    rows[1][field] = value
    assert not ready(rows, artifacts)


@pytest.mark.parametrize("field,value", [
    ("verification_source_sha256", "d" * 64), ("verification_receipt_sha256", "d" * 64),
    ("workspace_files", []), ("presentation", "intermediate"), ("artifact_id", "bad"),
])
def test_bundle_attestation_and_written_manifest_must_match(field: str, value: JsonValue) -> None:
    rows, artifacts = fixture()
    result = artifacts[2].content["result"]
    assert isinstance(result, Mapping)
    replace_result(rows, artifacts, 2, {**result, field: value})
    assert not ready(rows, artifacts)


def test_extra_protected_files_are_allowed_but_duplicate_manifest_is_not() -> None:
    rows, artifacts = fixture()
    result = artifacts[2].content["result"]
    assert isinstance(result, Mapping) and isinstance(result["workspace_files"], tuple)
    protected: JsonValue = {"path": "test.js", "sha256": "e" * 64, "size_bytes": 10}
    manifest = (*result["workspace_files"], protected)
    replace_result(rows, artifacts, 2, {**result, "workspace_files": manifest})
    assert ready(rows, artifacts)
    replace_result(rows, artifacts, 2, {**result, "workspace_files": (*manifest, manifest[0])})
    assert not ready(rows, artifacts)


@pytest.mark.parametrize("name", ("workspace.write_text", "run_safe_command", "project.verify"))
def test_later_mutation_or_check_invalidates_old_bundle(name: str) -> None:
    rows, artifacts = fixture()
    old = rows[0 if name != "project.verify" else 1]
    rows.append({**old, "position": 3, "name": name, "idempotency_key": hashlib.sha256(
        f"{RUN}:engineer:3:{name}:{'c' * 64}".encode()).hexdigest()})
    assert not ready(rows, artifacts)


def append_result(rows: list[dict[str, JsonValue]], artifacts: list[Artifact],
                  name: str, result: Mapping[str, JsonValue], *, status: str = "succeeded") -> None:
    position = len(rows)
    artifact = Artifact(id=uuid4(), type="tool_result", producer="engineer", content={
        "tool_name": name, "arguments_sha256": "c" * 64, "result": result,
    })
    artifacts.append(artifact)
    rows.append({"position": position, "actor": "engineer", "name": name,
                 "arguments_hash": "c" * 64, "status": status,
                 "idempotency_key": hashlib.sha256(
                     f"{RUN}:engineer:{position}:{name}:{'c' * 64}".encode()).hexdigest(),
                 "artifact_id": str(artifact.id), "artifact_sha256": artifact.content_sha256})


def test_real_later_write_requires_fresh_verification_and_current_manifest() -> None:
    rows, artifacts = fixture()
    verification = artifacts[1].content["result"]
    bundle = artifacts[2].content["result"]
    assert isinstance(verification, Mapping) and isinstance(bundle, Mapping)
    changed: JsonValue = {"path": "index.js", "sha256": "e" * 64, "size_bytes": 12}
    append_result(rows, artifacts, "workspace.write_text", {"workspace_files": (changed,)})
    assert not ready(rows, artifacts)
    append_result(rows, artifacts, "workspace.bundle", bundle)
    assert not ready(rows, artifacts)
    append_result(rows, artifacts, "project.verify", verification)
    append_result(rows, artifacts, "workspace.bundle", bundle)
    assert not ready(rows, artifacts)
    append_result(rows, artifacts, "workspace.bundle", {**bundle, "workspace_files": (changed,)})
    assert ready(rows, artifacts)


def test_read_only_result_after_bundle_preserves_proof() -> None:
    rows, artifacts = fixture()
    append_result(rows, artifacts, "workspace.read", {"text": "unchanged"})
    assert ready(rows, artifacts)


@pytest.mark.parametrize("version,tools,expected", [
    (0, TOOLS, False), (True, TOOLS, False), (1, TOOLS, True),
    (1, {"planner": ("workspace.read",)}, False),
    (1, {"writer": ("workspace.write_text",)}, True),
])
def test_only_exact_opt_in_engineering_writer_requires_delivery(
    version: JsonValue, tools: Mapping[str, tuple[str, ...]], expected: bool,
) -> None:
    from agent_hub.domain.runs import TaskMode
    from agent_hub.runtime.autogen.engineering_delivery import engineering_delivery_required
    from agent_hub.runtime.contracts import TaskContext

    ctx = TaskContext(run_id=RUN, tenant_id=uuid4(), mode=TaskMode.DISCUSS, request="plan",
                      routing_decision={"engineering_delivery_contract_version": version})
    assert engineering_delivery_required(ctx, tools) is expected
