"""Pure generated acceptance values; trusted filesystem/authority are caller-owned."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
from dataclasses import FrozenInstanceError, replace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.capabilities.project_baselines import ProjectBaselineReference
from agent_hub.capabilities.project_contracts import (
    ManagedProjectInput,
    build_project_check_contract,
)


@pytest.fixture
def api() -> Any:
    return importlib.import_module("agent_hub.capabilities.project_acceptance")


def payload(stack: str = "node") -> dict[str, Any]:
    return {"schema_version": 1, "stack": stack, "editable_paths": ["src"],
        "test_files": ["tests/test_core.py" if stack == "python" else "tests/core.test.cjs"],
        "criteria": [{"id": "AC-001", "description": "Return the requested total",
                      "tests": ["test_core.CoreTests.test_total" if stack == "python" else "total"]}]}


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def fingerprint(value: object) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def raw_manifest(stack: str = "node") -> bytes:
    return encoded(payload(stack))


def source_files(stack: str = "node", raw: bytes | None = None) -> dict[str, str]:
    files = {"ACCEPTANCE.json": raw or raw_manifest(stack),
             payload(stack)["test_files"][0]: b"trusted caller test bytes",
             "src/__init__.py" if stack == "python" else "src/core.cjs": b"implementation"}
    if stack == "node":
        files["package.json"] = b'{"scripts":{"test":"node --test"}}'
    else:
        files.update({"pyproject.toml": b"[project]", "requirements.lock": b"hashlocked"})
    return {path: hashlib.sha256(b"f\0" + data).hexdigest() for path, data in files.items()}


def binding(stack: str = "node", raw: bytes | None = None) -> dict[str, Any]:
    files = source_files(stack, raw)
    scope = {"tenant_id": UUID(int=1), "user_id": UUID(int=2), "run_id": UUID(int=3),
             "project_id": "new-project", "session_id": "session-one", "request_sha256": "4" * 64,
             "proposer_actor": "engineer"}
    contract = build_project_check_contract(scope["tenant_id"], scope["run_id"], scope["project_id"],
        scope["session_id"], fingerprint(files), ("src",), files)
    reference = ProjectBaselineReference(fingerprint({"schema_version": 1,
        "contract": contract.to_payload(), "source_files": files}), fingerprint(files), len(files))
    return {**scope, "manifest_raw": raw or raw_manifest(stack), "reference": reference,
            "source_files": files, "contract": contract}


@pytest.mark.parametrize("stack", ["node", "python"])
def test_manifest_is_strict_frozen_canonical_and_data_only(api: Any, stack: str) -> None:
    value = api.parse_acceptance_manifest(raw_manifest(stack))
    assert value.to_payload() == payload(stack)
    assert value.sha256 == fingerprint(value.to_payload())
    assert value.test_ids == tuple(payload(stack)["criteria"][0]["tests"])
    assert value.is_editable("src/implementation.py")
    assert not value.is_editable("ACCEPTANCE.json")
    assert not value.is_editable("src/ACCEPTANCE.json")
    assert not value.is_editable("src/package.json")
    assert not value.is_editable(payload(stack)["test_files"][0])
    with pytest.raises((FrozenInstanceError, AttributeError)):
        value.stack = "other"
    output = value.to_payload()
    output["editable_paths"].append("tests")
    assert value.editable_paths == ("src",)


def test_json_formatting_order_is_not_a_semantic_revision(api: Any) -> None:
    raw = payload()
    raw["criteria"].append({"id": "AC-002", "description": "Return errors", "tests": ["bad", "missing"]})
    first = api.parse_acceptance_manifest(encoded(raw))
    raw["criteria"].reverse()
    raw["criteria"][0]["tests"].reverse()
    second = api.parse_acceptance_manifest(json.dumps(raw, indent=2).encode())
    assert first.sha256 == second.sha256
    assert first.test_ids == ("bad", "missing", "total")


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"schema_version": "1"}, {"stack": "shell"},
    {"editable_paths": []}, {"test_files": []}, {"criteria": []},
    {"command": "npm test"}, {"url": "https://example.invalid"}, {"grant": True},
    {"symlink": False}, {"tenant_id": "1"}, {"passed": True},
])
def test_unknown_authority_and_coercion_are_rejected(api: Any, change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        api.parse_acceptance_manifest(encoded(payload() | change))


@pytest.mark.parametrize("path", ["/src", "../src", "src/../lib", "src//lib", "./src", "src/",
    "src\\lib", "C:src", "src:stream", "CON", "aux.txt", "src/trailing.", "src/a b", "src/\u200blib",
    "ACCEPTANCE.json", "acceptance.JSON", "src/acceptance.json", "package.json", "tests"])
def test_edit_scopes_cannot_alias_escape_or_cover_controls_and_tests(api: Any, path: str) -> None:
    with pytest.raises(ValueError):
        api.parse_acceptance_manifest(encoded(payload() | {"editable_paths": [path]}))


@pytest.mark.parametrize("files", [["src/test.cjs"], ["tests/core.test.cjs", "tests/CORE.test.cjs"],
    ["tests/core.test.cjs", "Tests/other.cjs"], ["tests/core.test.cjs", "tests/core.test.cjs/x.cjs"],
    ["package.json"], ["tests/link", {"path": "tests/x.cjs", "symlink": True}]])
def test_test_files_are_portable_disjoint_unambiguous_files(api: Any, files: list[Any]) -> None:
    with pytest.raises(ValueError):
        api.parse_acceptance_manifest(encoded(payload() | {"test_files": files}))


@pytest.mark.parametrize("criterion", [
    {"id": "AC-000", "description": "x", "tests": ["x"]},
    {"id": "ac-001", "description": "x", "tests": ["x"]},
    {"id": "AC-001", "description": "", "tests": ["x"]},
    {"id": "AC-001", "description": "x", "tests": []},
    {"id": "AC-001", "description": "x", "tests": ["x", "x"]},
    {"id": "AC-001", "description": "x", "tests": ["x", "X"]},
    {"id": "AC-001", "description": "x", "tests": [" x"]},
    {"id": "AC-001", "description": "x", "tests": ["x\n"]},
    {"id": "AC-001", "description": "x", "tests": ["x\u200b"]},
    {"id": "AC-001", "description": "x", "tests": ["x"], "passed": True},
])
def test_criteria_and_exact_names_are_nonempty_strict_and_unambiguous(api: Any, criterion: Any) -> None:
    with pytest.raises(ValueError):
        api.parse_acceptance_manifest(encoded(payload() | {"criteria": [criterion]}))


def test_duplicate_criteria_and_shared_test_names_are_rejected(api: Any) -> None:
    first = payload()["criteria"][0]
    for second in (first.copy(), first | {"id": "AC-002"}, first | {"tests": ["other"]}):
        with pytest.raises(ValueError):
            api.parse_acceptance_manifest(encoded(payload() | {"criteria": [first, second]}))


@pytest.mark.parametrize("raw", [b"", b"[]", b'{"schema_version":NaN}',
    b'{"schema_version":1,"schema_version":1}', b"\xef\xbb\xbf{}", b" " * (128 * 1024 + 1)],
    ids=["empty", "array", "nonfinite", "duplicate", "bom", "oversize"])
def test_parser_rejects_duplicates_nonfinite_wrong_shapes_and_oversize(api: Any, raw: bytes) -> None:
    with pytest.raises(ValueError):
        api.parse_acceptance_manifest(raw)


@pytest.mark.parametrize("stack", ["node", "python"])
def test_envelope_binds_generated_provenance_without_execution_authority(api: Any, stack: str) -> None:
    args = binding(stack)
    value = api.GeneratedAcceptanceEnvelope.bind(**args)
    expected = {key: args[key] for key in ("tenant_id", "user_id", "run_id", "project_id", "session_id", "request_sha256", "proposer_actor")}
    value.require_scope(**expected)
    value.require_current(args["source_files"])
    assert value.manifest == api.parse_acceptance_manifest(args["manifest_raw"])
    assert value.sha256 == fingerprint(value.to_payload())
    assert value.kind == "generated_project_acceptance"
    assert value.proposer_actor == "engineer"
    assert value.capture_phase == "post_model"
    assert value.purpose == "generated_acceptance"
    assert value.manifest_sha256 == value.manifest.sha256
    assert not {"install_allowed", "passed", "original_observation"} & value.to_payload().keys()
    roundtrip = api.GeneratedAcceptanceEnvelope.from_payload(value.to_payload(), manifest_raw=args["manifest_raw"])
    assert roundtrip == value
    with pytest.raises(TypeError):
        value.source_files["new"] = "0" * 64
    args["source_files"]["new"] = "0" * 64
    assert "new" not in value.source_files


@pytest.mark.parametrize("key,value", [("tenant_id", UUID(int=9)), ("user_id", UUID(int=9)),
    ("run_id", UUID(int=9)), ("project_id", "other"), ("session_id", "other"), ("request_sha256", "9" * 64),
    ("proposer_actor", "reviewer")])
def test_every_authoritative_identity_and_request_is_revalidated(api: Any, key: str, value: object) -> None:
    args = binding()
    envelope = api.GeneratedAcceptanceEnvelope.bind(**args)
    expected = {name: args[name] for name in ("tenant_id", "user_id", "run_id", "project_id", "session_id", "request_sha256", "proposer_actor")}
    with pytest.raises(ValueError):
        envelope.require_scope(**(expected | {key: value}))


@pytest.mark.parametrize("change", ["reference", "source", "manifest-bytes", "contract", "missing-test"])
def test_binding_rejects_divergent_actual_inputs(api: Any, change: str) -> None:
    args = binding()
    if change == "reference":
        args["reference"] = replace(args["reference"], input_source_sha256="f" * 64)
    elif change == "source":
        args["source_files"]["src/core.cjs"] = "f" * 64
    elif change == "manifest-bytes":
        args["manifest_raw"] += b"\n"
    elif change == "contract":
        other = binding("python")
        args["contract"] = other["contract"]
    else:
        args["source_files"].pop("tests/core.test.cjs")
    with pytest.raises(ValueError):
        api.GeneratedAcceptanceEnvelope.bind(**args)


def test_current_allows_implementation_not_protected_check_or_manifest_changes(api: Any) -> None:
    args = binding()
    value = api.GeneratedAcceptanceEnvelope.bind(**args)
    value.require_current(args["source_files"] | {"src/core.cjs": "a" * 64, "src/new.cjs": "b" * 64})
    for path in ("ACCEPTANCE.json", "tests/core.test.cjs", "package.json"):
        with pytest.raises(ValueError):
            value.require_current(args["source_files"] | {path: "f" * 64})
    for path in ("src/ACCEPTANCE.json", "src/package.json", "Tests/new.cjs"):
        with pytest.raises(ValueError):
            value.require_current(args["source_files"] | {path: "f" * 64})


def test_envelope_hash_binds_user_request_manifest_and_actual_raw_format(api: Any) -> None:
    args = binding()
    value = api.GeneratedAcceptanceEnvelope.bind(**args)
    assert value.sha256 != api.GeneratedAcceptanceEnvelope.bind(**(args | {"user_id": UUID(int=9)})).sha256
    assert value.sha256 != api.GeneratedAcceptanceEnvelope.bind(**(args | {"request_sha256": "9"*64})).sha256
    alternate_raw = json.dumps(payload(), indent=2).encode()
    alternate = api.GeneratedAcceptanceEnvelope.bind(**binding(raw=alternate_raw))
    assert alternate.manifest.sha256 == value.manifest.sha256
    assert alternate.sha256 != value.sha256
    changed = copy.deepcopy(value.to_payload())
    changed["manifest"]["criteria"][0]["description"] = "weakened"
    with pytest.raises(ValueError):
        api.GeneratedAcceptanceEnvelope.from_payload(changed, manifest_raw=args["manifest_raw"])


def test_original_managed_input_contract_is_not_relaxed_or_replaced(api: Any) -> None:
    assert ManagedProjectInput(("src",)).to_payload()["kind"] == "managed_workspace"
    with pytest.raises(ValueError):
        ManagedProjectInput(("package.json",))
    args = binding()
    with pytest.raises(ValueError):
        api.GeneratedAcceptanceEnvelope.bind(**(args | {"tenant_id": "not-a-uuid"}))
    with pytest.raises(ValueError):
        api.GeneratedAcceptanceEnvelope.bind(**(args | {"request_sha256": "untrusted text"}))


@pytest.mark.parametrize("field", ["manifest", "reference", "contract"])
def test_stored_nested_versions_never_use_boolean_integer_equality(api: Any, field: str) -> None:
    args = binding()
    stored = api.GeneratedAcceptanceEnvelope.bind(**args).to_payload()
    stored[field]["schema_version"] = True
    with pytest.raises(ValueError):
        api.GeneratedAcceptanceEnvelope.from_payload(stored, manifest_raw=args["manifest_raw"])


@pytest.mark.parametrize("actor", ["", "Engineer", "../engineer", "engineer\n", "a" * 129, True])
def test_proposer_actor_is_a_strict_harness_safe_identifier(api: Any, actor: object) -> None:
    with pytest.raises(ValueError):
        api.GeneratedAcceptanceEnvelope.bind(**(binding() | {"proposer_actor": actor}))


@pytest.mark.parametrize("key,value", [("capture_phase", "pre_model"), ("purpose", "original"),
    ("proposer_actor", "root/actor"), ("manifest_sha256", "f" * 64)])
def test_stored_generated_provenance_cannot_claim_original_or_unsafe_actor(api: Any, key: str, value: object) -> None:
    args = binding()
    stored = api.GeneratedAcceptanceEnvelope.bind(**args).to_payload()
    stored[key] = value
    with pytest.raises(ValueError):
        api.GeneratedAcceptanceEnvelope.from_payload(stored, manifest_raw=args["manifest_raw"])


def test_schema2_trusted_reference_is_bound_without_reconstructing_private_baseline_digest(api: Any) -> None:
    args = binding()
    first = api.GeneratedAcceptanceEnvelope.bind(**args)
    # The store authenticates schema2's private metadata; the pure binder cannot derive it.
    args["reference"] = replace(args["reference"], contract_sha256="f" * 64)
    second = api.GeneratedAcceptanceEnvelope.bind(**args)
    assert second.reference == args["reference"]
    assert second.sha256 != first.sha256
    actor_changed = api.GeneratedAcceptanceEnvelope.bind(**(args | {"proposer_actor": "reviewer"}))
    assert actor_changed.sha256 != second.sha256
