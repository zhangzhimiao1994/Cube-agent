"""Pure report decoding fixtures, not native execution or provenance tests."""

from __future__ import annotations

import importlib
import importlib.util
import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.capabilities.project_verification import (
    ProjectVerificationError,
    ProjectVerificationFailure,
)
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.python_project_process import (
    PYTHON_PHASE_OUTPUT_WITHHELD,
    _Capture,
    python_native_report_limit,
    python_phase_capture_limit,
)
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonProjectInvocation,
)
from tests.unit.capabilities.test_python_project_inputs import bind


@pytest.fixture
def api() -> Any:
    name = "agent_hub.capabilities.python_project_reports"
    assert importlib.util.find_spec(name) is not None, "Python reports decoder missing"
    return importlib.import_module(name)


def fixture(*, reused: bool = False) -> tuple[Any, Any, Any, dict[str, Any]]:
    inputs = bind(importlib.import_module("agent_hub.capabilities.python_project_inputs"))
    invocation = PythonProjectInvocation.model_validate(
        {
            "execution_id": "python_current",
            "tenant_id": inputs.contract.tenant_id,
            "run_id": inputs.contract.run_id,
            "project_id": inputs.contract.project_id,
            "session_id": inputs.contract.session_id,
            "original_reference": inputs.original_reference.to_payload(),
            "sandbox_profile": "workspace_write",
            "timeout_seconds": 180,
            "source_kind": "original",
            "install_allowed": not reused,
            "recipe_sha256": inputs.sha256,
            "environment_input_sha256": inputs.environment_input_sha256,
        },
        strict=True,
    )
    reference = PythonEnvironmentReference.model_validate(
        {
            "scope": {
                key: getattr(invocation, key)
                for key in (
                    "tenant_id",
                    "run_id",
                    "project_id",
                    "session_id",
                    "original_reference",
                )
            },
            "recipe_sha256": inputs.sha256,
            "environment_input_sha256": inputs.environment_input_sha256,
            "environment_sha256": "e" * 64,
            "security_manifest_sha256": "f" * 64,
            "origin_execution_id": "previous" if reused else invocation.execution_id,
        },
        strict=True,
    )
    wheel = {"filename": "billing-1.0.0-py3-none-any.whl", "sha256": "a" * 64, "size": 100}
    checks = []
    for phase in ("install", "build", "test"):
        detail: dict[str, Any] = {
            "status": "completed",
            "phase": "check_environment" if reused and phase == "install" else phase,
            "evidence": "native_process_only",
        }
        if phase == "install":
            detail["requires_dist_check"] = "completed"
        elif phase == "build":
            detail["wheel"] = wheel.copy()
        else:
            detail.update(
                test_count=5,
                **{
                    "import": {"module": "billing", "origin": "billing/__init__.py"},
                },
            )
        text = json.dumps(detail)
        checks.append(
            {
                "phase": phase,
                "status": "passed",
                "exit_code": 0,
                "timed_out": False,
                "stdout": text,
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
                "stdout_observed_bytes": len(text.encode()),
                "stderr_observed_bytes": 0,
            }
        )
    raw = {
        "schema_version": 2,
        "kind": "python_project",
        "evidence": "native_process_only",
        "status": "passed",
        "execution_id": invocation.execution_id,
        "source_sha256": inputs.original_reference.input_source_sha256,
        "recipe_sha256": inputs.sha256,
        "environment_input_sha256": inputs.environment_input_sha256,
        "environment_sha256": reference.environment_sha256,
        "original_check_contract": inputs.contract.protected_sha256,
        "runtime": inputs.interpreter.to_payload(),
        "checks": checks,
        "wheel": wheel,
        "test_count": 5,
        "exported_environment": not reused,
        "dependency_install": {
            "mode": "reused" if reused else "created",
            "origin_execution_id": reference.origin_execution_id,
        },
    }
    return invocation, inputs, reference, raw


def decode(api: Any, context: tuple[Any, Any, Any, dict[str, Any]], **changes: Any) -> Any:
    invocation, inputs, reference, raw = context
    arguments = {
        "invocation": invocation,
        "inputs": inputs,
        "expected_source_sha256": inputs.original_reference.input_source_sha256,
        "environment_reference": reference,
        "result": SkillResult(exit_code=0, timed_out=False, stdout=json.dumps(raw), stderr=""),
    } | changes
    return api.decode_python_project_report(**arguments)


def fail(raw: dict[str, Any], index: int, **changes: Any) -> None:
    raw["status"] = "failed"
    raw["checks"] = raw["checks"][: index + 1]
    raw["checks"][-1].update(status="failed", exit_code=1, stdout="", stdout_observed_bytes=0)
    raw["checks"][-1].update(changes)
    raw["test_count"] = None
    if index < 2:
        raw["wheel"] = None
    if index == 0:
        raw["environment_sha256"] = None
        raw["exported_environment"] = False


@pytest.mark.parametrize("reused", [False, True])
def test_passed_receipt_is_strict_frozen_and_contains_no_process_text(
    api: Any, reused: bool
) -> None:
    context = fixture(reused=reused)
    report = decode(api, context)
    assert type(report) is api.PythonProjectReport
    assert report.environment_reference == context[2]
    payload = report.to_payload()
    assert payload["schema_version"] == 2 and payload["status"] == "passed"
    assert "evidence" not in payload and "exported_environment" not in payload
    checks = payload["checks"]
    assert isinstance(checks, tuple)
    assert "stdout" not in checks[0]["execution_diagnostic"]
    with pytest.raises(TypeError):
        checks[0]["status"] = "failed"
    with pytest.raises(FrozenInstanceError):
        report.environment_reference = None


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("reused", [False, True])
def test_real_failed_prefix_retains_no_synthetic_checks_or_raw_text(
    api: Any,
    index: int,
    reused: bool,
) -> None:
    invocation, inputs, reference, raw = fixture(reused=reused)
    fail(raw, index, stderr="token=secret\n/home/private/error", stderr_observed_bytes=32)
    report = decode(
        api,
        (invocation, inputs, reference, raw),
        environment_reference=None if index == 0 else reference,
    )
    assert type(report) is api.PythonProjectFailure
    safe = report.to_payload()
    assert safe["kind"] == "python_project" and safe["schema_version"] == 2
    assert len(safe["checks"]) == index + 1
    assert safe["checks"][-1]["failure_reason"] == "process_failed"
    assert "secret" not in repr(report) and "/home/private" not in repr(report)
    assert safe["checks"][-1]["execution_diagnostic"]["stderr_metadata"]["redacted"] is True
    restored = api.validate_python_failure(
        safe,
        invocation=invocation,
        inputs=inputs,
        expected_source_sha256=inputs.original_reference.input_source_sha256,
        environment_reference=None if index == 0 else reference,
    )
    assert restored.to_payload() == safe


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 1),
        ("kind", "node_project"),
        ("evidence", "business_correctness"),
        ("execution_id", "other"),
        ("source_sha256", "b" * 64),
        ("recipe_sha256", "b" * 64),
        ("environment_input_sha256", "b" * 64),
        ("environment_sha256", "b" * 64),
        ("original_check_contract", "b" * 64),
        ("extra", "private"),
        ("test_count", True),
        ("test_count", 0),
        ("test_count", 6),
        ("exported_environment", False),
        ("exported_environment", 1),
    ],
)
def test_raw_cannot_choose_validator_or_rebind_expected_context(
    api: Any,
    field: str,
    value: Any,
) -> None:
    context = fixture()
    context[3][field] = value
    with pytest.raises(ProjectVerificationError, match="^python_project_report_invalid$"):
        decode(api, context)


@pytest.mark.parametrize(
    "mutation",
    [
        "runtime",
        "wheel_hash",
        "wheel_size_bool",
        "wheel_path",
        "order",
        "prefix",
        "detail_extra",
        "detail_wrong_import",
        "detail_false_count",
        "stdout_truncated",
        "fake_failure",
        "missing_check",
        "observation_count",
        "truncation_lie",
        "check_extra",
        "bool_exit",
        "timeout_passed",
        "no_exit",
        "origin",
        "dependency_extra",
    ],
)
def test_phase_process_and_payload_consistency(api: Any, mutation: str) -> None:
    context = fixture()
    raw = context[3]
    check = raw["checks"][2]
    if mutation == "runtime":
        raw["runtime"]["binary_sha256"] = "b" * 64
    elif mutation.startswith("wheel_"):
        raw["wheel"] = raw["wheel"].copy()
        key, value = {
            "wheel_hash": ("sha256", "b" * 64),
            "wheel_size_bool": ("size", True),
            "wheel_path": ("filename", "/host/a.whl"),
        }[mutation]
        raw["wheel"][key] = value
    elif mutation == "order":
        raw["checks"].reverse()
    elif mutation == "prefix":
        raw["checks"][0]["status"] = "failed"
    elif mutation.startswith("detail_"):
        detail = json.loads(check["stdout"])
        if mutation == "detail_extra":
            detail["command"] = "ignored"
        elif mutation == "detail_false_count":
            detail["test_count"] = True
        else:
            detail["import"]["origin"] = "/host/a.py"
        check["stdout"] = json.dumps(detail)
        check["stdout_observed_bytes"] = len(check["stdout"].encode())
    elif mutation == "stdout_truncated":
        check.update(stdout_truncated=True, stdout_observed_bytes=1000000)
    elif mutation == "fake_failure":
        raw["status"] = check["status"] = "failed"
    elif mutation == "missing_check":
        raw["checks"].pop()
    elif mutation == "observation_count":
        check["stdout_observed_bytes"] = 0
    elif mutation == "truncation_lie":
        check.update(stderr="text", stderr_truncated=True, stderr_observed_bytes=4)
    elif mutation == "check_extra":
        check["execution_diagnostic"] = {}
    elif mutation == "bool_exit":
        check["exit_code"] = False
    elif mutation == "timeout_passed":
        check["timed_out"] = True
    elif mutation == "no_exit":
        check["exit_code"] = None
    elif mutation == "origin":
        raw["dependency_install"]["origin_execution_id"] = "other"
    else:
        raw["dependency_install"]["extra"] = 1
    with pytest.raises(ProjectVerificationError, match="^python_project_report_invalid$"):
        decode(api, context)


@pytest.mark.parametrize(
    "change",
    [
        "scope",
        "recipe",
        "reference_scope",
        "reference_origin",
        "original_contract",
        "untrusted_type",
        "invalid_source",
    ],
)
def test_real_expected_objects_are_bound_independently(api: Any, change: str) -> None:
    invocation, inputs, reference, raw = fixture()
    kwargs: dict[str, Any] = {}
    if change == "scope":
        invocation = invocation.model_copy(update={"session_id": "different"})
    elif change == "recipe":
        invocation = invocation.model_copy(update={"recipe_sha256": "b" * 64})
    elif change == "reference_scope":
        reference = reference.model_copy(
            update={
                "scope": reference.scope.model_copy(update={"session_id": "different"}),
            }
        )
    elif change == "reference_origin":
        reference = reference.model_copy(update={"origin_execution_id": "other"})
    elif change == "original_contract":
        invocation = invocation.model_copy(
            update={
                "original_reference": invocation.original_reference.model_copy(
                    update={"input_source_sha256": "b" * 64}
                )
            }
        )
    elif change == "untrusted_type":
        kwargs["inputs"] = inputs.to_payload()
    else:
        kwargs["expected_source_sha256"] = "/host/private"
    with pytest.raises(ProjectVerificationError, match="^python_project_report_invalid$"):
        decode(api, (invocation, inputs, reference, raw), **kwargs)


def test_missing_root_reference_never_becomes_a_passed_receipt(api: Any) -> None:
    context = fixture()
    with pytest.raises(ProjectVerificationError):
        decode(api, context, environment_reference=None)
    fail(context[3], 2)
    report = decode(api, context, environment_reference=None)
    assert isinstance(report, api.PythonProjectFailure)
    assert report.environment_reference is None
    assert report.to_payload()["environment_sha256"] is None


def test_install_failure_cannot_issue_or_claim_environment(api: Any) -> None:
    context = fixture()
    fail(context[3], 0)
    with pytest.raises(ProjectVerificationError):
        decode(api, context)
    context[3]["environment_sha256"] = "e" * 64
    with pytest.raises(ProjectVerificationError):
        decode(api, context, environment_reference=None)


def test_stderr_truncation_does_not_relabel_success_as_failure(api: Any) -> None:
    context = fixture()
    check = context[3]["checks"][0]
    check.update(
        stderr="head\n[truncated]\ntail", stderr_truncated=True, stderr_observed_bytes=1000000
    )
    assert isinstance(decode(api, context), api.PythonProjectReport)


@pytest.mark.parametrize("reason", ["phase_output_invalid", "stdout_truncated", "timed_out"])
def test_exit_zero_output_failure_is_typed_without_faking_a_process_exit(
    api: Any, reason: str
) -> None:
    context = fixture()
    fail(context[3], 1, exit_code=0)
    check = context[3]["checks"][-1]
    if reason == "stdout_truncated":
        check.update(stdout_truncated=True, stdout_observed_bytes=1000000)
    elif reason == "timed_out":
        check.update(exit_code=None, timed_out=True)
    report = decode(api, context)
    last = report.to_payload()["checks"][-1]
    assert last["failure_reason"] == reason
    assert last["execution_diagnostic"]["exit_code"] == check["exit_code"]


@pytest.mark.parametrize(
    "text",
    [
        '{"schema_version":2,"schema_version":2}',
        '{"extra":NaN}',
        "[" * 1000 + "0" + "]" * 1000,
        '{"path":"/host/private"',
        '"' + "x" * (2 * 1024**2) + '"',
        "null",
        "\ud800",
    ],
    ids=["duplicate", "nonfinite", "deep", "malformed", "oversize", "null", "surrogate"],
)
def test_bounded_json_rejects_duplicate_nonfinite_deep_and_invalid_input(
    api: Any, text: str
) -> None:
    with pytest.raises(ProjectVerificationError, match="^python_project_report_invalid$") as error:
        decode(
            api, fixture(), result=SkillResult(exit_code=0, timed_out=False, stdout=text, stderr="")
        )
    assert "/host/private" not in str(error.value)
    assert error.value.__cause__ is None


def test_nested_duplicate_key_is_not_last_value_wins(api: Any) -> None:
    context = fixture()
    text = json.dumps(context[3]).replace('"size": 100', '"size": 1, "size": 100', 1)
    with pytest.raises(ProjectVerificationError):
        decode(
            api, context, result=SkillResult(exit_code=0, timed_out=False, stdout=text, stderr="")
        )
    check = context[3]["checks"][0]
    check["stdout"] = check["stdout"].replace('"completed"', '"bad", "status": "completed"', 1)
    check["stdout_observed_bytes"] = len(check["stdout"].encode())
    with pytest.raises(ProjectVerificationError):
        decode(api, context)


@pytest.mark.parametrize("exit_code,timed_out", [(1, False), (None, True), (0, True), (-9, False)])
def test_outer_runner_must_naturally_exit_zero(
    api: Any, exit_code: int | None, timed_out: bool
) -> None:
    context = fixture()
    with pytest.raises(ProjectVerificationError):
        decode(
            api,
            context,
            result=SkillResult(
                exit_code=exit_code,
                timed_out=timed_out,
                stdout=json.dumps(context[3]),
                stderr="/host/private",
            ),
        )


def test_persisted_failure_accepts_only_safe_schema_not_raw_native_body(api: Any) -> None:
    invocation, inputs, reference, raw = fixture()
    fail(raw, 2)
    expected = {
        "invocation": invocation,
        "inputs": inputs,
        "environment_reference": reference,
        "expected_source_sha256": inputs.original_reference.input_source_sha256,
    }
    with pytest.raises(ProjectVerificationError):
        api.validate_python_failure(raw, **expected)
    safe = decode(api, (invocation, inputs, reference, raw)).to_payload()
    assert isinstance(safe, Mapping)
    mutable = json.loads(json.dumps(safe, default=lambda value: dict(value)))
    mutable["checks"][-1]["execution_diagnostic"]["stderr"] = "private"
    with pytest.raises(ProjectVerificationError):
        api.validate_python_failure(mutable, **expected)


@pytest.mark.parametrize(
    "mutation",
    [
        "fake_passed",
        "fake_failed",
        "metadata_extra",
        "metadata_hash",
        "metadata_redaction",
        "metadata_count",
        "synthetic_check",
        "wrong_kind",
        "wrong_source",
        "wrong_execution",
        "missing_environment",
        "invent_environment",
        "wrong_reason",
        "wrong_runtime",
    ],
)
def test_persisted_failure_validates_safe_shape_and_real_expected_context(
    api: Any,
    mutation: str,
) -> None:
    invocation, inputs, reference, raw = fixture()
    fail(raw, 2)
    safe = decode(api, (invocation, inputs, reference, raw)).to_payload()
    mutable = json.loads(json.dumps(safe, default=lambda value: dict(value)))
    diagnostic = mutable["checks"][-1]["execution_diagnostic"]
    if mutation == "fake_passed":
        mutable["checks"][0]["execution_diagnostic"]["exit_code"] = 1
    elif mutation == "fake_failed":
        diagnostic["exit_code"] = 0
    elif mutation == "metadata_extra":
        diagnostic["stdout_metadata"]["raw"] = "secret"
    elif mutation == "metadata_hash":
        diagnostic["stdout_metadata"]["sha256"] = "a" * 64
    elif mutation == "metadata_redaction":
        diagnostic["stdout_metadata"]["redacted"] = True
    elif mutation == "metadata_count":
        diagnostic["stdout_metadata"]["returned_bytes"] = True
    elif mutation == "synthetic_check":
        mutable["checks"][-1] = {"phase": "test", "status": "not_run"}
    elif mutation == "wrong_kind":
        mutable["kind"] = "node_project"
    elif mutation == "wrong_source":
        mutable["source_sha256"] = "a" * 64
    elif mutation == "wrong_execution":
        mutable["execution_id"] = "other"
        mutable["dependency_install"]["mode"] = "reused"
    elif mutation == "missing_environment":
        mutable["environment_sha256"] = None
    elif mutation == "invent_environment":
        reference = None
    elif mutation == "wrong_runtime":
        mutable["runtime"]["version"] = "3.12.11"
    else:
        mutable["checks"][-1]["failure_reason"] = "not_run"
    with pytest.raises(ProjectVerificationError):
        api.validate_python_failure(
            mutable,
            invocation=invocation,
            inputs=inputs,
            environment_reference=reference,
            expected_source_sha256=inputs.original_reference.input_source_sha256,
        )


def test_candidate_uses_independent_source_not_original_or_raw_expected(api: Any) -> None:
    invocation, inputs, reference, raw = fixture(reused=True)
    invocation = invocation.model_copy(update={"source_kind": "candidate"})
    raw["source_sha256"] = "c" * 64
    report = decode(api, (invocation, inputs, reference, raw), expected_source_sha256="c" * 64)
    assert report.to_payload()["source_sha256"] == "c" * 64
    with pytest.raises(ProjectVerificationError):
        decode(api, (invocation, inputs, reference, raw), expected_source_sha256="d" * 64)


@pytest.mark.parametrize("target", ["invocation", "reference", "result"])
def test_pydantic_construct_and_copy_cannot_skip_strict_validation(api: Any, target: str) -> None:
    invocation, inputs, reference, raw = fixture()
    kwargs: dict[str, Any] = {}
    if target == "invocation":
        invocation = invocation.model_copy(update={"timeout_seconds": True})
    elif target == "reference":
        reference = reference.model_copy(
            update={
                "scope": reference.scope.model_copy(
                    update={
                        "original_reference": reference.scope.original_reference.model_copy(
                            update={"file_count": True}
                        )
                    }
                )
            }
        )
    else:
        kwargs["result"] = SkillResult.model_construct(
            exit_code=False,
            timed_out=False,
            stdout=json.dumps(raw),
            stderr="",
        )
    with pytest.raises(ProjectVerificationError):
        decode(api, (invocation, inputs, reference, raw), **kwargs)


def test_safe_data_is_detached_from_mutable_input_and_contains_metadata_only(api: Any) -> None:
    context = fixture()
    fail(context[3], 2)
    check = context[3]["checks"][-1]
    check.update(stderr="x" * 5000, stderr_observed_bytes=5000)
    report = decode(api, context)
    check["stderr"] = "changed"
    metadata = report.to_payload()["checks"][-1]["execution_diagnostic"]["stderr_metadata"]
    assert metadata["sanitized_bytes"] == 5000 and metadata["truncated"] is True
    with pytest.raises(TypeError):
        metadata["sha256"] = "a" * 64
    with pytest.raises(FrozenInstanceError):
        report.environment_reference = None


def test_original_test_failure_retains_wheel_and_reference_for_same_environment_repair(
    api: Any,
) -> None:
    # In-memory schema fixture only; the native canary was run independently.
    semantic = "841419cc66f942d8dd9c6aecf9ff81909975b130961b060c7b6a113aa90a9b05"
    invocation, inputs, reference, raw = fixture()
    reference = reference.model_copy(update={"environment_sha256": semantic})
    raw["environment_sha256"] = semantic
    fail(raw, 2)
    failed = decode(api, (invocation, inputs, reference, raw))
    assert type(failed) is api.PythonProjectFailure
    assert not isinstance(failed, ProjectVerificationFailure)
    failure = failed.to_payload()
    assert failure["test_count"] is None
    assert failure["wheel"] == raw["wheel"] and failure["wheel"] is not None
    assert failure["environment_sha256"] == semantic
    assert failed.environment_reference == reference
    assert raw["exported_environment"] is True
    assert [check["status"] for check in failure["checks"]] == ["passed", "passed", "failed"]

    repair_invocation, repair_inputs, repair_reference, repair_raw = fixture(reused=True)
    repair_invocation = repair_invocation.model_copy(
        update={"execution_id": "python_repair", "source_kind": "candidate"}
    )
    repair_reference = repair_reference.model_copy(
        update={
            "environment_sha256": semantic,
            "security_manifest_sha256": "9" * 64,
            "origin_execution_id": invocation.execution_id,
        }
    )
    repair_raw.update(
        execution_id=repair_invocation.execution_id,
        source_sha256="c" * 64,
        environment_sha256=semantic,
    )
    repair_raw["dependency_install"]["origin_execution_id"] = invocation.execution_id
    assert repair_raw["checks"][0]["phase"] == "install"
    assert json.loads(repair_raw["checks"][0]["stdout"])["phase"] == "check_environment"
    assert repair_raw["exported_environment"] is False
    passed = decode(
        api,
        (repair_invocation, repair_inputs, repair_reference, repair_raw),
        expected_source_sha256="c" * 64,
    )
    assert type(passed) is api.PythonProjectReport
    assert not isinstance(passed, ProjectVerificationFailure)
    receipt = passed.to_payload()
    assert receipt["test_count"] == 5
    assert receipt["environment_sha256"] == failure["environment_sha256"]
    assert receipt["dependency_install"] == {
        "mode": "reused",
        "origin_execution_id": invocation.execution_id,
    }
    assert passed.environment_reference == repair_reference
    assert passed.environment_reference.security_manifest_sha256 != (
        failed.environment_reference.security_manifest_sha256
    )


def test_semantic_binding_does_not_compare_security_snapshot_digest(api: Any) -> None:
    invocation, inputs, reference, raw = fixture()
    first = decode(api, (invocation, inputs, reference, raw))
    other_snapshot = reference.model_copy(update={"security_manifest_sha256": "9" * 64})
    second = decode(api, (invocation, inputs, other_snapshot, raw))
    assert first.to_payload() == second.to_payload()
    assert first.environment_reference != second.environment_reference
    assert second.environment_reference.security_manifest_sha256 != raw["environment_sha256"]
    # Security paths/identities belong in root evidence, not extra native raw fields.
    raw["security_manifest"] = {"root": "/private/other-snapshot"}
    with pytest.raises(ProjectVerificationError, match="^python_project_report_invalid$"):
        decode(api, (invocation, inputs, other_snapshot, raw))


@pytest.mark.parametrize("output_limit", [1, 1024**2])
@pytest.mark.parametrize(
    "pattern",
    ["\x00", "\\", "\u4e2d", "\x00\\\u4e2d\U0001f600"],
    ids=["nul", "backslash", "nonascii", "mixed"],
)
@pytest.mark.parametrize("truncated", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_shared_json_budget_accepts_maximum_bounded_streams_without_false_failure(
    api: Any,
    output_limit: int,
    pattern: str,
    truncated: bool,
    failed: bool,
) -> None:
    invocation, inputs, reference, raw = fixture()
    invocation = invocation.model_copy(update={"output_limit_bytes": output_limit})
    cap = python_phase_capture_limit(output_limit)
    unit = pattern.encode("utf-8")
    data = unit * (cap // len(unit)) + b"x" * (cap % len(unit))
    stderr = _Capture(cap)
    stderr.feed(data * (2 if truncated else 1))
    if failed:
        fail(raw, 2)
    for check in raw["checks"]:
        if check["status"] == "passed":
            # All retained stdout bytes remain valid fixed-phase JSON.
            stdout = check["stdout"] + "\t" * (cap - len(check["stdout"].encode()))
            check.update(stdout=stdout, stdout_observed_bytes=cap)
        else:
            check.update(
                stdout=stderr.text(),
                stdout_observed_bytes=stderr.observed,
                stdout_truncated=stderr.truncated,
            )
        check.update(
            stderr=stderr.text(),
            stderr_observed_bytes=stderr.observed,
            stderr_truncated=stderr.truncated,
        )
    text = json.dumps(raw, ensure_ascii=True, separators=(",", ":"))
    assert len(text.encode()) <= python_native_report_limit(output_limit) < 2 * 1024**2
    if output_limit == 1:
        assert len(text.encode()) > output_limit
    observed = decode(
        api,
        (invocation, inputs, reference, raw),
        result=SkillResult(
            exit_code=0,
            timed_out=False,
            stdout=text,
            stderr="",
        ),
    )
    assert type(observed) is (api.PythonProjectFailure if failed else api.PythonProjectReport)
    assert observed.to_payload()["checks"][-1]["status"] == ("failed" if failed else "passed")
    metadata = observed.to_payload()["checks"][0]["execution_diagnostic"]["stderr_metadata"]
    if truncated:
        # The digest/size describe the trusted replacement, not unreadable raw logs.
        assert metadata["sanitized_bytes"] == len(PYTHON_PHASE_OUTPUT_WITHHELD.encode())
        assert metadata["truncated"] is False
    elif output_limit == 1024**2:
        assert metadata["truncated"] is True


@pytest.mark.parametrize("output_limit", [1, 1024**2])
@pytest.mark.parametrize(
    "mutation", ["over_capture", "observed_lie", "truncation_lie", "over_report"]
)
def test_shared_budgets_still_reject_oversize_and_inconsistent_capture_metadata(
    api: Any,
    output_limit: int,
    mutation: str,
) -> None:
    invocation, inputs, reference, raw = fixture()
    invocation = invocation.model_copy(update={"output_limit_bytes": output_limit})
    cap = python_phase_capture_limit(output_limit)
    check = raw["checks"][0]
    if mutation == "over_capture":
        check.update(stderr="x" * (cap + 1), stderr_observed_bytes=cap + 1, stderr_truncated=True)
    elif mutation == "observed_lie":
        check.update(stderr="x", stderr_observed_bytes=cap + 1, stderr_truncated=False)
    elif mutation == "truncation_lie":
        check.update(stderr="x", stderr_observed_bytes=cap, stderr_truncated=True)
    text = json.dumps(raw)
    if mutation == "over_report":
        text += " " * (python_native_report_limit(output_limit) - len(text.encode()) + 1)
    with pytest.raises(ProjectVerificationError, match="^python_project_report_invalid$"):
        decode(
            api,
            (invocation, inputs, reference, raw),
            result=SkillResult(
                exit_code=0,
                timed_out=False,
                stdout=text,
                stderr="",
            ),
        )


@pytest.mark.parametrize("index", [0, 1, 2])
def test_native_failure_preserves_only_actual_failed_phase_diagnostic_preview(
    api: Any,
    index: int,
) -> None:
    invocation, inputs, reference, raw = fixture()
    stderr = "token=secret\nAssertionError: expected 12, got 9\n"
    fail(raw, index, stderr=stderr, stderr_observed_bytes=len(stderr.encode()))
    if index == 0:
        reference = None
    observed = decode(api, (invocation, inputs, reference, raw))
    assert type(observed.diagnostic) is ExecutionDiagnostic
    preview = observed.diagnostic.to_payload()
    assert preview["exit_code"] == 1 and preview["timed_out"] is False
    assert preview["stderr"] == "AssertionError: expected 12, got 9\n"
    assert "secret" not in str(preview)
    metadata = {key: value for key, value in preview.items() if key not in {"stdout", "stderr"}}
    safe = observed.to_payload()
    assert metadata == safe["checks"][-1]["execution_diagnostic"]
    assert "diagnostic" not in safe and "AssertionError" not in repr(safe)
    assert "AssertionError" not in repr(observed)
    restored = api.validate_python_failure(
        safe,
        invocation=invocation,
        inputs=inputs,
        environment_reference=reference,
        expected_source_sha256=inputs.original_reference.input_source_sha256,
    )
    assert restored.diagnostic is None
    assert api.PythonProjectFailure(safe, reference).diagnostic is None


@pytest.mark.parametrize("mutation", ["exit", "stderr", "backend", "skill_result", "duck_result"])
def test_failure_diagnostic_is_exact_type_and_bound_to_safe_failed_metadata(
    api: Any,
    mutation: str,
) -> None:
    context = fixture()
    fail(context[3], 2)
    observed = decode(api, context)
    assert observed.diagnostic is not None
    result = SkillResult(exit_code=1, timed_out=False, stdout="", stderr="")
    if mutation == "skill_result":
        diagnostic: Any = result
    elif mutation == "duck_result":
        diagnostic = SimpleNamespace(to_payload=observed.diagnostic.to_payload)
    else:
        result = result.model_copy(
            update={"exit_code": 2}
            if mutation == "exit"
            else {"stderr": "different"}
            if mutation == "stderr"
            else {}
        )
        diagnostic = ExecutionDiagnostic("unknown" if mutation == "backend" else "systemd", result)
    with pytest.raises(ValueError, match="^python_project_report_invalid$"):
        replace(observed, diagnostic=diagnostic)
    assert replace(observed, diagnostic=observed.diagnostic).to_payload() == observed.to_payload()


def test_safe_history_cannot_smuggle_or_recreate_preview_diagnostic(api: Any) -> None:
    invocation, inputs, reference, raw = fixture()
    fail(raw, 2)
    safe = dict(decode(api, (invocation, inputs, reference, raw)).to_payload())
    safe["diagnostic"] = {"backend": "systemd", "stderr": "pretend execution"}
    with pytest.raises(ProjectVerificationError, match="^python_project_report_invalid$"):
        api.validate_python_failure(
            safe,
            invocation=invocation,
            inputs=inputs,
            environment_reference=reference,
            expected_source_sha256=inputs.original_reference.input_source_sha256,
        )
