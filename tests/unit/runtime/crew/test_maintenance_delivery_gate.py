"""Existing-workspace delivery needs causal receipts, not summary assertions."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

import pytest

from agent_hub.runtime.contracts import Artifact, JsonValue


def ready(
    rows: tuple[tuple[str, dict[str, JsonValue], Artifact], ...],
    *, attempt: int = 0, round_index: int = 10,
) -> bool:
    from agent_hub.runtime.crew.workspace_maintenance_delivery import maintenance_delivery_ready

    return maintenance_delivery_ready(
        step_id="repair", actor="implementer", attempt=attempt, round_index=round_index,
        states={key: row for key, row, _ in rows},
        artifacts={key: artifact for key, _, artifact in rows},
    )


def receipt(
    name: str, round_index: int, *, attempt: int = 0, tool_index: int = 0,
    status: str = "succeeded", final: bool = True,
) -> tuple[str, dict[str, JsonValue], Artifact]:
    result: Mapping[str, JsonValue] = (
        {"path": "src/business.ts", "content_sha256": "b" * 64, "content_bytes": 12}
        if name == "workspace.write_text" else {
            "artifact_id": str(uuid4()), "presentation": "final_attachment" if final else "inline",
            "file": {"filename": "repair.zip"},
            "workspace_files": ({"path": "src/business.ts", "sha256": "b" * 64, "size_bytes": 12},),
        }
    )
    artifact = Artifact(
        id=uuid4(), type="tool_result", producer="implementer", source_ids=(str(uuid4()),),
        content={"agent_id": "implementer", "tool_name": name,
                 "arguments_sha256": "a" * 64, "result": result},
    )
    row: dict[str, JsonValue] = {
        "step_id": "repair", "attempt": attempt, "round": round_index,
        "tool_index": tool_index, "name": name, "status": status,
        "arguments_sha256": "a" * 64, "artifact_id": str(artifact.id),
        "sha256": artifact.content_sha256,
    }
    return str(uuid4()), row, artifact


def test_empty_or_bundle_only_cannot_claim_modified_delivery() -> None:
    assert not ready(())
    assert not ready((receipt("workspace.bundle", 2),))


def test_write_then_fresh_final_bundle_is_delivery() -> None:
    assert ready((receipt("workspace.write_text", 1), receipt("workspace.bundle", 2)))


@pytest.mark.parametrize("order", (("workspace.bundle", "workspace.write_text"),
                                  ("workspace.write_text", "workspace.bundle", "workspace.write_text")))
def test_bundle_before_last_write_is_stale(order: tuple[str, ...]) -> None:
    assert not ready(tuple(receipt(name, index) for index, name in enumerate(order)))


@pytest.mark.parametrize("write_index,bundle_index,expected", ((0, 1, True), (1, 0, False)))
def test_same_round_uses_tool_index(write_index: int, bundle_index: int, expected: bool) -> None:
    assert ready((receipt("workspace.write_text", 1, tool_index=write_index),
                  receipt("workspace.bundle", 1, tool_index=bundle_index))) is expected


def test_future_receipts_do_not_change_cached_prefix() -> None:
    write = receipt("workspace.write_text", 1)
    bundle = receipt("workspace.bundle", 2)
    assert not ready((write, bundle), round_index=2)
    assert ready((write, bundle, receipt("workspace.write_text", 5)), round_index=4)


def test_recovery_bundle_may_follow_a_previous_attempt_write() -> None:
    assert ready((receipt("workspace.write_text", 8), receipt("workspace.bundle", 0, attempt=1)), attempt=1)


@pytest.mark.parametrize("status", ("prepared", "running", "failed", "rejected", "received_cancelled"))
def test_unresolved_or_failed_write_never_becomes_success(status: str) -> None:
    assert not ready((receipt("workspace.write_text", 1, status=status), receipt("workspace.bundle", 2)))
    assert not ready((receipt("workspace.write_text", 1), receipt("workspace.bundle", 2),
                      receipt("workspace.write_text", 3, status=status)))


@pytest.mark.parametrize("field,value", (("sha256", "c" * 64), ("artifact_id", str(uuid4())),
                                       ("arguments_sha256", "d" * 64), ("tool_index", True)))
def test_inconsistent_write_receipt_is_not_proof(field: str, value: JsonValue) -> None:
    key, row, artifact = receipt("workspace.write_text", 1)
    row[field] = value
    assert not ready(((key, row, artifact), receipt("workspace.bundle", 2)))


@pytest.mark.parametrize("patch", ({"producer": "foreign"}, {"source_ids": ()},
                                  {"type": "model_response"}))
def test_unlinked_or_foreign_artifact_is_not_write_proof(patch: dict[str, object]) -> None:
    key, row, artifact = receipt("workspace.write_text", 1)
    changed = artifact.model_copy(update=patch)
    assert not ready(((key, row, changed), receipt("workspace.bundle", 2)))


def test_bundle_must_be_final_attachment_and_have_distinct_position() -> None:
    assert not ready((receipt("workspace.write_text", 1), receipt("workspace.bundle", 2, final=False)))
    assert not ready((receipt("workspace.write_text", 1), receipt("workspace.bundle", 1)))


def test_claimed_summary_without_written_file_metadata_is_not_proof() -> None:
    key, row, artifact = receipt("workspace.write_text", 1)
    content = dict(artifact.content)
    content["result"] = {"summary": "Everything was fixed and delivered."}
    changed = Artifact(id=artifact.id, type=artifact.type, producer=artifact.producer,
                       source_ids=artifact.source_ids, content=content)
    row["sha256"] = changed.content_sha256
    assert not ready(((key, row, changed), receipt("workspace.bundle", 2)))


def test_foreign_step_cannot_supply_the_required_write() -> None:
    key, row, artifact = receipt("workspace.write_text", 1)
    row["step_id"] = "other_step"
    assert not ready(((key, row, artifact), receipt("workspace.bundle", 2)))


def test_ledger_objects_are_not_mutated() -> None:
    rows = (receipt("workspace.write_text", 1), receipt("workspace.bundle", 2))
    before = tuple((key, dict(row), artifact.model_dump()) for key, row, artifact in rows)
    assert ready(rows)
    assert before == tuple((key, dict(row), artifact.model_dump()) for key, row, artifact in rows)


def test_native_write_shape_and_manifest_hash_must_agree() -> None:
    key, row, artifact = receipt("workspace.write_text", 1)
    content = dict(artifact.content)
    public: dict[str, JsonValue] = {"path": "src/business.ts", "sha256": "b" * 64, "size_bytes": 12}
    content["result"] = {"file": public, "workspace_files": (public,)}
    native = Artifact(id=artifact.id, type=artifact.type, producer=artifact.producer,
                      source_ids=artifact.source_ids, content=content)
    row["sha256"] = native.content_sha256
    assert ready(((key, row, native), receipt("workspace.bundle", 2)))
    changed: dict[str, JsonValue] = dict(public, sha256="c" * 64)
    content["result"] = {"file": changed, "workspace_files": (changed,)}
    stale = Artifact(id=artifact.id, type=artifact.type, producer=artifact.producer,
                     source_ids=artifact.source_ids, content=content)
    row["sha256"] = stale.content_sha256
    assert not ready(((key, row, stale), receipt("workspace.bundle", 2)))
