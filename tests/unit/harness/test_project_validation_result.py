"""Strict result and workspace-identity contracts for the ultra load evaluator."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any

import pytest

_COUNTS = {
    "target_projects": 1000,
    "foreign_projects": 17,
    "module_records": 44,
    "concurrent_clients": 4,
    "peak_active_clients": 4,
    "initial_traversals": 4,
    "restart_traversals": 1,
    "updated_traversals": 1,
    "boundary_pages": 3,
    "read_model_requests": 105,
    "write_requests": 1064,
    "request_errors": 0,
}


def _protocol() -> ModuleType:
    return importlib.import_module("agent_hub.harness.project_validation_result")


def _result(status: str = "passed") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "profile": "ultra-load-v1",
        "scale": "ultra",
        "status": status,
        "reasons": [] if status == "passed" else ["validation interrupted"],
        "cleanup_ok": status == "passed",
        "measurements": {**_COUNTS, "elapsed_seconds": 1.25},
    }


def test_valid_result_is_deep_copied() -> None:
    payload = _result()
    validated = _protocol().validate_scale_validation_result(payload)
    assert validated == payload and validated is not payload
    validated["measurements"]["target_projects"] = 0
    validated["reasons"].append("mutated copy")
    assert payload == _result()
    assert _protocol().scale_validation_passed(payload) is True


@pytest.mark.parametrize("status", ["failed", "unknown"])
@pytest.mark.parametrize("cleanup_ok", [False, True])
def test_partial_results_are_valid_but_never_pass(status: str, cleanup_ok: bool) -> None:
    payload = _result(status)
    payload["cleanup_ok"] = cleanup_ok
    payload["measurements"] = {key: maximum // 2 for key, maximum in _COUNTS.items()}
    payload["measurements"].update(request_errors=10**20, elapsed_seconds=0)
    assert _protocol().validate_scale_validation_result(payload) == payload
    assert _protocol().scale_validation_passed(payload) is False


@pytest.mark.parametrize("key", _COUNTS)
@pytest.mark.parametrize("value", [True, False, -1, 1.0, "1", None])
def test_count_types_are_exact_nonnegative_integers(key: str, value: object) -> None:
    payload = _result("failed")
    payload["measurements"][key] = value
    with pytest.raises(ValueError):
        _protocol().validate_scale_validation_result(payload)
    assert _protocol().scale_validation_passed(payload) is False


@pytest.mark.parametrize("key", _COUNTS)
def test_passed_requires_every_exact_count(key: str) -> None:
    payload = _result()
    payload["measurements"][key] = max(0, _COUNTS[key] - 1) if _COUNTS[key] else 1
    with pytest.raises(ValueError):
        _protocol().validate_scale_validation_result(payload)
    assert _protocol().scale_validation_passed(payload) is False


@pytest.mark.parametrize("status", ["passed", "failed", "unknown"])
@pytest.mark.parametrize("key", [key for key in _COUNTS if key != "request_errors"])
def test_counts_cannot_exceed_profile_maxima(status: str, key: str) -> None:
    payload = _result(status)
    payload["measurements"][key] = _COUNTS[key] + 1
    with pytest.raises(ValueError):
        _protocol().validate_scale_validation_result(payload)


@pytest.mark.parametrize("value", [True, False, -1, -0.1, "1.25", None,
                                    float("nan"), float("inf"), -float("inf")])
def test_elapsed_seconds_rejects_nonfinite_or_coerced_values(value: object) -> None:
    payload = _result()
    payload["measurements"]["elapsed_seconds"] = value
    with pytest.raises(ValueError):
        _protocol().validate_scale_validation_result(payload)
    assert _protocol().scale_validation_passed(payload) is False


@pytest.mark.parametrize("value", [0, 0.0, 123, 1.5])
def test_elapsed_seconds_accepts_finite_nonnegative_numbers(value: float) -> None:
    payload = _result()
    payload["measurements"]["elapsed_seconds"] = value
    assert _protocol().validate_scale_validation_result(payload) == payload


@pytest.mark.parametrize(("key", "value"), [
    ("schema_version", True), ("schema_version", 1.0), ("schema_version", "1"),
    ("schema_version", 2), ("profile", "ultra-load-v2"), ("profile", []),
    ("scale", "large"), ("status", "success"), ("status", []),
    ("cleanup_ok", 1), ("cleanup_ok", "true"), ("cleanup_ok", False),
    ("reasons", ["unexpected failure"]), ("reasons", ()),
    ("measurements", []), ("unexpected", True),
])
def test_invalid_headers_and_success_claims_are_rejected(key: str, value: object) -> None:
    payload = _result()
    payload[key] = value
    with pytest.raises(ValueError):
        _protocol().validate_scale_validation_result(payload)
    assert _protocol().scale_validation_passed(payload) is False


@pytest.mark.parametrize("key", list(_result()))
def test_missing_top_level_fields_are_rejected(key: str) -> None:
    payload = _result()
    del payload[key]
    with pytest.raises(ValueError):
        _protocol().validate_scale_validation_result(payload)


@pytest.mark.parametrize("key", [*_COUNTS, "elapsed_seconds", "extra"])
def test_measurement_keys_are_exact(key: str) -> None:
    payload = _result()
    if key == "extra":
        payload["measurements"][key] = 0
    else:
        del payload["measurements"][key]
    with pytest.raises(ValueError):
        _protocol().validate_scale_validation_result(payload)


@pytest.mark.parametrize("status", ["failed", "unknown"])
@pytest.mark.parametrize("reasons", [[], [""], [" \t"], [1], [None], "failed", ("failed",)])
def test_nonpassed_results_require_nonempty_string_reasons(status: str, reasons: object) -> None:
    payload = _result(status)
    payload["reasons"] = reasons
    with pytest.raises(ValueError):
        _protocol().validate_scale_validation_result(payload)


@pytest.mark.parametrize("payload", [None, [], ["legacy failure"], True, "passed", 1, {}])
def test_invalid_payloads_fail_closed(payload: object) -> None:
    with pytest.raises(ValueError):
        _protocol().validate_scale_validation_result(payload)
    assert _protocol().scale_validation_passed(payload) is False


def test_unknown_has_zeroed_independent_measurements() -> None:
    payload = _protocol().scale_validation_unknown("sandbox unavailable")
    assert payload == {
        "schema_version": 1, "profile": "ultra-load-v1", "scale": "ultra",
        "status": "unknown", "reasons": ["sandbox unavailable"], "cleanup_ok": False,
        "measurements": {**dict.fromkeys(_COUNTS, 0), "elapsed_seconds": 0.0},
    }
    assert _protocol().validate_scale_validation_result(payload) == payload
    payload["measurements"]["target_projects"] = 5
    payload["reasons"].append("changed")
    fresh = _protocol().scale_validation_unknown("sandbox unavailable")
    assert fresh["measurements"]["target_projects"] == 0
    assert fresh["reasons"] == ["sandbox unavailable"]


@pytest.mark.parametrize("reason", ["", " \t", None, 42])
def test_unknown_rejects_invalid_reasons(reason: Any) -> None:
    with pytest.raises(ValueError):
        _protocol().scale_validation_unknown(reason)


def test_manifest_hash_uses_sorted_compact_utf8_json_and_binds_content() -> None:
    manifest: Mapping[str, tuple[int, str]] = MappingProxyType({
        "src/\u6d4b\u8bd5.py": (20, "b" * 64), "README.md": (10, "a" * 64),
    })
    canonical = ('{"README.md":[10,"' + "a" * 64 + '"],"src/\u6d4b\u8bd5.py":[20,"'
                 + "b" * 64 + '"]}').encode("utf-8")
    digest = _protocol().scale_validation_manifest_sha256(manifest)
    assert digest == hashlib.sha256(canonical).hexdigest()
    assert _protocol().scale_validation_manifest_sha256(dict(reversed(list(manifest.items())))) == digest
    assert _protocol().scale_validation_manifest_sha256(json.loads(json.dumps(dict(manifest)))) == digest
    for changed in (
        {**manifest, "README.md": (11, "a" * 64)},
        {**manifest, "README.md": (10, "c" * 64)},
        {"renamed.md": (10, "a" * 64), "src/\u6d4b\u8bd5.py": (20, "b" * 64)},
    ):
        assert _protocol().scale_validation_manifest_sha256(changed) != digest


@pytest.mark.parametrize("manifest", [
    None, {}, [], {1: (1, "a" * 64)}, {"README.md": (True, "a" * 64)},
    {"README.md": (-1, "a" * 64)}, {"README.md": (1.0, "a" * 64)},
    {"README.md": (1, "A" * 64)}, {"README.md": (1, "a" * 63)},
    {"README.md": (1, "a" * 63 + "g")}, {"README.md": (1, None)},
    {"README.md": (1, "a" * 64, "extra")}, {"README.md": "invalid"},
])
def test_manifest_hash_rejects_invalid_metadata(manifest: Any) -> None:
    with pytest.raises(ValueError):
        _protocol().scale_validation_manifest_sha256(manifest)


@pytest.mark.parametrize("path", ["", ".", "../a", "/a", "./a", "src\\a", "src//a",
                                   "src/./a", "src/../a", "src/", "C:/a", "GET/a"])
def test_manifest_hash_rejects_unsafe_or_noncanonical_paths(path: str) -> None:
    with pytest.raises(ValueError):
        _protocol().scale_validation_manifest_sha256({path: (0, "a" * 64)})


def test_result_module_loads_with_only_stdlib() -> None:
    source = Path(__file__).resolve().parents[3] / "src/agent_hub/harness/project_validation_result.py"
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c",
         ("import runpy, sys; m = runpy.run_path(sys.argv[1]); "
         "assert m['scale_validation_unknown']('isolated')['status'] == 'unknown'; "
          "assert 'agent_hub.harness' not in sys.modules"), str(source)],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert completed.returncode == 0, completed.stderr
