"""Pure delivery proof for AutoGen's native write-ahead ledger and typed artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.autogen.model_tool_correction import (
    TOOL_CORRECTION_POLICY,
    validate_rejected_model_rows,
)
from agent_hub.runtime.contracts import (
    _MAX_JSON_BYTES,
    _MAX_JSON_NODES,
    Artifact,
    JsonValue,
    TaskContext,
    _mutable_json,
    _preflight_checkpoint_payload,
)
from agent_hub.runtime.crew.engineering_delivery import _successful_source
from agent_hub.runtime.crew.workspace_maintenance_delivery import _file_metadata
from agent_hub.runtime.engineering_actions import EngineeringProgress
from agent_hub.runtime.generated_draft_feedback import generated_draft_artifact_feedback
from agent_hub.runtime.generated_project_history import (
    generated_delivery_matches,
    generated_failure_current,
    generated_history_selected,
)
from agent_hub.runtime.original_project_progress import (
    original_contract_binding_matches,
    original_failure_observed,
    python_project_inspection_declared,
)
from agent_hub.runtime.python_project_history import (
    python_failure_current,
    python_history_selected,
    python_initial_files,
    python_productive_change,
)

_READ_ONLY = frozenset({"read_context", "project.inspect", "workspace.read", "workspace_read",
                        "workspace.list", "workspace.search"})


@dataclass(frozen=True, slots=True)
class EngineeringEvidenceLimits:
    initial_soft: int
    soft_limit: int
    hard_limit: int


def _json_footprint(value: object) -> tuple[int, int]:
    def nodes(item: object) -> int:
        if isinstance(item, Mapping):
            return 1 + sum(1 + nodes(child) for child in item.values())
        if isinstance(item, list | tuple):
            return 1 + sum(nodes(child) for child in item)
        return 1

    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return nodes(value), len(encoded)


def engineering_evidence_limits(
    payload: Mapping[str, object], *, token_budget: int, used_tokens: int,
    timeout_seconds: float, remaining_seconds: float, max_turns: int, tool_fanout: int,
) -> EngineeringEvidenceLimits:
    """Derive a soft evidence window without raising any execution or JSON budget.

    Completed, hash-bound outcomes and validated rejection receipts are retention
    progress, not extra authority. Pending outcomes require remaining execution
    budget. Every extension is capped by the measured JSON
    metadata-node and complete-envelope-byte headroom, then checked by contracts.
    """
    if (any(type(value) is not int or value < 0 for value in (token_budget, used_tokens))
        or type(max_turns) is not int or max_turns < 1
        or type(tool_fanout) is not int or tool_fanout < 1
        or any(isinstance(value, bool) or not isinstance(value, int | float)
               or not math.isfinite(value) or value < 0
               for value in (timeout_seconds, remaining_seconds))):
        raise ValueError("engineering evidence budget is invalid")
    plain = _mutable_json(cast(JsonValue, payload))
    # Use exactly the contract's unchanged structural/byte checks, including the
    # metadata 4096-node fuse rather than the cumulative Crew ledger allowance.
    _preflight_checkpoint_payload(plain)
    assert isinstance(plain, dict) and isinstance(plain["state"], dict)
    state = plain["state"]
    rows = (*state["model_ledger"], *state["tool_ledger"])
    registry = state["artifact_registry"]
    if not isinstance(registry, dict) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("engineering evidence is invalid")
    completed = sum(
        row.get("status") in ("succeeded", "failed")
        and isinstance(row.get("artifact_id"), str)
        and isinstance(row.get("artifact_sha256"), str)
        and registry.get(row["artifact_id"]) == row["artifact_sha256"]
        for row in rows
    )
    repair_version = state.get(TOOL_CORRECTION_POLICY)
    if TOOL_CORRECTION_POLICY in state and (type(repair_version) is not int or repair_version != 1):
        raise ValueError("engineering tool correction policy is invalid")
    records = validate_rejected_model_rows(state["model_ledger"], run_id=UUID(plain["run_id"]),
        tenant_id=UUID(plain["tenant_id"]), repair_enabled=repair_version == 1)
    # This admits only stopped receipts, never another pending call.
    completed += len(records)
    state_nodes, _ = _json_footprint(state)
    _, envelope_bytes = _json_footprint(plain)
    # A future row and its content-addressed reference use the actual observed
    # record shape. With no observations, use the native prepared model shape.
    samples = rows or ({"status": "running", "request_hash": "0" * 64},)
    footprints = [_json_footprint(row) for row in samples]
    reference_nodes, reference_bytes = _json_footprint({str(UUID(int=0)): "0" * 64})
    unit_nodes = max(item[0] for item in footprints) + reference_nodes - 1
    unit_bytes = max(item[1] for item in footprints) + reference_bytes
    extra = min(max(0, _MAX_JSON_NODES - state_nodes) // unit_nodes,
                max(0, _MAX_JSON_BYTES - envelope_bytes) // unit_bytes)
    hard = len(rows) + extra
    # The same minimum two-token model turn and one-second pacing used by the
    # discussion controller; a response can expose at most tool_fanout calls.
    def turn_capacity(tokens: int, seconds: float) -> int:
        if tokens <= 0 or seconds <= 0:
            return 0
        return min(max_turns, max(1, tokens // 2), max(1, int(seconds)))

    initial_turns = turn_capacity(token_budget, timeout_seconds)
    available_turns = turn_capacity(max(0, token_budget - used_tokens),
                                    min(timeout_seconds, remaining_seconds))
    initial = min(hard, initial_turns * (tool_fanout + 1))
    extension = available_turns * (tool_fanout + 1)
    soft = min(hard, max(initial, completed + extension) if extension else completed)
    if len(rows) > soft:
        raise ValueError("engineering evidence budget is exhausted")
    return EngineeringEvidenceLimits(initial_soft=initial, soft_limit=soft, hard_limit=hard)


def engineering_delivery_required(
    context: TaskContext, allowed_tools: Mapping[str, Sequence[str]],
) -> bool:
    """Planning-only Hybrid discussion has no engineering writer and is unchanged."""
    version = context.routing_decision.get("engineering_delivery_contract_version")
    return (context.mode is TaskMode.DISCUSS and type(version) is int and version == 1
            and any("workspace.write_text" in tools for tools in allowed_tools.values()))


def engineering_delivery_ready(
    *, run_id: UUID, tool_entries: Sequence[Mapping[str, JsonValue]],
    artifacts: Sequence[Artifact], allowed_tools: Mapping[str, Sequence[str]], require_write: bool = True,
    context: TaskContext | None = None,
) -> bool:
    """Inspect only persisted outcomes; no model, filesystem, tool or replay effects.

    AutoGen has no Crew source ids. Trust comes from its native ledger binding,
    artifact hashes, actor/tool authorization, and capability-issued attestation.
    """
    by_id = {str(artifact.id): artifact for artifact in artifacts}
    if len(by_id) != len(artifacts) or not tool_entries:
        return False
    writes: dict[str, tuple[str, str, int]] = {}
    verified: tuple[str, str] | None = None
    verified_result: Mapping[str, JsonValue] = {}
    ready = False
    used: set[str] = set()
    for position, row in enumerate(tool_entries):
        actor, name, args = row.get("actor"), row.get("name"), row.get("arguments_hash")
        status = row.get("status")
        if (type(row.get("position")) is not int or row.get("position") != position
            or not isinstance(actor, str) or not isinstance(name, str)
            or name not in allowed_tools.get(actor, ()) or not isinstance(args, str)
            or re.fullmatch("[a-f0-9]{64}", args) is None
            or row.get("idempotency_key") != hashlib.sha256(
                f"{run_id}:{actor}:{position}:{name}:{args}".encode()).hexdigest()
            or not isinstance(status, str) or status not in {"succeeded", "failed"}):
            return False
        artifact_id = row.get("artifact_id")
        artifact = by_id.get(artifact_id) if isinstance(artifact_id, str) else None
        if (artifact is None or artifact_id in used or artifact.type != "tool_result"
            or artifact.producer != actor or artifact.content.get("tool_name") != name
            or artifact.content.get("arguments_sha256") != args
            or artifact.content_sha256 != row.get("artifact_sha256")
            or artifact.content_sha256 != artifact.recompute_content_sha256()):
            return False
        assert isinstance(artifact_id, str)
        used.add(artifact_id)
        result = artifact.content.get("result")
        if not isinstance(result, Mapping):
            return False
        if status == "failed":
            # An observed failed check never becomes proof, but a later real
            # write/check/bundle can supersede it. Uncertain rows never get here.
            if name != "project.verify":
                return False
            verified, ready = None, False
            continue
        if name == "workspace.write_text":
            metadata = result.get("workspace_files")
            if isinstance(metadata, tuple | list):
                if len(metadata) != 1:
                    return False
                item = _file_metadata(metadata[0])
            else:
                item = _file_metadata(result)
            if item is None:
                return False
            writes[item[0]] = item
            verified, ready = None, False
        elif name == "project.verify":
            source = _successful_source(result, context=context)
            if source is not None and not original_contract_binding_matches(context, result, source):
                source = None
            receipt_sha = hashlib.sha256(json.dumps(
                result.get("verification"), default=dict, sort_keys=True,
                separators=(",", ":"), ensure_ascii=True,
            ).encode("ascii")).hexdigest()
            verified = (source, receipt_sha) if source is not None and (writes or not require_write) else None
            verified_result = result
            ready = False
        elif name == "workspace.bundle":
            ready = False
            if (verified is None or result.get("verification_source_sha256") != verified[0]
                or generated_history_selected(context) and (
                    context is None or not generated_delivery_matches(context, verified_result, result))
                or not original_contract_binding_matches(context, result, verified[0])
                or result.get("verification_receipt_sha256") != verified[1]
                or result.get("presentation") != "final_attachment"):
                continue
            delivered_id, file = result.get("artifact_id"), result.get("file")
            try:
                if not isinstance(delivered_id, str) or str(UUID(delivered_id)) != delivered_id:
                    continue
            except ValueError:
                continue
            if not isinstance(file, Mapping) or not isinstance(file.get("filename"), str):
                continue
            manifest = result.get("workspace_files")
            if not isinstance(manifest, tuple | list):
                continue
            actual: dict[str, tuple[str, str, int]] = {}
            for value in manifest:
                item = _file_metadata(value)
                if item is None or item[0] in actual:
                    break
                actual[item[0]] = item
            else:
                ready = (bool(writes) or not require_write) and bool(actual) and all(
                    actual.get(path) == item for path, item in writes.items()
                )
        elif name not in _READ_ONLY:
            verified, ready = None, False
    return ready


def engineering_progress(
    context: TaskContext, *, tool_entries: Sequence[Mapping[str, JsonValue]],
    artifacts: Sequence[Artifact], allowed_tools: Mapping[str, Sequence[str]],
    completion_proposed: bool = False, recovering: bool = False,
) -> EngineeringProgress:
    """Interpret scoped native outcomes only. A model's text is never evidence."""
    by_id = {str(item.id): item for item in artifacts}
    inspected = existing = has_writes = seen = current = failed_current = False
    seen = failed_current = original_failure_observed(context)
    python = python_history_selected(context)
    known: dict[str, tuple[str, str, int]] = python_initial_files(context) if python else {}
    used: set[str] = set()
    uncertain = len(by_id) != len(artifacts)
    for position, row in enumerate(tool_entries):
        actor, name, args = row.get("actor"), row.get("name"), row.get("arguments_hash")
        artifact_id, status = row.get("artifact_id"), row.get("status")
        artifact = by_id.get(artifact_id) if isinstance(artifact_id, str) else None
        if (type(row.get("position")) is not int or row.get("position") != position
            or not isinstance(actor, str) or not isinstance(name, str)
            or name not in allowed_tools.get(actor, ()) or not isinstance(args, str)
            or re.fullmatch("[a-f0-9]{64}", args) is None
            or row.get("idempotency_key") != hashlib.sha256(
                f"{context.run_id}:{actor}:{position}:{name}:{args}".encode()).hexdigest()
            or status not in ("succeeded", "failed") or artifact is None or artifact_id in used
            or artifact.type != "tool_result" or artifact.producer != actor
            or artifact.content.get("tool_name") != name or artifact.content.get("arguments_sha256") != args
            or artifact.content_sha256 != row.get("artifact_sha256")
            or artifact.content_sha256 != artifact.recompute_content_sha256()):
            uncertain = True
            break
        assert isinstance(artifact_id, str)
        used.add(artifact_id)
        result = artifact.content.get("result")
        if not isinstance(result, Mapping):
            uncertain = True
            break
        if status == "failed":
            if name != "project.verify":
                uncertain = True
                break
            if "generated_draft_rejection" in result:
                if generated_draft_artifact_feedback(artifact, context=context, actor=actor) != result:
                    uncertain = True
                    break
            elif (generated_history_selected(context) and not generated_failure_current(context, result)
                    or python and not python_failure_current(context, result)):
                uncertain = True
                break
            seen, current, failed_current = True, False, True
            continue
        if name == "project.inspect":
            scope = result.get("scope")
            if (type(result.get("schema_version")) is not int or result.get("schema_version") != 1
                or not isinstance(scope, Mapping)
                or scope.get("tenant_id") != str(context.tenant_id)
                or scope.get("run_id") != str(context.run_id)
                or any(not isinstance(context.routing_decision.get(key), str)
                       or scope.get(key) != context.routing_decision.get(key)
                       for key in ("project_id", "workspace_session_id"))):
                uncertain = True
                break
            inspected = True
            existing = False
            observed_package = False
            manifests, operations, issues = result.get("manifests"), result.get("operations"), result.get("issues")
            if isinstance(manifests, tuple | list):
                for manifest in manifests:
                    if isinstance(manifest, Mapping) and manifest.get("status") == "observed":
                        metadata = _file_metadata({"path": manifest.get("path"), "sha256": manifest.get("sha256"),
                                                   "size_bytes": manifest.get("observed_bytes")})
                        if metadata is not None:
                            known.setdefault(metadata[0], metadata)
                            observed_package = observed_package or metadata[0] == "package.json"
                existing = (isinstance(operations, Mapping)
                            and operations.get("build") == operations.get("test") == "declared"
                            and isinstance(issues, tuple | list) and not issues
                            and (observed_package or python_project_inspection_declared(result)))
        elif name == "workspace.write_text":
            files = result.get("workspace_files")
            item = _file_metadata(files[0]) if isinstance(files, tuple | list) and len(files) == 1 else _file_metadata(result)
            if item is None:
                uncertain = True
                break
            has_writes = True
            current = False
            if python_productive_change(context, item, known) if python else known.get(item[0]) != item:
                failed_current = False
            known[item[0]] = item
        elif name == "project.verify":
            seen = True
            source = _successful_source(result, context=context)
            current = source is not None and original_contract_binding_matches(context, result, source)
            failed_current = not current and not python and not generated_history_selected(context)
            if (python or generated_history_selected(context)) and not current:
                uncertain = True
                break
        elif name not in _READ_ONLY | {"workspace.bundle"}:
            current = False
    bundle = not uncertain and engineering_delivery_ready(
        run_id=context.run_id, tool_entries=tool_entries, artifacts=artifacts,
        allowed_tools=allowed_tools, require_write=not existing, context=context,
    )
    return EngineeringProgress(
        inspected=inspected, existing_project=existing, has_writes=has_writes,
        verification_seen=seen, verification_current=current,
        verification_failed_current=failed_current, bundle_current=bundle, uncertain=uncertain,
        completion_proposed=completion_proposed, recovering=recovering,
    )
