"""Read-only, causal proof of an opted-in engineering step's real delivery."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import cast

from agent_hub.runtime.contracts import Artifact, JsonValue
from agent_hub.runtime.crew.workspace_maintenance_delivery import (
    maintenance_delivery_ready,
)

Position = tuple[int, int, int]


def _trusted_result(key: str, row: Mapping[str, JsonValue], actor: str,
                    artifacts: Mapping[str, Artifact]) -> Mapping[str, JsonValue] | None:
    artifact = artifacts.get(key)
    if (artifact is None or artifact.type != "tool_result" or artifact.producer != actor
        or len(artifact.source_ids) != 1 or artifact.content.get("agent_id") != actor
        or artifact.content.get("tool_name") != row.get("name")
        or artifact.content.get("arguments_sha256") != row.get("arguments_sha256")
        or str(artifact.id) != row.get("artifact_id") or artifact.content_sha256 != row.get("sha256")
        or artifact.content_sha256 != artifact.recompute_content_sha256()):
        return None
    value = artifact.content.get("result")
    return value if isinstance(value, Mapping) else None


def _successful_source(result: Mapping[str, JsonValue]) -> str | None:
    receipt = result.get("verification")
    if (not isinstance(receipt, Mapping) or type(receipt.get("schema_version")) is not int
        or receipt.get("schema_version") != 1 or receipt.get("status") != "passed"):
        return None
    source, checks = receipt.get("source_sha256"), receipt.get("checks")
    if (not isinstance(source, str) or re.fullmatch("[a-f0-9]{64}", source) is None
        or not isinstance(checks, tuple | list) or len(checks) != 3):
        return None
    for phase, check in zip(("install", "build", "test"), checks, strict=True):
        if not isinstance(check, Mapping) or check.get("phase") != phase or check.get("status") != "passed":
            return None
        diagnostic = check.get("execution_diagnostic")
        if (not isinstance(diagnostic, Mapping) or type(diagnostic.get("schema_version")) is not int
            or diagnostic.get("schema_version") != 1 or diagnostic.get("backend") != "systemd"
            or type(diagnostic.get("exit_code")) is not int or diagnostic.get("exit_code") != 0
            or diagnostic.get("timed_out") is not False):
            return None
    return source


def engineering_delivery_ready(
    *, step_id: str, actor: str, states: Mapping[str, Mapping[str, JsonValue]],
    artifacts: Mapping[str, Artifact],
    attempt: int | None = None, round_index: int | None = None,
) -> bool:
    """Require trusted write -> successful verify -> matching current final bundle.

    No filesystem/model/tool calls are performed, including during completed replay.
    The bundle's attestation is issued only after the capability checks ZIP bytes.
    """
    if ((attempt is None) != (round_index is None)
        or (attempt is not None and (type(attempt) is not int or attempt < 0
                                    or type(round_index) is not int or round_index < 0))):
        return False
    rows: list[tuple[Position, str, Mapping[str, JsonValue]]] = []
    for key, row in states.items():
        if row.get("step_id") != step_id:
            continue
        coordinates = tuple(row.get(field) for field in ("attempt", "round", "tool_index"))
        if any(type(value) is not int or value < 0 for value in coordinates):
            return False
        position = cast(Position, coordinates)
        if attempt is not None and position[:2] >= (attempt, cast(int, round_index)):
            continue
        rows.append((position, key, row))
    if not rows or len({position for position, _, _ in rows}) != len(rows):
        return False
    # A confirmed failure with no output did not create a deliverable. It may be
    # superseded; prepared/running/uncertain records must never be discarded.
    superseded_bundles = {
        key for _, key, row in rows
        if row.get("name") == "workspace.bundle" and row.get("status") == "failed"
        and row.get("artifact_id") is None and row.get("sha256") is None and key not in artifacts
    }
    delivery_states = {key: row for key, row in states.items() if key not in superseded_bundles}
    end_attempt = max(position[0] for position, _, _ in rows) + 1 if attempt is None else attempt
    if not maintenance_delivery_ready(step_id=step_id, actor=actor, attempt=end_attempt,
                                      round_index=0 if round_index is None else round_index,
                                      states=delivery_states, artifacts=artifacts):
        return False
    wrote = False
    verified: tuple[Position, str, str] | None = None
    ready = False
    for position, key, row in sorted(rows):
        if key in superseded_bundles:
            continue
        name = row.get("name")
        if name in {"workspace.write_text", "project.verify", "workspace.bundle"}:
            result = _trusted_result(key, row, actor, artifacts)
            if row.get("status") != "succeeded" or result is None:
                if name == "project.verify" and row.get("status") in {"failed", "rejected"}:
                    verified, ready = None, False
                    continue
                return False
            if name == "workspace.write_text":
                wrote = True
                verified, ready = None, False
            elif name == "project.verify":
                source = _successful_source(result)
                digest = hashlib.sha256(json.dumps(
                    result.get("verification"), default=dict, sort_keys=True,
                    separators=(",", ":"), ensure_ascii=True,
                ).encode("ascii")).hexdigest()
                verified = (position, source, digest) if source is not None and wrote else None
                ready = False
            else:
                ready = (verified is not None and verified[0] < position
                         and result.get("verification_source_sha256") == verified[1]
                         and result.get("verification_receipt_sha256") == verified[2]
                         and maintenance_delivery_ready(
                             step_id=step_id, actor=actor, attempt=position[0], round_index=position[1] + 1,
                             states={k: r for p, k, r in rows if p <= position and k not in superseded_bundles},
                             artifacts=artifacts,
                         ))
        elif name not in {"read_context", "project.inspect", "workspace.read", "workspace_read",
                          "workspace.list", "workspace.search"}:
            # An intervening command/prune/unknown tool may mutate the checked source.
            verified, ready = None, False
    return ready
