"""Pure, bounded historical Python evidence, never installation or ZIP authority.

Only the trusted preparer creates the context projection. Tool callers must first
check artifact ownership/content hashes and causal ordering. Claimed source hashes
are labels cross-checked with a separate bundle attestation, not re-measurements.
No filesystem, model, process, cache availability or authorization calls occur here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from agent_hub.capabilities.project_contracts import ManagedProjectInput, _editable, _path
from agent_hub.capabilities.project_verification import _validate_original_diagnostic
from agent_hub.capabilities.python_project_inputs import PythonInterpreterIdentity
from agent_hub.capabilities.python_project_receipts import validate_python_passed_receipt
from agent_hub.capabilities.python_project_reports import (
    _failure_shape,
    _hash,
    _object,
)
from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext, _freeze_object
from agent_hub.skills.sandbox.python_project_protocol import PythonEnvironmentReference

if TYPE_CHECKING:
    from agent_hub.capabilities.python_project_service import PythonOriginalContext

HISTORY_KEY = "python_project_history"
_FIELDS = {
    "schema_version",
    "scope",
    "reference",
    "recipe",
    "runtime",
    "protected_sha256",
    "original_execution_id",
    "observation_sha256",
    "environment_references",
    "artifact_id",
    "artifact_sha256",
    "editable_paths",
    "initial_files",
    "editable_files",
}
_INVALID = (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError)


def _need(value: object) -> None:
    if not value:
        raise ValueError("python_project_history_invalid")


def _encoded(value: object) -> bytes:
    return json.dumps(
        value,
        default=dict,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _files(artifact: Artifact) -> tuple[Mapping[str, JsonValue], ...]:
    source = _object(
        artifact.content.get("python_source_index"),
        {"schema_version", "source_sha256", "editable_files", "files"},
    )
    _need(
        type(source["schema_version"]) is int
        and source["schema_version"] == 1
        and source["source_sha256"] == artifact.content.get("original_source_sha256")
    )
    rows = source["files"]
    scopes = source["editable_files"]
    _need(isinstance(scopes, tuple | list) and len(scopes) <= 128)
    assert isinstance(scopes, tuple | list)
    _need(
        len(scopes) == len(set(scopes))
        and all(
            _path(scope) in cast(tuple[str, ...], artifact.content["editable_paths"])
            for scope in scopes
        )
    )
    _need(isinstance(rows, tuple | list) and len(rows) <= 256)
    assert isinstance(rows, tuple | list)
    result: list[Mapping[str, JsonValue]] = []
    seen: set[str] = set()
    for row in rows:
        entry = _object(row, {"path", "sha256", "size_bytes", "source_file_sha256"})
        path = _path(entry["path"])
        _need(
            path.endswith(".py")
            and path not in seen
            and type(entry["size_bytes"]) is int
            and entry["size_bytes"] >= 0
        )
        _hash(entry["sha256"])
        _hash(entry["source_file_sha256"])
        seen.add(path)
        result.append({key: entry[key] for key in ("path", "sha256", "size_bytes")})
    return tuple(result)


def build_python_history(
    original: PythonOriginalContext, artifact: Artifact
) -> Mapping[str, JsonValue]:
    """Called only by the trusted preparer after reading the service's private history."""
    inputs, invocation = original.inputs, original.invocation
    _need(
        artifact.type == "original_project_observation"
        and artifact.producer == "original_project_preparer"
        and artifact.content_sha256 == artifact.recompute_content_sha256()
        and artifact.content.get("observation") == original.observation
        and artifact.content.get("protected_sha256") == inputs.contract.protected_sha256
        and artifact.content.get("original_source_sha256")
        == original.stored.reference.input_source_sha256
        and inputs.original_reference == original.stored.reference
    )
    if len(original.environment_references) > 128:
        raise ValueError("python_project_history_budget_exceeded")
    source_index = cast(Mapping[str, JsonValue], artifact.content.get("python_source_index"))
    initial = _files(artifact)
    indexed = cast(tuple[Mapping[str, JsonValue], ...], source_index["files"])
    _need(
        {row["path"] for row in initial}
        == {path for path in original.stored.source_files if path.endswith(".py")}
        and all(
            original.stored.source_files[cast(str, row["path"])] == row["source_file_sha256"]
            for row in indexed
        )
        and source_index["editable_files"]
        == tuple(
            scope
            for scope in inputs.contract.editable_paths
            if scope in original.stored.source_files
        )
    )
    metadata = _freeze_object(
        {
            "schema_version": 1,
            "scope": {
                "tenant_id": str(invocation.tenant_id),
                "run_id": str(invocation.run_id),
                "project_id": invocation.project_id,
                "workspace_session_id": invocation.session_id,
            },
            "reference": inputs.original_reference.to_payload(),
            "recipe": {
                "schema_version": 1,
                "recipe_sha256": inputs.sha256,
                "environment_input_sha256": inputs.environment_input_sha256,
            },
            "runtime": inputs.interpreter.to_payload(),
            "protected_sha256": inputs.contract.protected_sha256,
            "original_execution_id": invocation.execution_id,
            "observation_sha256": hashlib.sha256(_encoded(original.observation)).hexdigest(),
            "environment_references": tuple(
                {
                    "environment_sha256": ref.environment_sha256,
                    "origin_execution_id": ref.origin_execution_id,
                }
                for ref in original.environment_references
            ),
            "artifact_id": str(artifact.id),
            "artifact_sha256": artifact.content_sha256,
            "editable_paths": inputs.contract.editable_paths,
            "initial_files": initial,
            "editable_files": source_index["editable_files"],
        },
        name="Python historical context",
    )
    for ref in original.environment_references:
        _validated_reference(ref.model_dump(mode="json"), metadata)
    if len(_encoded(metadata)) > 65536:
        raise ValueError("python_project_history_budget_exceeded")
    return metadata


def python_history_selected(context: TaskContext | None) -> bool:
    if context is None:
        return False
    route, state = (
        context.routing_decision,
        context.routing_decision.get("project_input_preparation"),
    )
    return (
        type(route.get("project_input_contract_version")) is int
        and route.get("project_input_contract_version") == 1
        and isinstance(state, Mapping)
        and type(state.get("schema_version")) is int
        and state.get("schema_version") == 2
    )


def _history(context: TaskContext) -> Mapping[str, JsonValue]:
    _need(python_history_selected(context))
    route = context.routing_decision
    metadata = _object(route.get(HISTORY_KEY), _FIELDS)
    state = route.get("project_input_preparation")
    _need(isinstance(state, Mapping))
    assert isinstance(state, Mapping)
    _need(
        type(metadata["schema_version"]) is int
        and metadata["schema_version"] == 1
        and state.get("phase") == "observed"
        and metadata["reference"] == state.get("reference")
        and metadata["recipe"] == state.get("python_recipe")
        and metadata["original_execution_id"] == state.get("execution_id")
        and metadata["observation_sha256"] == state.get("observation_sha256")
        and metadata["observation_sha256"]
        == hashlib.sha256(_encoded(state.get("observation"))).hexdigest()
        and metadata["scope"]
        == {
            "tenant_id": str(context.tenant_id),
            "run_id": str(context.run_id),
            "project_id": route.get("project_id"),
            "workspace_session_id": route.get("workspace_session_id"),
        }
    )
    marker = _object(
        metadata["recipe"], {"schema_version", "recipe_sha256", "environment_input_sha256"}
    )
    _need(type(marker["schema_version"]) is int and marker["schema_version"] == 1)
    _hash(marker["recipe_sha256"])
    _hash(marker["environment_input_sha256"])
    _hash(metadata["protected_sha256"])
    PythonInterpreterIdentity.from_payload(metadata["runtime"])
    selected = ManagedProjectInput.from_payload(route.get("project_input"))
    _need(metadata["editable_paths"] == selected.editable_paths)
    matches = [item for item in context.artifacts if str(item.id) == metadata["artifact_id"]]
    _need(len(matches) == 1)
    artifact = matches[0]
    _need(
        artifact.type == "original_project_observation"
        and artifact.producer == "original_project_preparer"
        and artifact.content_sha256
        == metadata["artifact_sha256"]
        == artifact.recompute_content_sha256()
        and artifact.content.get("observation") == state.get("observation")
        and artifact.content.get("protected_sha256") == metadata["protected_sha256"]
        and artifact.content.get("editable_paths") == metadata["editable_paths"]
        and artifact.content.get("original_source_sha256")
        == cast(Mapping[str, JsonValue], metadata["reference"])["input_source_sha256"]
        and metadata["initial_files"] == _files(artifact)
        and metadata["editable_files"]
        == cast(Mapping[str, JsonValue], artifact.content["python_source_index"])["editable_files"]
    )
    refs = metadata["environment_references"]
    _need(isinstance(refs, tuple | list) and len(refs) <= 128 and len(_encoded(metadata)) <= 65536)
    assert isinstance(refs, tuple | list)
    for raw in refs:
        item = _object(raw, {"environment_sha256", "origin_execution_id"})
        _hash(item["environment_sha256"])
        origin = item["origin_execution_id"]
        _need(
            isinstance(origin, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", origin) is not None
        )
    return metadata


def _validated_reference(
    value: object, metadata: Mapping[str, JsonValue]
) -> PythonEnvironmentReference:
    ref = PythonEnvironmentReference.model_validate_json(_encoded(value), strict=True)
    marker = cast(Mapping[str, JsonValue], metadata["recipe"])
    scope = cast(Mapping[str, JsonValue], metadata["scope"])
    _need(
        ref.recipe_sha256 == marker["recipe_sha256"]
        and ref.environment_input_sha256 == marker["environment_input_sha256"]
        and ref.scope.original_reference.model_dump() == metadata["reference"]
        and str(ref.scope.tenant_id) == scope["tenant_id"]
        and str(ref.scope.run_id) == scope["run_id"]
        and ref.scope.project_id == scope["project_id"]
        and ref.scope.session_id == scope["workspace_session_id"]
    )
    return ref


def _report(
    value: object,
    metadata: Mapping[str, JsonValue],
    *,
    passed: bool,
    sibling: object = None,
    source_label: object = None,
) -> Mapping[str, JsonValue]:
    if passed:
        _need(isinstance(value, Mapping))
        assert isinstance(value, Mapping)
        report = cast(Mapping[str, JsonValue], value)
    else:
        report = _failure_shape(value)
        _need(report["original_check_contract"] == metadata["protected_sha256"])
    recipe = cast(Mapping[str, JsonValue], metadata["recipe"])
    _need(
        report["recipe_sha256"] == recipe["recipe_sha256"]
        and report["environment_input_sha256"] == recipe["environment_input_sha256"]
        and report["runtime"] == metadata["runtime"]
    )
    environment = report["environment_sha256"]
    expected_environment: str | None = None
    if environment is not None:
        dependency = cast(Mapping[str, JsonValue], report["dependency_install"])
        if sibling is not None:
            ref = _validated_reference(sibling, metadata)
            _need(ref.origin_execution_id == dependency["origin_execution_id"])
            expected_environment = ref.environment_sha256
            _need(environment == expected_environment)
        else:
            refs = cast(tuple[Mapping[str, JsonValue], ...], metadata["environment_references"])
            match = next(
                (
                    ref
                    for ref in refs
                    if ref.get("environment_sha256") == environment
                    and ref.get("origin_execution_id") == dependency["origin_execution_id"]
                ),
                None,
            )
            _need(match is not None)
            assert match is not None
            expected_environment = cast(str, match["environment_sha256"])
    else:
        _need(
            sibling is None
            and not passed
            and len(cast(tuple[JsonValue, ...], report["checks"])) == 1
        )
    if passed:
        assert expected_environment is not None
        # The source label comes from the trusted sibling contract binding. It
        # is historical only; the later ZIP attestation independently binds it.
        return validate_python_passed_receipt(
            value,
            expected_source=_hash(source_label),
            expected_recipe=cast(str, recipe["recipe_sha256"]),
            expected_environment_inputs=cast(str, recipe["environment_input_sha256"]),
            expected_environment=expected_environment,
        )
    return report


def python_binding_matches(
    context: TaskContext, result: Mapping[str, JsonValue], source: str
) -> bool:
    try:
        metadata = _history(context)
        reference = cast(Mapping[str, JsonValue], metadata["reference"])
        _hash(source)
        return result.get("original_check_contract") == {
            "schema_version": 1,
            "contract_sha256": metadata["protected_sha256"],
            "protected_sha256": metadata["protected_sha256"],
            "baseline_envelope_sha256": reference["contract_sha256"],
            "baseline_source_sha256": reference["input_source_sha256"],
            "baseline_observation_sha256": metadata["observation_sha256"],
            "candidate_source_sha256": source,
        }
    except _INVALID:
        return False


def python_success_source(context: TaskContext, result: Mapping[str, JsonValue]) -> str | None:
    try:
        if "python_environment_reference" in result:
            _need(result["python_environment_reference"] is not None)
        binding = result.get("original_check_contract")
        _need(isinstance(binding, Mapping))
        assert isinstance(binding, Mapping)
        report = _report(
            result.get("verification"),
            _history(context),
            passed=True,
            sibling=result.get("python_environment_reference"),
            source_label=binding.get("candidate_source_sha256"),
        )
        source = _hash(report["source_sha256"])
        _need(python_binding_matches(context, result, source))
        return source
    except _INVALID:
        return None


def python_failure_feedback(
    payload: object, *, tool_name: object
) -> Mapping[str, JsonValue] | None:
    """Structural diagnostic forwarding only. It cannot grant failure/delivery credit."""
    try:
        _need(tool_name == "project.verify" and isinstance(payload, Mapping))
        assert isinstance(payload, Mapping)
        _need("approval_id" not in payload and "verification_failure" not in payload)
        report = _failure_shape(payload.get("python_verification_failure"))
        diagnostic = payload.get("execution_diagnostic")
        _validate_original_diagnostic(diagnostic, retain_tail=True)
        assert isinstance(diagnostic, Mapping)
        last = cast(tuple[Mapping[str, JsonValue], ...], report["checks"])[-1]
        _need(
            last["execution_diagnostic"]
            == {key: value for key, value in diagnostic.items() if key not in {"stdout", "stderr"}}
            and diagnostic["backend"] == "systemd"
            and type(diagnostic["exit_code"]) is int
            and diagnostic["exit_code"] != 0
            and diagnostic["timed_out"] is False
        )
        result: dict[str, JsonValue] = {
            "status": "failed",
            "untrusted": True,
            "execution_diagnostic": diagnostic,
            "python_verification_failure": report,
        }
        if "python_environment_reference" in payload:
            ref = payload["python_environment_reference"]
            result["python_environment_reference"] = (
                None
                if ref is None
                else cast(
                    Mapping[str, JsonValue],
                    PythonEnvironmentReference.model_validate_json(
                        _encoded(ref), strict=True
                    ).model_dump(mode="json"),
                )
            )
        return _freeze_object(result, name="Python failure feedback")
    except _INVALID:
        return None


def python_failure_current(context: TaskContext, payload: Mapping[str, JsonValue]) -> bool:
    try:
        feedback = python_failure_feedback(payload, tool_name="project.verify")
        _need(feedback is not None)
        assert feedback is not None
        _report(
            feedback["python_verification_failure"],
            _history(context),
            passed=False,
            sibling=feedback.get("python_environment_reference"),
        )
        return True
    except _INVALID:
        return False


def python_original_failed(context: TaskContext) -> bool:
    try:
        metadata = _history(context)
        state = cast(Mapping[str, JsonValue], context.routing_decision["project_input_preparation"])
        observation = cast(Mapping[str, JsonValue], state["observation"])
        report = _report(observation.get("verification"), metadata, passed=False)
        reference = cast(Mapping[str, JsonValue], metadata["reference"])
        _need(
            report["source_sha256"] == reference["input_source_sha256"]
            and report["execution_id"] == metadata["original_execution_id"]
            and observation.get("original_check_contract")
            == {
                "schema_version": 1,
                "contract_sha256": metadata["protected_sha256"],
                "protected_sha256": metadata["protected_sha256"],
            }
        )
        return python_failure_current(
            context,
            {
                "python_verification_failure": report,
                "execution_diagnostic": observation.get("execution_diagnostic"),
            },
        )
    except _INVALID:
        return False


def python_initial_files(context: TaskContext) -> dict[str, tuple[str, str, int]]:
    try:
        rows = cast(tuple[Mapping[str, JsonValue], ...], _history(context)["initial_files"])
        return {
            cast(str, row["path"]): (
                cast(str, row["path"]),
                cast(str, row["sha256"]),
                cast(int, row["size_bytes"]),
            )
            for row in rows
        }
    except _INVALID:
        return {}


def python_productive_change(
    context: TaskContext, item: tuple[str, str, int], known: Mapping[str, tuple[str, str, int]]
) -> bool:
    """Consume only file metadata from a caller-validated successful tool outcome."""
    try:
        metadata = _history(context)
        initial = python_initial_files(context)
        path = item[0]
        _hash(item[1])
        _need(type(item[2]) is int and item[2] >= 0)
        previous = known.get(path, initial.get(path))
        return _editable_python_path(metadata, path, initial) and (
            previous is None or previous[1] != item[1]
        )
    except _INVALID:
        return False


def python_retry_digest(context: TaskContext, known: Mapping[str, str]) -> str:
    """Known hashes are acknowledged writes, never raw proposed model paths."""
    metadata = _history(context)
    initial = python_initial_files(context)
    files = {path: item[1] for path, item in initial.items()}
    for path, digest in known.items():
        if _editable_python_path(metadata, path, initial):
            files[path] = _hash(digest)
    refs = cast(tuple[Mapping[str, JsonValue], ...], metadata["environment_references"])
    return hashlib.sha256(
        _encoded(
            {
                "files": files,
                "recipe": metadata["recipe"],
                "environments": sorted({cast(str, ref["environment_sha256"]) for ref in refs}),
            }
        )
    ).hexdigest()


def _editable_python_path(
    metadata: Mapping[str, JsonValue], path: str, initial: Mapping[str, tuple[str, str, int]]
) -> bool:
    try:
        _path(path)
        return (
            path.endswith(".py")
            and not any(part.startswith(".") for part in path.split("/"))
            and _editable(path, cast(tuple[str, ...], metadata["editable_paths"]))
            and not any(
                path.startswith(root + "/")
                for root in cast(tuple[str, ...], metadata["editable_files"])
            )
            and not any(
                path.startswith(existing + "/")
                or (path != existing and path.casefold() == existing.casefold())
                for existing in initial
            )
        )
    except _INVALID:
        return False
