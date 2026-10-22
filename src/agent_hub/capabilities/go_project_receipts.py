"""Pure Go passed-receipt validation, with no IO, execution or authority.

Trusted Root callers must actually measure source, exported binary hash/size and
execution identity, and reconstruct inputs from sealed originals independently.
Never derive expected values from the receipt. Matching data proves neither
Root provenance nor execution history, live DB approval, freshness, availability
or permission to execute, install, cache, publish or deliver anything.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from agent_hub.capabilities.go_project_business import GoBusinessSuite
from agent_hub.capabilities.go_project_inputs import GoProjectInputs
from agent_hub.runtime.contracts import JsonValue, _freeze_object

_ERROR = "invalid Go passed receipt"
_SHA = re.compile(r"[a-f0-9]{64}")
_EXECUTION_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}")
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                  r"(?:\.[0-9]+)?(?:Z|\+00:00)")
_MAX_INT = 2**63 - 1
_FIELDS = {
    "schema_version", "kind", "status", "execution_id", "scope", "source_kind",
    "source_sha256", "recipe_sha256", "environment_input_sha256", "original_reference",
    "original_check_contract", "toolchain", "acceptance_sha256", "business_suite_sha256",
    "binary", "checks", "named_tests", "business_evidence", "verified_at",
}
_BUSINESS_FIELDS = {
    "case_id", "criterion_id", "case_sha256", "expected_exit", "exit_code", "timed_out",
    "stdout_sha256", "stderr_sha256", "stdout_observed_bytes", "stderr_observed_bytes",
    "stdout_truncated", "stderr_truncated", "stdout_utf8_complete", "stderr_utf8_complete",
    "stdout_matches", "stderr_matches", "passed", "reason",
}


def _need(condition: bool) -> None:
    if not condition:
        raise ValueError(_ERROR)


def _object(value: JsonValue, fields: set[str]) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(_ERROR)
    return value


def _hash(value: object) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError(_ERROR)
    return value


def _execution_id(value: object) -> str:
    if type(value) is not str or _EXECUTION_ID.fullmatch(value) is None:
        raise ValueError(_ERROR)
    return value


def _integer(value: object, *, minimum: int = 0, maximum: int = _MAX_INT) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(_ERROR)
    return value


def _canonical(value: object) -> str:
    return json.dumps(value, default=dict, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _bound(value: JsonValue, expected: object) -> None:
    # Canonical JSON equality also distinguishes bool/int/float in nested DTOs.
    _need(_canonical(value) == _canonical(expected))


def _business(value: JsonValue, suite: GoBusinessSuite) -> None:
    cases = suite.cases
    _need(len({case.id for case in cases}) == len(cases)
          and any(case.expected_exit == 0 for case in cases)
          and any(case.expected_exit != 0 for case in cases))
    if not isinstance(value, tuple) or len(value) != len(cases):
        raise ValueError(_ERROR)
    for item, case in zip(value, cases, strict=True):
        row = _object(item, _BUSINESS_FIELDS)
        canonical_sha = hashlib.sha256(_canonical(case.to_payload()).encode("ascii")).hexdigest()
        case_sha = _hash(getattr(case, "sha256", canonical_sha))
        _need(row["case_id"] == case.id and row["criterion_id"] == case.criterion_id
              and _hash(row["case_sha256"]) == case_sha)
        expected_exit = _integer(case.expected_exit, maximum=255)
        _need(_integer(row["expected_exit"], maximum=255) == expected_exit
              and _integer(row["exit_code"], maximum=255) == expected_exit
              and row["timed_out"] is False and row["passed"] is True
              and row["reason"] is None)
        for stream, text in (("stdout", case.expected_stdout), ("stderr", case.expected_stderr)):
            raw = text.encode("utf-8", errors="strict")
            _need(_hash(row[stream + "_sha256"]) == hashlib.sha256(raw).hexdigest()
                  and _integer(row[stream + "_observed_bytes"]) == len(raw)
                  and row[stream + "_truncated"] is False
                  and row[stream + "_utf8_complete"] is True
                  and row[stream + "_matches"] is True)


def validate_go_passed_receipt(
    value: object, *, inputs: GoProjectInputs, expected_source: str,
    expected_binary_sha256: str, expected_binary_bytes: int, expected_execution_id: str,
    source_kind: Literal["original", "candidate"],
) -> Mapping[str, JsonValue]:
    """Validate and deeply freeze data against independently trusted observations.

    No IO or code execution occurs. Inputs and all expected values must come from
    the trusted Root boundary, never this value or project diagnostic JSON. This
    function cannot authenticate that boundary, DB approval or availability.
    verified_at checks UTC RFC3339 syntax/calendar only, never time or freshness.
    """
    try:
        if type(inputs) is not GoProjectInputs or inputs.acceptance is None:
            raise ValueError(_ERROR)
        acceptance = inputs.acceptance
        suite = acceptance.business
        source = _hash(expected_source)
        binary_sha = _hash(expected_binary_sha256)
        binary_bytes = _integer(expected_binary_bytes, minimum=1)
        execution_id = _execution_id(expected_execution_id)
        _need(type(source_kind) is str and source_kind in ("original", "candidate"))
        if source_kind == "original":
            _need(source == inputs.scope.source_sha256 == inputs.original_reference.input_source_sha256)

        # Size the node allowance from trusted expectations, not report lengths;
        # retain the shared freezer's depth, string and serialized byte limits.
        nodes = 4096 + len(acceptance.test_ids) + len(suite.cases) * (2 * len(_BUSINESS_FIELDS) + 1)
        report = _object(_freeze_object(value, name="Go passed receipt", max_nodes=nodes), _FIELDS)
        _need(_integer(report["schema_version"]) == 1
              and report["kind"] == "go_project_execution_receipt" and report["status"] == "passed")
        _need(_execution_id(report["execution_id"]) == execution_id
              and report["source_kind"] == source_kind)
        for field, digest in (
            ("source_sha256", source), ("recipe_sha256", _hash(inputs.sha256)),
            ("environment_input_sha256", _hash(inputs.environment_input_sha256)),
            ("original_check_contract", _hash(inputs.contract.protected_sha256)),
            ("acceptance_sha256", _hash(acceptance.sha256)),
            ("business_suite_sha256", _hash(suite.sha256)),
        ):
            _need(_hash(report[field]) == digest)
        _bound(report["scope"], inputs.scope.to_payload())
        _bound(report["original_reference"], inputs.original_reference.to_payload())
        _bound(report["toolchain"], inputs.toolchain.model_dump(mode="json"))

        binary = _object(report["binary"], {"filename", "sha256", "size", "format", "target"})
        _need(binary["filename"] == "project" and binary["format"] == "ELF64"
              and binary["target"] == "linux_amd64" and _hash(binary["sha256"]) == binary_sha
              and _integer(binary["size"], minimum=1) == binary_bytes)
        checks = report["checks"]
        if not isinstance(checks, tuple) or len(checks) != 5:
            raise ValueError(_ERROR)
        for phase, item in zip(("inspect", "build", "test", "canary", "business"), checks, strict=True):
            check = _object(item, {"phase", "status"})
            _need(check["phase"] == phase and check["status"] == "passed")

        tests = report["named_tests"]
        if not isinstance(tests, tuple) or not tests or not all(type(test) is str for test in tests):
            raise ValueError(_ERROR)
        _need(len(set(tests)) == len(tests) and set(tests) == set(acceptance.test_ids))
        _business(report["business_evidence"], suite)
        timestamp = report["verified_at"]
        if type(timestamp) is not str or _UTC.fullmatch(timestamp) is None:
            raise ValueError(_ERROR)
        datetime.fromisoformat(timestamp)
        return report
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise ValueError(_ERROR) from None
