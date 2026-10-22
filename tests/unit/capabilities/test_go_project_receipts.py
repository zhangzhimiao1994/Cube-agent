"""Go receipts validate inert data, never native measurements or authority."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Literal, cast
from uuid import UUID

import pytest

from agent_hub.capabilities.go_project_acceptance import GoAcceptanceCriterion, GoAcceptanceManifest
from agent_hub.capabilities.go_project_business import GoBusinessCase, GoBusinessSuite
from agent_hub.capabilities.go_project_inputs import GoProjectInputs
from agent_hub.capabilities.go_project_recipe import parse_go_project_declaration
from agent_hub.capabilities.project_baselines import ProjectBaselineReference
from agent_hub.capabilities.project_contracts import build_project_check_contract
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainPlanScope
from agent_hub.skills.sandbox.toolchain_protocol import ToolchainReference

_MODULE = "agent_hub.capabilities.go_project_receipts"
_ERROR = "invalid Go passed receipt"
_BINARY = "b" * 64
_EXECUTION = "exec-go_1"
_MISSING = object()


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode("ascii")).hexdigest()


@pytest.fixture
def inputs() -> GoProjectInputs:
    suite = GoBusinessSuite((
        GoBusinessCase("success", "AC-001", ("--echo",), "\u4e2d\n", "\u4e2d\r\n", "", 0),
        GoBusinessCase("invalid", "AC-001", ("--invalid",), "", "", "invalid\n", 2),
    ))
    acceptance = GoAcceptanceManifest(
        editable_paths=("main.go",), test_files=("main_test.go",),
        criteria=(GoAcceptanceCriterion("AC-001", "Handle valid and invalid input",
                                        ("example.org/demo::TestA", "example.org/demo::TestB")),),
        business=suite,
    )
    return _inputs(acceptance)


def _inputs(acceptance: GoAcceptanceManifest | None) -> GoProjectInputs:
    raw = {
        "go.mod": b"module example.org/demo\ngo 1.27\n",
        "main.go": b"package main\nfunc main() {}\n",
        "main_test.go": (b'package main\nimport "testing"\n'
                         b"func TestA(t *testing.T) {}\nfunc TestB(t *testing.T) {}\n"),
    }
    if acceptance is not None:
        raw["ACCEPTANCE.json"] = json.dumps(acceptance.to_payload()).encode()
    files = {name: hashlib.sha256(b"f\0" + data).hexdigest() for name, data in raw.items()}
    source = _digest(files)
    contract = build_project_check_contract(
        UUID(int=1), UUID(int=3), "demo", "session", source, ("main.go",), files,
    )
    reference = ProjectBaselineReference(
        _digest({"schema_version": 1, "contract": contract.to_payload(), "source_files": files}),
        source, len(files),
    )
    scope = ToolchainPlanScope(
        tenant_id=UUID(int=1), user_id=UUID(int=2), run_id=UUID(int=3), project_id="demo",
        session_id="session", actor_id="main_agent", actor_role="operator",
        source_sha256=source, contract_sha256=reference.contract_sha256,
    )
    sdk = ToolchainReference(
        artifact_sha256="4" * 64, archive_sha256="5" * 64, tree_sha256="6" * 64,
        binary_sha256="7" * 64, gofmt_sha256="8" * 64, total_bytes=1234,
        file_count=100, version="1.27.3", platform="linux_amd64",
    )
    return GoProjectInputs(
        scope=scope, contract=contract, original_reference=reference, toolchain=sdk,
        source_files=files, source_limit_bytes=1024**2, file_limit=100,
        declaration=parse_go_project_declaration(raw), acceptance=acceptance,
    )


def _receipt(inputs: GoProjectInputs) -> dict[str, Any]:
    acceptance = inputs.acceptance
    assert acceptance is not None
    evidence = []
    for case in acceptance.business.cases:
        row = {
            "case_id": case.id, "criterion_id": case.criterion_id,
            "case_sha256": _digest(case.to_payload()), "expected_exit": case.expected_exit,
            "exit_code": case.expected_exit, "timed_out": False,
            "stdout_truncated": False, "stderr_truncated": False,
            "stdout_utf8_complete": True, "stderr_utf8_complete": True,
            "stdout_matches": True, "stderr_matches": True, "passed": True, "reason": None,
        }
        for stream in ("stdout", "stderr"):
            raw = getattr(case, "expected_" + stream).encode("utf-8")
            row[stream + "_sha256"] = hashlib.sha256(raw).hexdigest()
            row[stream + "_observed_bytes"] = len(raw)
        evidence.append(row)
    return {
        "schema_version": 1, "kind": "go_project_execution_receipt", "status": "passed",
        "execution_id": _EXECUTION, "scope": inputs.scope.to_payload(),
        "source_kind": "original", "source_sha256": inputs.scope.source_sha256,
        "recipe_sha256": inputs.sha256,
        "environment_input_sha256": inputs.environment_input_sha256,
        "original_reference": inputs.original_reference.to_payload(),
        "original_check_contract": inputs.contract.protected_sha256,
        "toolchain": inputs.toolchain.model_dump(mode="json"),
        "acceptance_sha256": acceptance.sha256,
        "business_suite_sha256": acceptance.business.sha256,
        "binary": {"filename": "project", "sha256": _BINARY, "size": 42,
                   "format": "ELF64", "target": "linux_amd64"},
        "checks": [{"phase": phase, "status": "passed"}
                   for phase in ("inspect", "build", "test", "canary", "business")],
        "named_tests": list(acceptance.test_ids), "business_evidence": evidence,
        "verified_at": "2026-10-22T01:02:03Z",
    }


def _validate(value: object, inputs: GoProjectInputs, **changes: Any) -> Mapping[str, JsonValue]:
    spec = importlib.util.find_spec(_MODULE)
    assert spec is not None, "Go passed receipt validator is missing"
    validator = importlib.import_module(_MODULE).validate_go_passed_receipt
    return cast(Mapping[str, JsonValue], validator(value, **({
        "inputs": inputs, "expected_source": inputs.scope.source_sha256,
        "expected_binary_sha256": _BINARY,
        "expected_binary_bytes": 42, "expected_execution_id": _EXECUTION,
        "source_kind": "original",
    } | changes)))


def _at(value: Any, path: tuple[str | int, ...]) -> Any:
    for key in path:
        value = value[key]
    return value


def _change(value: Any, path: tuple[str | int, ...], replacement: object) -> None:
    parent = _at(value, path[:-1])
    if replacement is _MISSING:
        del parent[path[-1]]
    else:
        parent[path[-1]] = replacement


def _reject(value: object, inputs: GoProjectInputs, **changes: Any) -> None:
    with pytest.raises(ValueError) as caught:
        _validate(value, inputs, **changes)
    assert str(caught.value) == _ERROR
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("source_kind", ["original", "candidate"])
def test_roundtrip_deep_freeze_and_detachment(
    inputs: GoProjectInputs, source_kind: Literal["original", "candidate"],
) -> None:
    value = _receipt(inputs)
    value["source_kind"] = source_kind
    result = _validate(value, inputs, source_kind=source_kind)
    assert json.loads(json.dumps(result, default=dict)) == value
    assert _validate(result, inputs, source_kind=source_kind) == result
    assert isinstance(result["business_evidence"], tuple)
    for path in ((), ("binary",), ("scope",), ("toolchain",), ("original_reference",),
                 ("checks", 0), ("business_evidence", 0)):
        with pytest.raises(TypeError):
            _at(result, path)["extra"] = "PRIVATE_SOURCE"
    _change(value, ("business_evidence", 0, "exit_code"), 123)
    assert _at(result, ("business_evidence", 0, "exit_code")) == 0


@pytest.mark.parametrize("field", [
    "execution_id", "source_sha256", "recipe_sha256", "environment_input_sha256",
    "original_check_contract", "acceptance_sha256", "business_suite_sha256",
])
def test_every_binding_rejects_an_unrelated_measurement(inputs: GoProjectInputs, field: str) -> None:
    value = _receipt(inputs)
    value[field] = "unrelated" if field == "execution_id" else "f" * 64
    _reject(value, inputs)


@pytest.mark.parametrize("field,bad", [
    ("schema_version", 0), ("schema_version", 2), ("schema_version", None),
    ("kind", "go_project_diagnostic"), ("kind", "python_project"),
    ("status", "failed"), ("status", "blocked"), ("status", True),
    ("source_kind", "candidate"), ("source_kind", "diagnostic"), ("source_kind", None),
])
def test_only_the_selected_passed_receipt_schema_is_accepted(
    inputs: GoProjectInputs, field: str, bad: object,
) -> None:
    value = _receipt(inputs)
    value[field] = bad
    _reject(value, inputs)


@pytest.mark.parametrize("path", [
    ("scope", "tenant_id"), ("scope", "user_id"), ("scope", "run_id"),
    ("scope", "project_id"), ("scope", "session_id"), ("scope", "actor_id"),
    ("scope", "actor_role"), ("scope", "source_sha256"), ("scope", "contract_sha256"),
    ("original_reference", "contract_sha256"), ("original_reference", "input_source_sha256"),
    ("toolchain", "artifact_sha256"), ("toolchain", "archive_sha256"),
    ("toolchain", "tree_sha256"), ("toolchain", "binary_sha256"),
    ("toolchain", "gofmt_sha256"), ("toolchain", "version"), ("toolchain", "platform"),
    ("toolchain", "total_bytes"), ("toolchain", "file_count"),
    ("original_reference", "file_count"), ("original_reference", "schema_version"),
])
def test_scope_reference_and_entire_sdk_are_bound(
    inputs: GoProjectInputs, path: tuple[str | int, ...],
) -> None:
    value = _receipt(inputs)
    _change(value, path, "PRIVATE_SOURCE")
    _reject(value, inputs)


@pytest.mark.parametrize("field,bad", [
    ("filename", "../project"), ("filename", "other"), ("sha256", "f" * 64),
    ("format", "ELF32"), ("target", "windows_amd64"),
    ("size", 0), ("size", -1), ("size", True), ("size", 42.0), ("size", 43),
    ("size", 2**63),
])
def test_binary_matches_the_measured_export(inputs: GoProjectInputs, field: str, bad: object) -> None:
    value = _receipt(inputs)
    _change(value, ("binary", field), bad)
    _reject(value, inputs)


@pytest.mark.parametrize("expected,bad", [
    ("expected_source", "f" * 64), ("expected_binary_sha256", "f" * 64),
    ("expected_binary_bytes", 43), ("expected_execution_id", "other"),
    ("expected_binary_bytes", True), ("expected_binary_bytes", 42.0),
    ("expected_binary_bytes", 0), ("expected_binary_bytes", 2**63),
    ("source_kind", "diagnostic"), ("source_kind", True),
])
def test_expected_values_never_default_from_the_receipt(
    inputs: GoProjectInputs, expected: str, bad: object,
) -> None:
    _reject(_receipt(inputs), inputs, **{expected: bad})


@pytest.mark.parametrize("bad", ["", "A" * 64, "g" * 64, "a" * 63, "a" * 65,
                                  "a" * 64 + "\n", True, 1, None])
@pytest.mark.parametrize("field,expected", [
    ("source_sha256", "expected_source"), ("binary", "expected_binary_sha256"),
])
def test_agreeing_invalid_hashes_are_rejected(
    inputs: GoProjectInputs, field: str, expected: str, bad: object,
) -> None:
    value = _receipt(inputs)
    _change(value, ("binary", "sha256") if field == "binary" else (field,), bad)
    _reject(value, inputs, **{expected: bad})


@pytest.mark.parametrize("bad", ["", "Exec", "_exec", "a" * 129, "exec/new", "exec\n", True])
def test_agreeing_invalid_execution_ids_are_rejected(inputs: GoProjectInputs, bad: object) -> None:
    value = _receipt(inputs)
    value["execution_id"] = bad
    _reject(value, inputs, expected_execution_id=bad)


def test_candidate_source_is_measured_separately_from_original(inputs: GoProjectInputs) -> None:
    value = _receipt(inputs)
    value["source_sha256"] = "e" * 64
    _reject(value, inputs, expected_source="e" * 64)
    value["source_kind"] = "candidate"
    _validate(value, inputs, expected_source="e" * 64, source_kind="candidate")
    _reject(value, inputs, source_kind="candidate")


@pytest.mark.parametrize("bad", [[], ["inspect"] * 5, None])
def test_all_five_checks_are_required(inputs: GoProjectInputs, bad: object) -> None:
    value = _receipt(inputs)
    value["checks"] = bad
    _reject(value, inputs)


@pytest.mark.parametrize("index", range(5))
@pytest.mark.parametrize("field,bad", [("phase", "other"), ("status", "failed")])
def test_each_check_is_ordered_and_passed(
    inputs: GoProjectInputs, index: int, field: str, bad: object,
) -> None:
    value = _receipt(inputs)
    _change(value, ("checks", index, field), bad)
    _reject(value, inputs)


@pytest.mark.parametrize("bad", [[], ["example.org/demo::TestA"] * 2,
                                  ["example.org/demo::TestA"], [True], [{"status": "passed"}]])
def test_named_tests_are_exact_nonempty_unique_ids(inputs: GoProjectInputs, bad: object) -> None:
    value = _receipt(inputs)
    value["named_tests"] = bad
    _reject(value, inputs)


def test_named_test_order_does_not_create_a_different_set(inputs: GoProjectInputs) -> None:
    value = _receipt(inputs)
    value["named_tests"].reverse()
    _validate(value, inputs)


@pytest.mark.parametrize("field,bad", [
    ("case_id", "other"), ("criterion_id", "AC-002"), ("case_sha256", "f" * 64),
    ("expected_exit", 1), ("expected_exit", False), ("expected_exit", 0.0),
    ("exit_code", 1), ("exit_code", False), ("exit_code", 0.0),
    ("timed_out", True), ("timed_out", 0),
    ("passed", False), ("passed", 1), ("reason", "PRIVATE_SOURCE"), ("reason", False),
])
def test_passed_flag_cannot_hide_wrong_case_or_exit(
    inputs: GoProjectInputs, field: str, bad: object,
) -> None:
    value = _receipt(inputs)
    _change(value, ("business_evidence", 0, field), bad)
    _reject(value, inputs)


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("suffix,bad", [
    ("sha256", "f" * 64), ("sha256", "A" * 64), ("observed_bytes", -1),
    ("observed_bytes", True), ("observed_bytes", 100), ("observed_bytes", 0.0),
    ("observed_bytes", 2**63), ("truncated", True), ("truncated", 0),
    ("utf8_complete", False), ("utf8_complete", 1), ("matches", False), ("matches", 1),
])
def test_both_complete_streams_are_compared_to_original_utf8_bytes(
    inputs: GoProjectInputs, index: int, stream: str, suffix: str, bad: object,
) -> None:
    value = _receipt(inputs)
    _change(value, ("business_evidence", index, stream + "_" + suffix), bad)
    _reject(value, inputs)


@pytest.mark.parametrize("mode", ["missing", "duplicate", "reverse", "extra"])
def test_business_evidence_is_one_per_original_case_in_order(
    inputs: GoProjectInputs, mode: str,
) -> None:
    value = _receipt(inputs)
    rows = value["business_evidence"]
    value["business_evidence"] = {
        "missing": rows[:1], "duplicate": [rows[0], rows[0]],
        "reverse": list(reversed(rows)), "extra": [*rows, rows[0]],
    }[mode]
    _reject(value, inputs)


def test_case_digest_covers_argv_stdin_and_literal_line_endings(inputs: GoProjectInputs) -> None:
    value = _receipt(inputs)
    assert inputs.acceptance is not None
    case = inputs.acceptance.business.cases[0]
    for change in ({"argv": ("--other",)}, {"stdin": "other"}, {"expected_stdout": "\u4e2d\n"}):
        _change(value, ("business_evidence", 0, "case_sha256"), _digest(replace(case, **change).to_payload()))
        _reject(value, inputs)


def test_existing_case_digest_property_is_used(
    inputs: GoProjectInputs, monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _receipt(inputs)
    monkeypatch.setattr(GoBusinessCase, "sha256", property(lambda _: "e" * 64), raising=False)
    _reject(value, inputs)
    for row in value["business_evidence"]:
        row["case_sha256"] = "e" * 64
    _validate(value, inputs)


def test_diagnostic_inputs_without_acceptance_cannot_pass(inputs: GoProjectInputs) -> None:
    _reject(_receipt(inputs), _inputs(None))


def test_large_valid_suite_uses_trusted_structure_allocation(inputs: GoProjectInputs) -> None:
    assert inputs.acceptance is not None
    suite = inputs.acceptance.business
    larger = GoBusinessSuite(tuple(
        replace(suite.cases[index % 2], id=f"case-{index}") for index in range(160)
    ))
    current = _inputs(replace(inputs.acceptance, business=larger))
    value = _receipt(current)
    rows = _validate(value, current)["business_evidence"]
    assert isinstance(rows, tuple) and len(rows) == 160


@pytest.mark.parametrize("raw", [b"\xe4\xb8\xad\n", b"\xef\xbf\xbd\r\n", b"\xe4\xb8\xad"])
def test_normalized_replaced_or_partial_bytes_cannot_pass_with_matching_flags(
    inputs: GoProjectInputs, raw: bytes,
) -> None:
    value = _receipt(inputs)
    row = value["business_evidence"][0]
    row["stdout_sha256"] = hashlib.sha256(raw).hexdigest()
    row["stdout_observed_bytes"] = len(raw)
    _reject(value, inputs)


@pytest.mark.parametrize("expected", ["inputs", "expected_source", "expected_binary_sha256",
    "expected_binary_bytes", "expected_execution_id", "source_kind"])
def test_every_expected_argument_is_required(inputs: GoProjectInputs, expected: str) -> None:
    validator = importlib.import_module(_MODULE).validate_go_passed_receipt
    arguments: dict[str, object] = {
        "inputs": inputs, "expected_source": inputs.scope.source_sha256,
        "expected_binary_sha256": _BINARY, "expected_binary_bytes": 42,
        "expected_execution_id": _EXECUTION, "source_kind": "original",
    }
    del arguments[expected]
    with pytest.raises(TypeError):
        validator(_receipt(inputs), **arguments)


_OBJECTS: list[tuple[str | int, ...]] = [
    (), ("scope",), ("original_reference",), ("toolchain",), ("binary",),
    *(("checks", index) for index in range(5)),
    ("business_evidence", 0), ("business_evidence", 1),
]


@pytest.mark.parametrize("path", _OBJECTS)
@pytest.mark.parametrize("extra", ["unknown", "stdout", "execution_diagnostic"])
def test_unknown_fields_and_diagnostics_are_never_accepted(
    inputs: GoProjectInputs, path: tuple[str | int, ...], extra: str,
) -> None:
    value = _receipt(inputs)
    _change(value, (*path, extra), "PRIVATE_SOURCE")
    _reject(value, inputs)


@pytest.mark.parametrize("path", _OBJECTS)
def test_no_missing_field_is_defaulted(inputs: GoProjectInputs, path: tuple[str | int, ...]) -> None:
    for key in _at(_receipt(inputs), path):
        value = _receipt(inputs)
        _change(value, (*path, key), _MISSING)
        _reject(value, inputs)


@pytest.mark.parametrize("path", [("schema_version",), ("original_reference", "schema_version"),
                                  ("original_reference", "file_count"), ("toolchain", "total_bytes")])
@pytest.mark.parametrize("bad_type", [bool, float])
def test_nested_numeric_bindings_use_json_types(
    inputs: GoProjectInputs, path: tuple[str | int, ...], bad_type: type[bool | float],
) -> None:
    value = _receipt(inputs)
    _change(value, path, bad_type(_at(value, path)))
    _reject(value, inputs)


@pytest.mark.parametrize("value", [None, True, [], "{}", b"{}", object(),
                                  {"status": "passed", "stdout": "PRIVATE_SOURCE"}])
def test_raw_json_and_native_diagnostics_are_not_receipts(inputs: GoProjectInputs, value: object) -> None:
    _reject(value, inputs)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), b"raw", object(),
                                  "\ud800"])
def test_bad_json_is_rejected_without_leaking_content(inputs: GoProjectInputs, bad: object) -> None:
    value = _receipt(inputs)
    value["PRIVATE_SOURCE"] = bad
    _reject(value, inputs)


@pytest.mark.parametrize("timestamp", ["2000-01-01T00:00:00Z", "2999-01-01T00:00:00+00:00",
                                        "2024-02-29T23:59:59.123456789Z"])
def test_timestamp_format_has_no_freshness_claim(inputs: GoProjectInputs, timestamp: str) -> None:
    value = _receipt(inputs)
    value["verified_at"] = timestamp
    _validate(value, inputs)


@pytest.mark.parametrize("timestamp", [None, True, "", "2026-10-22", "2026-10-22T01:02:03",
    "2026-10-22T01:02:03+08:00", "2026-10-22T01:02:03-00:00", "2026-02-30T00:00:00Z",
    "2026-10-22 01:02:03Z", "2026-10-22T01:02:03Z\n"])
def test_timestamp_requires_a_valid_utc_rfc3339_value(inputs: GoProjectInputs, timestamp: object) -> None:
    value = _receipt(inputs)
    value["verified_at"] = timestamp
    _reject(value, inputs)
