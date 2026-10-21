"""Explicit Python raw-schema-2 decoding, without execution or ZIP authority.

Only the decoder observes native output. Callers supply independently measured
source/inputs and an authenticated root reference already checked by the client
against its actual request. Neither a matching DTO nor persisted safe metadata
proves provenance, live cache availability, business correctness, or authority.

Diagnostic sizes and digests describe the sanitized disclosed preview. When a
native stream overflowed, its preview is a trusted context-loss replacement,
not a prefix/tail or a hash of the complete native stream. Exit/timeout/phase
evidence survives, but an ActualError beyond the capture budget is not exposed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field

from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.capabilities.project_verification import ProjectVerificationError
from agent_hub.capabilities.python_project_inputs import (
    PythonInterpreterIdentity,
    PythonProjectInputs,
)
from agent_hub.capabilities.python_project_receipts import _metadata, validate_python_passed_receipt
from agent_hub.runtime.contracts import JsonValue, _freeze_object
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.python_project_phase import (
    validate_generated_python_test_execution as validate_generated_python_test_execution,  # noqa: PLC0414 - public re-export.
)
from agent_hub.skills.sandbox.python_project_process import (
    PYTHON_PHASE_OUTPUT_WITHHELD,
    python_native_report_limit,
    python_phase_capture_limit,
)
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonProjectInvocation,
    _reference_matches,
)

_ERROR = "python_project_report_invalid"
_MAX_INT = 2**63 - 1
_SHA = re.compile(r"[a-f0-9]{64}")
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}")
_WHEEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]*\.whl")
_PHASES = ("install", "build", "test")
_PUBLIC_FIELDS = {
    "schema_version",
    "kind",
    "status",
    "execution_id",
    "source_sha256",
    "recipe_sha256",
    "environment_input_sha256",
    "environment_sha256",
    "checks",
    "runtime",
    "wheel",
    "test_count",
    "dependency_install",
}
_RAW_FIELDS = _PUBLIC_FIELDS | {"evidence", "original_check_contract", "exported_environment"}
_CHECK_FIELDS = {
    "phase",
    "status",
    "exit_code",
    "timed_out",
    "stdout",
    "stderr",
    "stdout_truncated",
    "stderr_truncated",
    "stdout_observed_bytes",
    "stderr_observed_bytes",
}
_DIAGNOSTIC_FIELDS = {
    "schema_version",
    "backend",
    "exit_code",
    "timed_out",
    "stdout_metadata",
    "stderr_metadata",
}
_INVALID = (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError)


def _need(condition: bool) -> None:
    if not condition:
        raise ValueError(_ERROR)


def _object(value: object, fields: set[str]) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(_ERROR)
    return value


def _hash(value: object) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError(_ERROR)
    return value


def _identifier(value: object) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ValueError(_ERROR)
    return value


def _integer(value: object, *, minimum: int = 0, maximum: int = _MAX_INT) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(_ERROR)
    return value


def _text(value: object, limit: int) -> str:
    if type(value) is not str or len(value) > limit or len(value.encode("utf-8")) > limit:
        raise ValueError(_ERROR)
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _need(key not in result)
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ValueError(_ERROR)


def _load(value: object, limit: int) -> Mapping[str, JsonValue]:
    text = _text(value, limit)
    parsed: object = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    frozen = _freeze_object(parsed, name="Python report")
    return frozen


def _wheel(value: object) -> Mapping[str, JsonValue]:
    wheel = _object(value, {"filename", "sha256", "size"})
    name = _text(wheel["filename"], 255)
    _need(_WHEEL.fullmatch(name) is not None)
    _hash(wheel["sha256"])
    _integer(wheel["size"], minimum=1)
    return wheel


def _runtime(value: object) -> None:
    runtime = _object(
        value,
        {
            "implementation",
            "version",
            "abi",
            "platform",
            "binary_sha256",
            "base_runtime_sha256",
        },
    )
    PythonInterpreterIdentity.from_payload(dict(runtime))


def _process(value: Mapping[str, JsonValue]) -> None:
    _need(type(value["timed_out"]) is bool)
    if value["exit_code"] is None:
        _need(value["timed_out"] is True)
    else:
        _integer(value["exit_code"], minimum=-(2**31), maximum=2**31 - 1)


def _diagnostic(value: object) -> Mapping[str, JsonValue]:
    diagnostic = _object(value, _DIAGNOSTIC_FIELDS)
    _need(type(diagnostic["schema_version"]) is int and diagnostic["schema_version"] == 1)
    _need(diagnostic["backend"] == "systemd")
    _process(diagnostic)
    _metadata(diagnostic["stdout_metadata"])
    _metadata(diagnostic["stderr_metadata"])
    return diagnostic


def _identity(report: Mapping[str, JsonValue], *, status: str) -> None:
    _need(type(report["schema_version"]) is int and report["schema_version"] == 2)
    _need(report["kind"] == "python_project" and report["status"] == status)
    execution = _identifier(report["execution_id"])
    for name in ("source_sha256", "recipe_sha256", "environment_input_sha256"):
        _hash(report[name])
    if report["environment_sha256"] is not None:
        _hash(report["environment_sha256"])
    _runtime(report["runtime"])
    dependency = _object(report["dependency_install"], {"mode", "origin_execution_id"})
    _need(dependency["mode"] in ("created", "reused"))
    origin = _identifier(dependency["origin_execution_id"])
    _need((origin == execution) == (dependency["mode"] == "created"))


def _failure_shape(value: object) -> Mapping[str, JsonValue]:
    report = _object(
        _freeze_object(value, name="Python failure"), _PUBLIC_FIELDS | {"original_check_contract"}
    )
    _identity(report, status="failed")
    _hash(report["original_check_contract"])
    checks = report["checks"]
    if not isinstance(checks, tuple) or not 1 <= len(checks) <= 3:
        raise ValueError(_ERROR)
    for index, item in enumerate(checks):
        last = index == len(checks) - 1
        check = _object(
            item,
            {"phase", "status", "execution_diagnostic"} | ({"failure_reason"} if last else set()),
        )
        _need(check["phase"] == _PHASES[index])
        _need(check["status"] == ("failed" if last else "passed"))
        diagnostic = _diagnostic(check["execution_diagnostic"])
        if not last:
            _need(diagnostic["exit_code"] == 0 and diagnostic["timed_out"] is False)
            continue
        reason = check["failure_reason"]
        if reason == "timed_out":
            _need(diagnostic["timed_out"] is True)
        elif reason == "process_failed":
            _need(diagnostic["timed_out"] is False and diagnostic["exit_code"] not in (None, 0))
        else:
            _need(reason in ("stdout_truncated", "phase_output_invalid"))
            _need(diagnostic["exit_code"] == 0 and diagnostic["timed_out"] is False)
    if len(checks) == 1:
        _need(report["environment_sha256"] is None)
    if len(checks) == 3:
        _wheel(report["wheel"])
    else:
        _need(report["wheel"] is None)
    _need(report["test_count"] is None)
    return report


@dataclass(frozen=True, slots=True)
class PythonProjectReport:
    """Safe passed data; constructing this DTO is not a native observation."""

    report: Mapping[str, JsonValue]
    environment_reference: PythonEnvironmentReference
    expected_source_sha256: InitVar[str]

    def __post_init__(self, expected_source_sha256: str) -> None:
        value = _object(_freeze_object(self.report, name="Python passed data"), _PUBLIC_FIELDS)
        reference = _reference(self.environment_reference)
        if reference is None:
            raise ValueError(_ERROR)
        report = validate_python_passed_receipt(
            value,
            expected_source=expected_source_sha256,
            expected_recipe=reference.recipe_sha256,
            expected_environment_inputs=reference.environment_input_sha256,
            expected_environment=reference.environment_sha256,
        )
        dependency = _object(report["dependency_install"], {"mode", "origin_execution_id"})
        _need(dependency["origin_execution_id"] == reference.origin_execution_id)
        object.__setattr__(self, "report", report)
        object.__setattr__(self, "environment_reference", reference)

    def to_payload(self) -> Mapping[str, JsonValue]:
        return self.report


@dataclass(frozen=True, slots=True)
class PythonProjectFailure:
    """Safe Python failure with an optional, nonpersisted native phase preview.

    Binding preview metadata prevents accidental substitution, not forgery by
    privileged Python callers. Only the native decoder observes raw output;
    persisted safe data cannot establish execution provenance or restore logs.
    """

    report: Mapping[str, JsonValue]
    environment_reference: PythonEnvironmentReference | None
    diagnostic: ExecutionDiagnostic | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        value = _failure_shape(self.report)
        reference = _reference(self.environment_reference)
        _bind_reference(value, reference)
        if self.diagnostic is not None:
            _need(type(self.diagnostic) is ExecutionDiagnostic)
            checks = value["checks"]
            assert isinstance(checks, tuple)
            last = _object(
                checks[-1],
                {
                    "phase",
                    "status",
                    "execution_diagnostic",
                    "failure_reason",
                },
            )
            _need(_diagnostic_metadata(self.diagnostic) == last["execution_diagnostic"])
        object.__setattr__(self, "report", value)
        object.__setattr__(self, "environment_reference", reference)

    def to_payload(self) -> Mapping[str, JsonValue]:
        return self.report


def _reference(value: PythonEnvironmentReference | None) -> PythonEnvironmentReference | None:
    if value is None:
        return None
    _need(type(value) is PythonEnvironmentReference)
    return PythonEnvironmentReference.model_validate(
        value.model_dump(warnings="error"), strict=True
    )


def _context(
    invocation: PythonProjectInvocation,
    inputs: PythonProjectInputs,
    expected_source_sha256: str,
    environment_reference: PythonEnvironmentReference | None,
) -> tuple[PythonProjectInvocation, PythonProjectInputs, PythonEnvironmentReference | None]:
    _need(type(invocation) is PythonProjectInvocation and type(inputs) is PythonProjectInputs)
    # Dump nested models to dictionaries, retaining strict Python primitives:
    # validating an existing Pydantic instance may trust constructed data.
    invocation = PythonProjectInvocation.model_validate(
        invocation.model_dump(warnings="error"),
        strict=True,
    )
    inputs = PythonProjectInputs.from_payload(inputs.to_payload())
    _hash(expected_source_sha256)
    contract = inputs.contract
    _need(
        (invocation.tenant_id, invocation.run_id, invocation.project_id, invocation.session_id)
        == (contract.tenant_id, contract.run_id, contract.project_id, contract.session_id)
    )
    _need(invocation.original_reference.model_dump() == inputs.original_reference.to_payload())
    _need(
        invocation.recipe_sha256 == inputs.sha256
        and invocation.environment_input_sha256 == inputs.environment_input_sha256
    )
    if invocation.source_kind == "original":
        _need(expected_source_sha256 == inputs.original_reference.input_source_sha256)
    reference = _reference(environment_reference)
    if reference is not None:
        _need(_reference_matches(invocation, reference))
    return invocation, inputs, reference


def _bind_expected(
    report: Mapping[str, JsonValue],
    invocation: PythonProjectInvocation,
    inputs: PythonProjectInputs,
    source: str,
) -> None:
    _need(report["execution_id"] == invocation.execution_id)
    _need(report["source_sha256"] == source and report["recipe_sha256"] == inputs.sha256)
    _need(report["environment_input_sha256"] == inputs.environment_input_sha256)
    _need(report["runtime"] == inputs.interpreter.to_payload())
    _need(report["original_check_contract"] == inputs.contract.protected_sha256)
    dependency = _object(report["dependency_install"], {"mode", "origin_execution_id"})
    if dependency["mode"] == "created":
        _need(invocation.install_allowed)


def _bind_reference(
    report: Mapping[str, JsonValue],
    reference: PythonEnvironmentReference | None,
) -> None:
    if reference is None:
        _need(report["environment_sha256"] is None)
    else:
        _need(report["environment_sha256"] == reference.environment_sha256)
        _need(report["recipe_sha256"] == reference.recipe_sha256)
        _need(report["environment_input_sha256"] == reference.environment_input_sha256)
        dependency = _object(report["dependency_install"], {"mode", "origin_execution_id"})
        _need(dependency["origin_execution_id"] == reference.origin_execution_id)


def _phase_detail(
    stdout: JsonValue,
    *,
    phase: str,
    import_module: str,
    limit: int,
) -> Mapping[str, JsonValue] | None:
    try:
        detail = _load(stdout, limit)
        base = {"status", "phase", "evidence"}
        _need(
            detail.get("status") == "completed"
            and detail.get("phase") == phase
            and detail.get("evidence") == "native_process_only"
        )
        if phase in ("install", "check_environment"):
            _object(detail, base | {"requires_dist_check"})
            _need(detail["requires_dist_check"] == "completed")
        elif phase == "build":
            _object(detail, base | {"wheel"})
            _wheel(detail["wheel"])
        else:
            extra = {"generated_test_execution"} if "generated_test_execution" in detail else set()
            _object(detail, base | {"test_count", "import"} | extra)
            _integer(detail["test_count"], minimum=1)
            _need(
                detail["import"]
                == {
                    "module": import_module,
                    "origin": import_module.replace(".", "/") + "/__init__.py",
                }
            )
            if extra:
                proof = validate_generated_python_test_execution(detail["generated_test_execution"])
                _need(proof["count"] == detail["test_count"])
        return detail
    except _INVALID:
        return None


def _check(
    value: object,
    *,
    phase: str,
    actual_phase: str,
    import_module: str,
    limit: int,
) -> tuple[dict[str, JsonValue], Mapping[str, JsonValue] | None, ExecutionDiagnostic]:
    check = _object(value, _CHECK_FIELDS)
    _need(check["phase"] == phase and check["status"] in ("passed", "failed"))
    _process(check)
    texts: dict[str, str] = {}
    for stream in ("stdout", "stderr"):
        text = _text(check[stream], limit)
        observed = _integer(check[f"{stream}_observed_bytes"])
        truncated = check[f"{stream}_truncated"]
        _need(type(truncated) is bool and len(text.encode("utf-8")) <= observed)
        _need(observed > limit if truncated else observed <= limit)
        # A legacy head/tail capture may have discarded a sensitive label or
        # multiline opener. No remaining fragment can establish safe context.
        texts[stream] = PYTHON_PHASE_OUTPUT_WITHHELD[:limit] if truncated else text
    reason = None
    detail = None
    if check["timed_out"]:
        reason = "timed_out"
    elif check["exit_code"] != 0:
        reason = "process_failed"
    elif check["stdout_truncated"]:
        reason = "stdout_truncated"
    else:
        detail = _phase_detail(
            check["stdout"], phase=actual_phase, import_module=import_module, limit=limit
        )
        if detail is None:
            reason = "phase_output_invalid"
    _need(check["status"] == ("passed" if reason is None else "failed"))
    result = SkillResult.model_validate(
        {
            "exit_code": check["exit_code"],
            "timed_out": check["timed_out"],
            **texts,
        },
        strict=True,
    )
    # Metadata hashes the disclosed sanitized preview/replacement, NOT the full
    # native stream. Raw observed-byte counts and overflow flags are separate.
    diagnostic = ExecutionDiagnostic("systemd", result, retain_tail=True)
    safe: dict[str, JsonValue] = {
        "phase": phase,
        "status": check["status"],
        "execution_diagnostic": _diagnostic_metadata(diagnostic),
    }
    if reason is not None:
        safe["failure_reason"] = reason
    return safe, detail, diagnostic


def _diagnostic_metadata(diagnostic: ExecutionDiagnostic) -> Mapping[str, JsonValue]:
    payload = diagnostic.to_payload()
    return _diagnostic({name: payload[name] for name in _DIAGNOSTIC_FIELDS})


def decode_python_project_report(
    *,
    invocation: PythonProjectInvocation,
    inputs: PythonProjectInputs,
    expected_source_sha256: str,
    environment_reference: PythonEnvironmentReference | None,
    result: SkillResult,
) -> PythonProjectReport | PythonProjectFailure:
    """Observe explicitly selected Python raw2 against independent trusted context.

    The outer runner must exit naturally with zero; individual phase failures
    remain typed data. A failed report without a root reference discards the raw
    environment hash. No returned object grants cache reuse or ZIP readiness.
    """
    try:
        invocation, inputs, reference = _context(
            invocation,
            inputs,
            expected_source_sha256,
            environment_reference,
        )
        _need(type(result) is SkillResult)
        result = SkillResult.model_validate(result.model_dump(), strict=True)
        _need(type(result.exit_code) is int and result.exit_code == 0 and not result.timed_out)
        report_limit = python_native_report_limit(invocation.output_limit_bytes)
        capture_limit = python_phase_capture_limit(invocation.output_limit_bytes)
        _text(result.stderr, report_limit)
        loaded = _load(result.stdout, report_limit)
        extra = {"generated_test_execution"} if "generated_test_execution" in loaded else set()
        raw = _object(loaded, _RAW_FIELDS | extra)
        _need(raw["evidence"] == "native_process_only" and raw["status"] in ("passed", "failed"))
        status = "passed" if raw["status"] == "passed" else "failed"
        _identity(raw, status=status)
        _bind_expected(raw, invocation, inputs, expected_source_sha256)
        dependency = _object(raw["dependency_install"], {"mode", "origin_execution_id"})
        checks = raw["checks"]
        if not isinstance(checks, tuple) or not 1 <= len(checks) <= 3:
            raise ValueError(_ERROR)
        safe_checks: list[JsonValue] = []
        wheel: JsonValue = None
        test_count: JsonValue = None
        generated_test_execution: Mapping[str, object] | None = None
        failure_diagnostic = None
        for index, item in enumerate(checks):
            phase = _PHASES[index]
            actual = "check_environment" if index == 0 and dependency["mode"] == "reused" else phase
            safe, detail, diagnostic = _check(
                item,
                phase=phase,
                actual_phase=actual,
                import_module=inputs.declaration.import_module,
                limit=capture_limit,
            )
            last_failed = status == "failed" and index == len(checks) - 1
            _need(safe["status"] == ("failed" if last_failed else "passed"))
            if last_failed:
                failure_diagnostic = diagnostic
            safe_checks.append(safe)
            if detail is not None and phase == "build":
                wheel = detail["wheel"]
            if detail is not None and phase == "test":
                test_count = detail["test_count"]
                if "generated_test_execution" in detail:
                    generated_test_execution = validate_generated_python_test_execution(detail["generated_test_execution"])
        _need(status != "passed" or len(checks) == 3)
        _need(raw["wheel"] == wheel and raw["test_count"] == test_count)
        _need(bool(extra) == (generated_test_execution is not None))
        if extra:
            _need(status == "passed" and validate_generated_python_test_execution(
                raw["generated_test_execution"]) == generated_test_execution)
        # Equality alone would accept bools as integers in untrusted JSON.
        if raw["wheel"] is not None:
            _wheel(raw["wheel"])
        if raw["test_count"] is not None:
            _integer(raw["test_count"], minimum=1)
        dependencies_ready = len(checks) > 1 or status == "passed"
        _need(type(raw["exported_environment"]) is bool)
        _need(
            raw["exported_environment"] == (dependencies_ready and dependency["mode"] == "created")
        )
        if dependencies_ready:
            _hash(raw["environment_sha256"])
        else:
            _need(raw["environment_sha256"] is None and reference is None)
        if reference is not None:
            _bind_reference(raw, reference)
        safe_report = {key: raw[key] for key in _PUBLIC_FIELDS}
        safe_report.update(
            checks=tuple(safe_checks),
            environment_sha256=reference.environment_sha256 if reference else None,
        )
        if status == "passed":
            if reference is None:
                raise ValueError(_ERROR)
            receipt = validate_python_passed_receipt(
                safe_report,
                expected_source=expected_source_sha256,
                expected_recipe=inputs.sha256,
                expected_environment_inputs=inputs.environment_input_sha256,
                expected_environment=reference.environment_sha256,
            )
            return PythonProjectReport(receipt, reference, expected_source_sha256)
        safe_report["original_check_contract"] = inputs.contract.protected_sha256
        return PythonProjectFailure(safe_report, reference, failure_diagnostic)
    except _INVALID:
        raise ProjectVerificationError(_ERROR) from None


def validate_python_failure(
    value: object,
    *,
    invocation: PythonProjectInvocation,
    inputs: PythonProjectInputs,
    expected_source_sha256: str,
    environment_reference: PythonEnvironmentReference | None,
) -> PythonProjectFailure:
    """Validate persisted *safe* failure data, not raw output or execution evidence.

    No decoding, provenance inference, synthetic process, or Node DTO is used.
    Context and an optional authenticated root reference must be supplied again.
    """
    try:
        invocation, inputs, reference = _context(
            invocation,
            inputs,
            expected_source_sha256,
            environment_reference,
        )
        report = _failure_shape(value)
        _bind_expected(report, invocation, inputs, expected_source_sha256)
        _bind_reference(report, reference)
        return PythonProjectFailure(report, reference)
    except _INVALID:
        raise ProjectVerificationError(_ERROR) from None
