"""Python success receipts preserve data, not provenance or execution authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Protocol, cast

import pytest

from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.capabilities.python_project_receipts import validate_python_passed_receipt
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.base import SkillResult

_EXPECTED = {
    "expected_source": "a" * 64,
    "expected_recipe": "b" * 64,
    "expected_environment_inputs": "c" * 64,
    "expected_environment": "d" * 64,
}
_HASH_FIELDS = (
    ("source_sha256", "expected_source"),
    ("recipe_sha256", "expected_recipe"),
    ("environment_input_sha256", "expected_environment_inputs"),
    ("environment_sha256", "expected_environment"),
)
_MISSING = object()
_MAX_INT = 2**63 - 1


class Validator(Protocol):
    def __call__(
        self, value: object, *, expected_source: str, expected_recipe: str,
        expected_environment_inputs: str, expected_environment: str,
    ) -> Mapping[str, JsonValue]: ...


@pytest.fixture
def validator() -> Validator:
    return validate_python_passed_receipt


def _diagnostic(stdout: str = "ok\n", stderr: str = "") -> dict[str, object]:
    diagnostic = ExecutionDiagnostic(
        backend="systemd",
        result=SkillResult(exit_code=0, timed_out=False, stdout=stdout, stderr=stderr),
    )
    return {key: value for key, value in diagnostic.to_payload().items()
            if key not in {"stdout", "stderr"}}


def _receipt() -> dict[str, object]:
    return {
        "schema_version": 2, "kind": "python_project", "status": "passed",
        "execution_id": "exec-new_1",
        "source_sha256": "a" * 64, "recipe_sha256": "b" * 64,
        "environment_input_sha256": "c" * 64, "environment_sha256": "d" * 64,
        "checks": [{"phase": phase, "status": "passed", "execution_diagnostic": _diagnostic()}
                   for phase in ("install", "build", "test")],
        "runtime": {
            "implementation": "cpython", "version": "3.12.10", "abi": "cp312",
            "platform": "linux_x86_64", "binary_sha256": "1" * 64,
            "base_runtime_sha256": "2" * 64,
        },
        "wheel": {"filename": "demo-1.0-py3-none-any.whl", "sha256": "3" * 64, "size": 42},
        "test_count": 1,
        "dependency_install": {"mode": "created", "origin_execution_id": "exec-new_1"},
    }


def _change(payload: dict[str, object], path: tuple[str | int, ...], value: object) -> None:
    current: object = payload
    for part in path[:-1]:
        if isinstance(part, int):
            current = cast(list[object], current)[part]
        else:
            current = cast(dict[str, object], current)[part]
    key = cast(str, path[-1])
    target = cast(dict[str, object], current)
    if value is _MISSING:
        target.pop(key)
    else:
        target[key] = value


@pytest.mark.parametrize("mode,origin", [("created", "exec-new_1"), ("reused", "exec-old_1")])
def test_schema_two_roundtrip_is_lossless_and_deeply_frozen(
    validator: Validator, mode: str, origin: str,
) -> None:
    payload = _receipt()
    payload["dependency_install"] = {"mode": mode, "origin_execution_id": origin}
    result = validator(payload, **_EXPECTED)
    assert json.loads(json.dumps(result, default=dict)) == payload
    assert validator(result, **_EXPECTED) == result
    assert isinstance(result["checks"], tuple)
    with pytest.raises(TypeError):
        cast(dict[str, JsonValue], result)["test_count"] = 0
    with pytest.raises(TypeError):
        cast(dict[str, JsonValue], result["wheel"])["size"] = 0
    check = cast(tuple[Mapping[str, JsonValue], ...], result["checks"])[0]
    diagnostic = cast(Mapping[str, JsonValue], check["execution_diagnostic"])
    with pytest.raises(TypeError):
        cast(dict[str, JsonValue], diagnostic["stdout_metadata"])["sha256"] = "0" * 64
    _change(payload, ("checks", 0, "phase"), "test")
    assert check["phase"] == "install"


@pytest.mark.parametrize("field,expected", _HASH_FIELDS)
def test_each_current_hash_must_match_the_trusted_caller(
    validator: Validator, field: str, expected: str,
) -> None:
    payload = _receipt()
    payload[field] = "e" * 64
    with pytest.raises(ValueError):
        validator(payload, **_EXPECTED)
    with pytest.raises(ValueError):
        validator(_receipt(), **(_EXPECTED | {expected: "f" * 64}))


def test_wrong_tenant_is_rejected_by_scoped_recipe_hash(validator: Validator) -> None:
    other_tenant_recipe = hashlib.sha256(b"tenant-two/scoped-recipe").hexdigest()
    with pytest.raises(ValueError):
        validator(_receipt(), **(_EXPECTED | {"expected_recipe": other_tenant_recipe}))


@pytest.mark.parametrize("field,expected", _HASH_FIELDS)
@pytest.mark.parametrize("bad", ["", "A" * 64, "a" * 63, "a" * 65, "g" * 64,
                                      "a" * 64 + "\n", True, 0, None, b"a" * 64])
def test_hashes_are_strict_even_when_report_and_expected_agree(
    validator: Validator, field: str, expected: str, bad: object,
) -> None:
    payload = _receipt()
    payload[field] = bad
    expectations: dict[str, object] = _EXPECTED | {expected: bad}
    with pytest.raises((ValueError, TypeError)):
        validator(payload, **cast(dict[str, str], expectations))


@pytest.mark.parametrize("expected", list(_EXPECTED))
def test_expected_hashes_are_required(validator: Validator, expected: str) -> None:
    expectations = dict(_EXPECTED)
    del expectations[expected]
    with pytest.raises(TypeError):
        validator(_receipt(), **expectations)


@pytest.mark.parametrize("path,bad", [
    (("schema_version",), 1), (("schema_version",), True), (("schema_version",), 2.0),
    (("kind",), "node_project"), (("status",), "failed"), (("status",), "blocked"),
    (("checks",), []), (("checks",), [{"phase": "install", "status": "passed"}] * 3),
    (("checks", 0, "phase"), "build"), (("checks", 1, "status"), "not_run"),
    (("checks", 2, "status"), "failed"), (("checks", 0, "execution_diagnostic"), _MISSING),
    (("runtime", "implementation"), "pypy"), (("runtime", "version"), "3.11.9"),
    (("runtime", "version"), True), (("runtime", "abi"), "cp311"),
    (("runtime", "platform"), "win_amd64"), (("runtime", "binary_sha256"), "A" * 64),
    (("runtime", "base_runtime_sha256"), "unmeasured"),
    (("wheel", "sha256"), "A" * 64), (("wheel", "size"), 0),
    (("wheel", "size"), True), (("wheel", "size"), -1),
    (("wheel", "size"), 1.0), (("wheel", "size"), _MAX_INT + 1),
    (("test_count",), 0), (("test_count",), True), (("test_count",), -1),
    (("test_count",), 1.0), (("test_count",), "1"), (("test_count",), _MAX_INT + 1),
    (("dependency_install", "mode"), "fresh"),
    (("dependency_install", "origin_execution_id"), "exec-old_1"),
    (("dependency_install", "mode"), "reused"),
])
def test_invalid_success_shape_is_rejected(
    validator: Validator, path: tuple[str | int, ...], bad: object,
) -> None:
    payload = _receipt()
    _change(payload, path, bad)
    with pytest.raises((ValueError, TypeError)):
        validator(payload, **_EXPECTED)


@pytest.mark.parametrize("bad", ["", "Exec", "_exec", "-exec", "a" * 129,
                                      "exec/new", "exec.new", "exec\n", "\u00e9xec", 1, True])
@pytest.mark.parametrize("path", [("execution_id",), ("dependency_install", "origin_execution_id")])
def test_execution_ids_use_strict_ascii_grammar(
    validator: Validator, path: tuple[str | int, ...], bad: object,
) -> None:
    payload = _receipt()
    _change(payload, path, bad)
    with pytest.raises((ValueError, TypeError)):
        validator(payload, **_EXPECTED)


def test_reuse_keeps_its_original_execution_when_a_new_uuid_is_issued(validator: Validator) -> None:
    payload = _receipt()
    payload["execution_id"] = "12345678-1234-1234-1234-123456789abc"
    payload["dependency_install"] = {"mode": "reused", "origin_execution_id": "exec-old_1"}
    result = validator(payload, **_EXPECTED)
    assert result["dependency_install"] == {"mode": "reused", "origin_execution_id": "exec-old_1"}
    _change(payload, ("dependency_install", "mode"), "created")
    with pytest.raises(ValueError):
        validator(payload, **_EXPECTED)


@pytest.mark.parametrize("filename", ["", ".whl", "../demo.whl", "a/demo.whl", "a\\demo.whl",
    "/demo.whl", "C:\\demo.whl", "C:demo.whl", "demo.whl:stream", "demo.WHL",
    "demo.zip", "demo.whl\n", "demo\x00.whl", True])
def test_wheel_is_a_basename_not_a_path(validator: Validator, filename: object) -> None:
    payload = _receipt()
    _change(payload, ("wheel", "filename"), filename)
    with pytest.raises((ValueError, TypeError)):
        validator(payload, **_EXPECTED)


@pytest.mark.parametrize("field,bad", [
    ("schema_version", True), ("schema_version", 2), ("schema_version", 1.0),
    ("backend", "unknown"), ("backend", "docker"), ("exit_code", False),
    ("exit_code", None), ("exit_code", 1), ("exit_code", 0.0), ("exit_code", "0"),
    ("timed_out", True), ("timed_out", 0), ("timed_out", None),
])
def test_every_phase_requires_completed_successful_process_metadata(
    validator: Validator, field: str, bad: object,
) -> None:
    for phase in range(3):
        payload = _receipt()
        _change(payload, ("checks", phase, "execution_diagnostic", field), bad)
        with pytest.raises((ValueError, TypeError)):
            validator(payload, **_EXPECTED)


@pytest.mark.parametrize("stream", ["stdout_metadata", "stderr_metadata"])
@pytest.mark.parametrize("field,bad", [
    ("sanitized_bytes", True), ("sanitized_bytes", -1), ("sanitized_bytes", _MAX_INT + 1),
    ("returned_bytes", False), ("returned_bytes", -1), ("returned_bytes", 4),
    ("returned_bytes", 0.0), ("returned_bytes", _MAX_INT + 1),
    ("redacted_lines", True), ("redacted_lines", -1), ("redacted_lines", _MAX_INT + 1),
    ("truncated", 0), ("redacted", 0), ("sha256", "A" * 64), ("sha256", "a" * 63),
    ("truncated", True), ("redacted", True), ("redacted_lines", 1),
    ("sanitized_bytes", 5000),
])
def test_stream_metadata_rejects_weak_types_and_inconsistent_byte_counts(
    validator: Validator, stream: str, field: str, bad: object,
) -> None:
    payload = _receipt()
    _change(payload, ("checks", 0, "execution_diagnostic", stream, field), bad)
    with pytest.raises((ValueError, TypeError)):
        validator(payload, **_EXPECTED)


_OBJECT_PATHS: list[tuple[str | int, ...]] = [
    (), ("runtime",), ("wheel",), ("dependency_install",), ("checks", 0),
    ("checks", 0, "execution_diagnostic"),
    ("checks", 0, "execution_diagnostic", "stdout_metadata"),
    ("checks", 0, "execution_diagnostic", "stderr_metadata"),
]


@pytest.mark.parametrize("path", _OBJECT_PATHS)
@pytest.mark.parametrize("field", ["stdout", "stderr", "unexpected"])
def test_unknown_fields_and_raw_output_are_rejected_at_every_object(
    validator: Validator, path: tuple[str | int, ...], field: str,
) -> None:
    payload = _receipt()
    _change(payload, (*path, field), "PRIVATE_SOURCE")
    with pytest.raises(ValueError) as caught:
        validator(payload, **_EXPECTED)
    assert "PRIVATE_SOURCE" not in str(caught.value)


@pytest.mark.parametrize("path", _OBJECT_PATHS)
def test_missing_fields_are_never_defaulted(
    validator: Validator, path: tuple[str | int, ...],
) -> None:
    original = _receipt()
    current: object = original
    for part in path:
        current = (cast(list[object], current)[part] if isinstance(part, int)
                   else cast(dict[str, object], current)[part])
    for field in cast(dict[str, object], current):
        payload = _receipt()
        _change(payload, (*path, field), _MISSING)
        with pytest.raises((ValueError, TypeError)):
            validator(payload, **_EXPECTED)


@pytest.mark.parametrize("value", [None, True, "{}", [], object(), {"schema_version": 1,
    "status": "passed", "source_sha256": "a" * 64, "checks": [],
    "runtime": {"node": "v22.12.0", "npm": "10.9.0"}}])
def test_non_objects_and_node_one_receipts_are_rejected(validator: Validator, value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        validator(value, **_EXPECTED)


@pytest.mark.parametrize("stdout,stderr", [("", ""), ("x" * 5000, "API_KEY=SECRET\n"),
                                           ("\u4e2d" * 2000, "error\n")])
def test_real_diagnostic_metadata_survives_without_output_text(
    validator: Validator, stdout: str, stderr: str,
) -> None:
    payload = _receipt()
    _change(payload, ("checks", 0, "execution_diagnostic"), _diagnostic(stdout, stderr))
    result = validator(payload, **_EXPECTED)
    assert json.loads(json.dumps(result, default=dict)) == payload
    assert "SECRET" not in json.dumps(result, default=dict)


def test_positive_integer_and_execution_id_boundaries_are_preserved(validator: Validator) -> None:
    payload = _receipt()
    payload["execution_id"] = "a" * 128
    payload["dependency_install"] = {"mode": "created", "origin_execution_id": "a" * 128}
    payload["test_count"] = _MAX_INT
    _change(payload, ("wheel", "size"), _MAX_INT)
    _change(payload, ("runtime", "platform"), "linux_aarch64")
    result = validator(payload, **_EXPECTED)
    assert result["test_count"] == _MAX_INT
    assert json.loads(json.dumps(result, default=dict)) == payload


@pytest.mark.parametrize("order", [("build", "install", "test"), ("install", "test", "build"),
                                  ("install", "build", "build")])
def test_all_three_phases_must_be_in_order(validator: Validator, order: tuple[str, ...]) -> None:
    payload = _receipt()
    payload["checks"] = [{"phase": phase, "status": "passed", "execution_diagnostic": _diagnostic()}
                         for phase in order]
    with pytest.raises(ValueError):
        validator(payload, **_EXPECTED)


@pytest.mark.parametrize("stream", ["stdout_metadata", "stderr_metadata"])
def test_empty_stream_hash_must_agree_with_byte_count(validator: Validator, stream: str) -> None:
    payload = _receipt()
    bad_hash = hashlib.sha256(b"").hexdigest() if stream == "stdout_metadata" else "e" * 64
    _change(payload, ("checks", 0, "execution_diagnostic", stream, "sha256"), bad_hash)
    with pytest.raises(ValueError):
        validator(payload, **_EXPECTED)
