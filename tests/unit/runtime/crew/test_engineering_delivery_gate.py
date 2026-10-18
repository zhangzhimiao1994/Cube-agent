"""Source-bound engineering delivery uses real tool result envelopes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import cast
from uuid import uuid4

import pytest

from agent_hub.capabilities.project_verification import _validate_report
from agent_hub.runtime.contracts import Artifact, JsonValue


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fixture() -> tuple[dict[str, Mapping[str, JsonValue]], dict[str, Artifact], dict[str, Artifact]]:
    states: dict[str, Mapping[str, JsonValue]] = {}
    artifacts: dict[str, Artifact] = {}
    models: dict[str, Artifact] = {}
    text = 'module.exports = 42;\n'
    metadata: dict[str, JsonValue] = {"path": "index.js", "sha256": sha(text.encode()), "size_bytes": len(text)}
    source = sha(json.dumps({"index.js": sha(b"f\0" + text.encode())},
                            sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii"))
    report = _validate_report(json.dumps({
        "schema_version": 1, "status": "passed", "source_sha256": source,
        "checks": [{"phase": phase, "status": "passed", "exit_code": 0, "timed_out": False,
                    "stdout": "", "stderr": ""} for phase in ("install", "build", "test")],
        "runtime": {"node": "v22.0.0", "npm": "10.0.0"},
    }), expected_source=source)
    receipt_digest = sha(json.dumps(report, default=dict, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True).encode("ascii"))
    for index, (name, args, result) in enumerate((
        ("workspace.write_text", {"path": "index.js", "content": text},
         {"file": metadata, "workspace_files": (metadata,)}),
        ("project.verify", {}, {"verification": report}),
        ("workspace.bundle", {}, {"artifact_id": str(uuid4()), "presentation": "final_attachment",
                                  "file": {"filename": "project.zip"}, "workspace_files": (metadata,),
                                  "verification_source_sha256": source,
                                  "verification_receipt_sha256": receipt_digest}),
    )):
        digest = sha(json.dumps(args, ensure_ascii=False, allow_nan=False, sort_keys=True,
                                separators=(",", ":")).encode())
        model = Artifact(id=uuid4(), type="model_response", producer="implementer", content={
            "tool_calls": ({"id": str(index), "name": name, "arguments": args},),
        })
        models[str(model.id)] = model
        artifact = Artifact(id=uuid4(), type="tool_result", producer="implementer", source_ids=(str(model.id),),
                            content={"agent_id": "implementer", "tool_name": name,
                                     "arguments_sha256": digest, "result": cast(JsonValue, result)})
        key = str(index)
        artifacts[key] = artifact
        states[key] = {"step_id": "build", "attempt": 0, "round": index, "tool_index": 0,
                       "name": name, "status": "succeeded", "arguments_sha256": digest,
                       "artifact_id": str(artifact.id), "sha256": artifact.content_sha256}
    return states, artifacts, models


def ready(data: tuple[dict[str, Mapping[str, JsonValue]], dict[str, Artifact], dict[str, Artifact]],
          **prefix: int) -> bool:
    from agent_hub.runtime.crew.engineering_delivery import engineering_delivery_ready

    states, artifacts, _ = data
    return engineering_delivery_ready(step_id="build", actor="implementer", states=states,
                                      artifacts=artifacts, **prefix)


def change_result(data: tuple[dict[str, Mapping[str, JsonValue]], dict[str, Artifact], dict[str, Artifact]],
                  key: str, result: Mapping[str, JsonValue]) -> None:
    states, artifacts, _ = data
    artifact = artifacts[key]
    changed = Artifact(id=artifact.id, type=artifact.type, producer=artifact.producer,
                       source_ids=artifact.source_ids, content={**artifact.content, "result": result})
    artifacts[key] = changed
    states[key] = {**states[key], "sha256": changed.content_sha256}


def test_real_write_verify_bundle_and_prefix() -> None:
    data = fixture()
    assert ready(data)
    assert not ready(data, attempt=0, round_index=2)
    assert ready(data, attempt=0, round_index=3)


@pytest.mark.parametrize("missing", ("0", "1", "2"))
def test_every_delivery_stage_is_required(missing: str) -> None:
    data = fixture()
    del data[0][missing]
    assert not ready(data)


@pytest.mark.parametrize("status", ("running", "prepared", "failed", "rejected", "received_cancelled"))
def test_unresolved_or_failed_verify_is_not_completion(status: str) -> None:
    data = fixture()
    data[0]["1"] = {**data[0]["1"], "status": status}
    assert not ready(data)


@pytest.mark.parametrize("key", ("0", "1", "2"))
def test_ledger_artifact_hash_binding(key: str) -> None:
    data = fixture()
    data[0][key] = {**data[0][key], "sha256": "a" * 64}
    assert not ready(data)


@pytest.mark.parametrize("position", (0, 2, 3))
def test_verify_must_be_strictly_after_write_and_before_bundle(position: int) -> None:
    data = fixture()
    data[0]["1"] = {**data[0]["1"], "round": position}
    assert not ready(data)


def test_wrong_source_hash_is_not_current_bundle() -> None:
    data = fixture()
    result = data[1]["1"].content["result"]
    assert isinstance(result, Mapping) and isinstance(result["verification"], Mapping)
    change_result(data, "1", {"verification": {**result["verification"], "source_sha256": "a" * 64}})
    assert not ready(data)


def test_failed_check_inside_success_wrapper_is_rejected() -> None:
    data = fixture()
    result = data[1]["1"].content["result"]
    assert isinstance(result, Mapping) and isinstance(result["verification"], Mapping)
    change_result(data, "1", {"verification": {**result["verification"], "checks": ()}})
    assert not ready(data)


def test_bundle_without_runtime_source_attestation_is_rejected() -> None:
    data = fixture()
    result = data[1]["2"].content["result"]
    assert isinstance(result, Mapping)
    change_result(data, "2", {key: value for key, value in result.items() if key != "verification_source_sha256"})
    assert not ready(data)


def test_protected_existing_files_need_not_be_rewritten() -> None:
    data = fixture()
    result = data[1]["2"].content["result"]
    assert isinstance(result, Mapping) and isinstance(result["workspace_files"], tuple)
    change_result(data, "2", {**result, "workspace_files": (*result["workspace_files"], {
        "path": "protected.test.js", "sha256": "c" * 64, "size_bytes": 31,
    })})
    assert ready(data)


def test_later_write_invalidates_delivery() -> None:
    data = fixture()
    data[0]["0"] = {**data[0]["0"], "round": 3}
    assert not ready(data)


def test_same_round_tool_index_orders_delivery() -> None:
    data = fixture()
    for key in data[0]:
        data[0][key] = {**data[0][key], "round": 0, "tool_index": int(key)}
    assert ready(data)


def test_readiness_never_mutates_or_invokes_tools() -> None:
    data = fixture()
    before = repr(data)
    assert ready(data)
    assert repr(data) == before


@pytest.mark.parametrize("name,expected", (("project.inspect", True), ("run_safe_command", False),
                                          ("workspace.prune", False)))
def test_post_verify_read_only_inspection_preserves_proof(name: str, expected: bool) -> None:
    data = fixture()
    data[0]["2"] = {**data[0]["2"], "round": 3}
    data[0]["inspection"] = {"step_id": "build", "attempt": 0, "round": 2,
                              "tool_index": 0, "name": name, "status": "succeeded"}
    assert ready(data) is expected


def test_failed_verify_then_write_pass_verify_bundle_is_delivered() -> None:
    data = fixture()
    data[0]["earlier_failure"] = {**data[0]["1"], "round": 0, "tool_index": 1,
                                 "status": "failed"}
    for key in ("0", "1", "2"):
        data[0][key] = {**data[0][key], "round": int(key) + 1}
    assert ready(data)


@pytest.mark.parametrize("status,expected", (("failed", True), ("running", False),
                                           ("uncertain", False), ("UNKNOWN", False), ("prepared", False)))
def test_only_definitely_failed_bundle_without_artifact_can_be_superseded(status: str, expected: bool) -> None:
    data = fixture()
    for key in ("0", "1", "2"):
        data[0][key] = {**data[0][key], "round": int(key) + 1}
    data[0]["early_bundle"] = {"step_id": "build", "attempt": 0, "round": 0,
                                "tool_index": 0, "name": "workspace.bundle", "status": status}
    assert ready(data) is expected
