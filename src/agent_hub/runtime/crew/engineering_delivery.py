"""Read-only, causal proof of an opted-in engineering step's real delivery."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import cast

from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext
from agent_hub.runtime.crew.workspace_maintenance_delivery import (
    _file_metadata,
    maintenance_delivery_ready,
)
from agent_hub.runtime.engineering_actions import EngineeringProgress
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
    python_success_source,
)

Position = tuple[int, int, int]


def engineering_action_progress(
    *, context: TaskContext, step_id: str, actor: str,
    states: Mapping[str, Mapping[str, JsonValue]], artifacts: Mapping[str, Artifact],
    attempt: int, round_index: int, completion_proposed: bool = False, recovering: bool = False,
) -> EngineeringProgress:
    rows: list[tuple[Position, str, Mapping[str, JsonValue]]] = []
    uncertain = EngineeringProgress(uncertain=True)
    for key, row in states.items():
        if row.get('step_id') != step_id:
            continue
        coordinates = tuple(row.get(name) for name in ('attempt', 'round', 'tool_index'))
        if any(type(value) is not int or value < 0 for value in coordinates):
            return uncertain
        position = cast(Position, coordinates)
        if position[:2] < (attempt, round_index):
            rows.append((position, key, row))
    inspected = existing = wrote = seen = verified = failed = False
    seen = failed = original_failure_observed(context)
    python = python_history_selected(context)
    known: dict[str, tuple[str, str, int]] = python_initial_files(context) if python else {}
    for _, key, row in sorted(rows):
        name, status = row.get('name'), row.get('status')
        if status in {'prepared', 'running', 'uncertain', 'UNKNOWN'}:
            return uncertain
        if name not in {'project.inspect', 'workspace.write_text', 'project.verify', 'workspace.bundle'}:
            if status == 'succeeded' and name not in {
                'read_context', 'workspace.read', 'workspace_read', 'workspace.list', 'workspace.search',
            }:
                inspected = verified = failed = False
            continue
        if (name == 'workspace.bundle' and status == 'failed'
                and row.get('engineering_bundle_correction') == 1
                and row.get('artifact_id') is None):
            continue
        result = _trusted_result(key, row, actor, artifacts)
        if result is None:
            return uncertain
        if status == 'rejected':
            continue
        if name == 'project.inspect' and status == 'succeeded':
            scope = result.get('scope')
            if (not isinstance(scope, Mapping) or scope.get('run_id') != str(context.run_id)
                    or scope.get('tenant_id') != str(context.tenant_id)):
                return uncertain
            inspected = True
            manifests, operations, issues = result.get('manifests'), result.get('operations'), result.get('issues')
            if not wrote and isinstance(manifests, tuple | list) and isinstance(operations, Mapping):
                existing = (isinstance(issues, tuple | list) and not issues
                    and operations.get('build') == 'declared' and operations.get('test') == 'declared'
                    and (any(isinstance(item, Mapping) and item.get('path') == 'package.json'
                             and item.get('status') == 'observed' for item in manifests)
                         or python_project_inspection_declared(result)))
        elif name == 'workspace.write_text' and status == 'succeeded':
            files = result.get('workspace_files')
            item = (_file_metadata(files[0]) if isinstance(files, tuple | list) and len(files) == 1
                    else _file_metadata(result))
            if item is None:
                return uncertain
            wrote = True
            verified = False
            if python_productive_change(context, item, known) if python else known.get(item[0]) != item:
                failed = False
            known[item[0]] = item
        elif name == 'project.verify':
            seen = True
            source = _successful_source(result, context=context)
            verified = (status == 'succeeded' and source is not None
                        and original_contract_binding_matches(context, result, source))
            failed = (status == 'failed' and (python_failure_current(context, result) if python
                                             else result.get('status') == 'failed'))
            if not verified and not failed:
                return uncertain
    return EngineeringProgress(inspected=inspected, existing_project=existing, has_writes=wrote,
        verification_seen=seen, verification_current=verified, verification_failed_current=failed,
        completion_proposed=completion_proposed, recovering=recovering,
        bundle_current=engineering_delivery_ready(step_id=step_id, actor=actor, states=states,
            artifacts=artifacts, attempt=attempt, round_index=round_index, require_write=False, context=context))


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


def _successful_source(result: Mapping[str, JsonValue], *, context: TaskContext | None = None) -> str | None:
    if context is not None and python_history_selected(context):
        return python_success_source(context, result)
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
    require_write: bool = True,
    context: TaskContext | None = None,
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
                                      states=delivery_states, artifacts=artifacts, require_write=require_write):
        return False
    wrote = not require_write
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
                source = _successful_source(result, context=context)
                if source is not None and not original_contract_binding_matches(context, result, source):
                    source = None
                digest = hashlib.sha256(json.dumps(
                    result.get("verification"), default=dict, sort_keys=True,
                    separators=(",", ":"), ensure_ascii=True,
                ).encode("ascii")).hexdigest()
                verified = (position, source, digest) if source is not None and wrote else None
                ready = False
            else:
                ready = (verified is not None and verified[0] < position
                         and original_contract_binding_matches(context, result, verified[1])
                         and result.get("verification_source_sha256") == verified[1]
                         and result.get("verification_receipt_sha256") == verified[2]
                         and maintenance_delivery_ready(
                             step_id=step_id, actor=actor, attempt=position[0], round_index=position[1] + 1,
                             states={k: r for p, k, r in rows if p <= position and k not in superseded_bundles},
                             artifacts=artifacts, require_write=require_write,
                         ))
        elif name not in {"read_context", "project.inspect", "workspace.read", "workspace_read",
                          "workspace.list", "workspace.search"}:
            # An intervening command/prune/unknown tool may mutate the checked source.
            verified, ready = None, False
    return ready
