"""Strict Python schema 2 data validation, without provenance or availability.

Trusted callers select this validator and independently measure all four expected
hashes. A matching payload does not prove those measurements, wheel origin, or
execution history and grants no install, execution, cache, or ZIP authority.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from agent_hub.capabilities.python_project_inputs import PythonInterpreterIdentity
from agent_hub.runtime.contracts import JsonValue, _freeze_object

_SHA = re.compile(r"[a-f0-9]{64}")
_EXECUTION_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}")
_WHEEL_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]*\.whl")
_EMPTY_SHA = hashlib.sha256(b"").hexdigest()
_MAX_INT = 2**63 - 1
_ERROR = "invalid Python passed receipt"
_REPORT_FIELDS = {
    "schema_version", "kind", "status", "execution_id", "source_sha256", "recipe_sha256",
    "environment_input_sha256", "environment_sha256", "checks", "runtime", "wheel",
    "test_count", "dependency_install",
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


def _execution_id(value: JsonValue) -> str:
    if type(value) is not str or _EXECUTION_ID.fullmatch(value) is None:
        raise ValueError(_ERROR)
    return value


def _integer(value: JsonValue, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _MAX_INT:
        raise ValueError(_ERROR)
    return value


def _metadata(value: JsonValue) -> None:
    metadata = _object(value, {
        "sanitized_bytes", "returned_bytes", "sha256", "truncated", "redacted", "redacted_lines",
    })
    sanitized = _integer(metadata["sanitized_bytes"])
    returned = _integer(metadata["returned_bytes"])
    redacted_lines = _integer(metadata["redacted_lines"])
    digest = _hash(metadata["sha256"])
    _need(type(metadata["truncated"]) is bool and type(metadata["redacted"]) is bool)
    _need(metadata["redacted"] == (redacted_lines > 0))
    _need(returned <= min(sanitized, 4096))
    _need((digest == _EMPTY_SHA) == (sanitized == 0))
    if metadata["truncated"]:
        # Bounded prefix/head-tail previews lose at most three UTF-8 boundary bytes.
        _need(sanitized > 4096 and 4093 <= returned <= 4096)
    else:
        _need(sanitized <= 4096 and returned == sanitized)


def _diagnostic(value: JsonValue) -> None:
    diagnostic = _object(value, {
        "schema_version", "backend", "exit_code", "timed_out", "stdout_metadata", "stderr_metadata",
    })
    _need(type(diagnostic["schema_version"]) is int and diagnostic["schema_version"] == 1)
    _need(diagnostic["backend"] == "systemd")
    _need(type(diagnostic["exit_code"]) is int and diagnostic["exit_code"] == 0)
    _need(diagnostic["timed_out"] is False)
    _metadata(diagnostic["stdout_metadata"])
    _metadata(diagnostic["stderr_metadata"])


def validate_python_passed_receipt(
    value: object, *, expected_source: str, expected_recipe: str,
    expected_environment_inputs: str, expected_environment: str,
) -> Mapping[str, JsonValue]:
    """Validate and freeze exact receipt data against independently trusted hashes.

    No defaults, raw native report decoding, validator dispatch, or authority are
    provided. The caller must never derive expected hashes from this payload.
    """
    expected = (
        ("source_sha256", _hash(expected_source)),
        ("recipe_sha256", _hash(expected_recipe)),
        ("environment_input_sha256", _hash(expected_environment_inputs)),
        ("environment_sha256", _hash(expected_environment)),
    )
    report = _object(_freeze_object(value, name="Python passed receipt"), _REPORT_FIELDS)
    _need(type(report["schema_version"]) is int and report["schema_version"] == 2)
    _need(report["kind"] == "python_project" and report["status"] == "passed")
    execution_id = _execution_id(report["execution_id"])
    for field, digest in expected:
        _need(_hash(report[field]) == digest)

    checks = report["checks"]
    if not isinstance(checks, tuple) or len(checks) != 3:
        raise ValueError(_ERROR)
    for phase, item in zip(("install", "build", "test"), checks, strict=True):
        check = _object(item, {"phase", "status", "execution_diagnostic"})
        _need(check["phase"] == phase and check["status"] == "passed")
        _diagnostic(check["execution_diagnostic"])

    runtime = _object(report["runtime"], {
        "implementation", "version", "abi", "platform", "binary_sha256", "base_runtime_sha256",
    })
    PythonInterpreterIdentity.from_payload(dict(runtime))
    wheel = _object(report["wheel"], {"filename", "sha256", "size"})
    filename = wheel["filename"]
    _need(type(filename) is str and _WHEEL_FILENAME.fullmatch(filename) is not None)
    _hash(wheel["sha256"])
    _integer(wheel["size"], minimum=1)
    _integer(report["test_count"], minimum=1)

    install = _object(report["dependency_install"], {"mode", "origin_execution_id"})
    mode = install["mode"]
    _need(mode in ("created", "reused"))
    origin = _execution_id(install["origin_execution_id"])
    _need((origin == execution_id) == (mode == "created"))
    return report
