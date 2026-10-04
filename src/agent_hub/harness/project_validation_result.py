"""Strict, stdlib-only result protocols for scale-specific validation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import PurePosixPath
from typing import cast

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
_RESULT_KEYS = {
    "schema_version", "profile", "scale", "status", "reasons", "cleanup_ok", "measurements",
}
STORAGE_PROFILE = "ultra-load-storage-v1"
LARGE_MODULE_PROFILE = "large-module-v1"
_LARGE_COUNTS = {
    "inventory_stores": 2, "inventory_instances": 3, "inventory_calls": 14,
    "inventory_commits": 14, "inventory_successes": 10, "inventory_conflicts": 4,
    "concurrency_pairs": 1, "reporting_instances": 2, "reporting_snapshots": 6,
    "reporting_reads": 6, "composition_http_requests": 9, "composition_markers": 5,
    "starts": 1, "stops": 1,
}
_ISOLATION_COUNTS = {
    "starts": 3, "stops": 3, "empty_program_checks": 1, "marker_writes": 2,
    "marker_readbacks": 2, "original_program_checks": 2, "marker_absence_checks": 2,
}
_RELOCATION_COUNTS = {
    "starts": 1, "stops": 1, "target_projects": 1000, "foreign_projects": 17,
    "traversals": 2, "read_model_requests": 13, "original_program_checks": 2,
}
_DIGESTS = {
    "source_data_sha256", "copied_data_sha256", "frozen_code_sha256", "relocated_code_sha256",
}
_RELOCATION_EXTRA = {"old_paths_unavailable", "data_files", "data_bytes", *_DIGESTS}


def scale_validation_profile(scale: str) -> str | None:
    """Return the complete acceptance profile for scales with an extra evidence gate."""
    return {"large": LARGE_MODULE_PROFILE, "ultra": STORAGE_PROFILE}.get(scale)


def validate_scale_validation_result(
    payload: object, *, expected_profile: str | None = None,
) -> dict[str, object]:
    """Return an independent validated result, rejecting malformed success claims."""
    if expected_profile is not None and (
        not isinstance(payload, dict) or payload.get("profile") != expected_profile
    ):
        raise ValueError(f"scale validation requires {expected_profile}")
    if isinstance(payload, dict) and payload.get("profile") == STORAGE_PROFILE:
        return _validate_storage_result(payload)
    if isinstance(payload, dict) and payload.get("profile") == LARGE_MODULE_PROFILE:
        return _validate_large_result(payload)
    if not isinstance(payload, dict) or payload.keys() != _RESULT_KEYS:
        raise ValueError("scale validation result must have exactly the protocol fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("scale validation schema_version must be integer 1")
    if payload["profile"] != "ultra-load-v1" or payload["scale"] != "ultra":
        raise ValueError("scale validation requires the ultra-load-v1 profile and ultra scale")
    status = payload["status"]
    if not isinstance(status, str) or status not in {"passed", "failed", "unknown"}:
        raise ValueError("scale validation status must be passed, failed or unknown")
    reasons = payload["reasons"]
    if not isinstance(reasons, list) or any(
        not isinstance(reason, str) or not reason.strip() for reason in reasons
    ):
        raise ValueError("scale validation reasons must be a list of nonempty strings")
    if type(payload["cleanup_ok"]) is not bool:
        raise ValueError("scale validation cleanup_ok must be a boolean")
    if status == "passed":
        if reasons or not payload["cleanup_ok"]:
            raise ValueError("passed scale validation requires cleanup and no reasons")
    elif not reasons:
        raise ValueError("failed or unknown scale validation requires reasons")

    measurements = payload["measurements"]
    if not isinstance(measurements, dict) or measurements.keys() != {*_COUNTS, "elapsed_seconds"}:
        raise ValueError("scale validation measurements must have exactly the profile fields")
    for key, expected in _COUNTS.items():
        value = measurements[key]
        if type(value) is not int or value < 0:
            raise ValueError(f"scale validation {key} must be a nonnegative integer")
        if status == "passed" and value != expected:
            raise ValueError(f"passed scale validation requires {key}={expected}")
        if key != "request_errors" and value > expected:
            raise ValueError(f"scale validation {key} exceeds profile maximum {expected}")
    elapsed = measurements["elapsed_seconds"]
    if (
        type(elapsed) not in (int, float) or elapsed < 0
        or (type(elapsed) is float and not math.isfinite(elapsed))
    ):
        raise ValueError("scale validation elapsed_seconds must be finite and nonnegative")
    return deepcopy(cast(dict[str, object], payload))


def scale_validation_passed(payload: object, *, expected_profile: str | None = None) -> bool:
    """Fail closed for any result that does not satisfy the complete success contract."""
    try:
        return validate_scale_validation_result(
            payload, expected_profile=expected_profile,
        )["status"] == "passed"
    except ValueError:
        return False


def scale_validation_unknown(reason: str, *, profile: str = "ultra-load-v1") -> dict[str, object]:
    """Describe unavailable validation without inventing measurements or cleanup evidence."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("unknown scale validation requires a nonempty reason")
    if profile == LARGE_MODULE_PROFILE:
        return {
            "schema_version": 1, "profile": LARGE_MODULE_PROFILE, "scale": "large",
            "status": "unknown", "reasons": [reason], "cleanup_ok": False,
            "isolation_verified": False, "npm_start_module_binding": "unknown",
            "measurements": {**dict.fromkeys(_LARGE_COUNTS, 0), "elapsed_seconds": 0.0},
        }
    if profile == STORAGE_PROFILE:
        def check(measurements: dict[str, object]) -> dict[str, object]:
            return {"status": "unknown", "reasons": [reason], "cleanup_ok": False,
                    "measurements": measurements}

        return {
            "schema_version": 1, "profile": STORAGE_PROFILE, "scale": "ultra",
            "status": "unknown", "reasons": [reason], "cleanup_ok": False,
            "checks": {
                "load": scale_validation_unknown(reason),
                "data_dir_isolation": check(dict.fromkeys(_ISOLATION_COUNTS, 0)),
                "same_version_relocation": check({
                    **dict.fromkeys(_RELOCATION_COUNTS, 0),
                    "old_paths_unavailable": False, "data_files": 0, "data_bytes": 0,
                    **dict.fromkeys(_DIGESTS),
                }),
            },
        }
    if profile != "ultra-load-v1":
        raise ValueError("unsupported scale validation profile")
    return {
        "schema_version": 1,
        "profile": "ultra-load-v1",
        "scale": "ultra",
        "status": "unknown",
        "reasons": [reason],
        "cleanup_ok": False,
        "measurements": {**dict.fromkeys(_COUNTS, 0), "elapsed_seconds": 0.0},
    }


def _validate_large_result(payload: dict[str, object]) -> dict[str, object]:
    if payload.keys() != _RESULT_KEYS | {"isolation_verified", "npm_start_module_binding"}:
        raise ValueError("large module result must have exactly the protocol fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("large module schema_version must be integer 1")
    if payload["scale"] != "large":
        raise ValueError("large module profile requires large scale")
    status = payload["status"]
    if not isinstance(status, str) or status not in {"passed", "failed", "unknown"}:
        raise ValueError("invalid large module status")
    reasons = payload["reasons"]
    if not isinstance(reasons, list) or any(
        not isinstance(reason, str) or not reason.strip() for reason in reasons
    ) or bool(reasons) != (status != "passed"):
        raise ValueError("large module reasons must match the observed status")
    for key in ("cleanup_ok", "isolation_verified"):
        if type(payload[key]) is not bool or (status == "passed" and not payload[key]):
            raise ValueError(f"large module {key} must be boolean and true for passed")
    if payload["npm_start_module_binding"] != "unknown":
        raise ValueError("large module npm_start_module_binding must remain unknown")
    measurements = payload["measurements"]
    if not isinstance(measurements, dict) or measurements.keys() != {
        *_LARGE_COUNTS, "elapsed_seconds",
    }:
        raise ValueError("large module measurements must have exactly the profile fields")
    for key, maximum in _LARGE_COUNTS.items():
        value = measurements[key]
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError(f"invalid large module count: {key}")
        if status == "passed" and value != maximum:
            raise ValueError(f"passed large module validation requires {key}={maximum}")
    elapsed = measurements["elapsed_seconds"]
    if (
        type(elapsed) not in (int, float) or elapsed < 0
        or (type(elapsed) is float and not math.isfinite(elapsed))
    ):
        raise ValueError("large module elapsed_seconds must be finite and nonnegative")
    return deepcopy(payload)


def _storage_status(check: dict[str, object]) -> str:
    status, reasons = check["status"], check["reasons"]
    if not isinstance(status, str) or status not in {"passed", "failed", "unknown"}:
        raise ValueError("invalid storage status")
    if not isinstance(reasons, list) or any(
        not isinstance(item, str) or not item.strip() for item in reasons
    ) or bool(reasons) != (status != "passed"):
        raise ValueError("storage reasons must match the observed status")
    if type(check["cleanup_ok"]) is not bool or (status == "passed" and not check["cleanup_ok"]):
        raise ValueError("storage cleanup must be boolean and true for passed")
    return status


def _validate_storage_result(payload: dict[str, object]) -> dict[str, object]:
    if payload.keys() != (_RESULT_KEYS - {"measurements"}) | {"checks"}:
        raise ValueError("storage result must have exactly the protocol fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("storage schema_version must be integer 1")
    if payload["scale"] != "ultra":
        raise ValueError("storage profile requires ultra scale")
    status = _storage_status(payload)
    checks = payload["checks"]
    if not isinstance(checks, dict) or checks.keys() != {
        "load", "data_dir_isolation", "same_version_relocation",
    }:
        raise ValueError("storage requires load, isolation and relocation checks")
    load = validate_scale_validation_result(checks["load"], expected_profile="ultra-load-v1")
    statuses = [load["status"]]
    cleanup = [load["cleanup_ok"]]
    for name, counts in (("data_dir_isolation", _ISOLATION_COUNTS),
                         ("same_version_relocation", _RELOCATION_COUNTS)):
        check = checks[name]
        if not isinstance(check, dict) or check.keys() != {
            "status", "reasons", "cleanup_ok", "measurements",
        }:
            raise ValueError(f"{name} must have exactly the subcheck fields")
        check_status = _storage_status(check)
        statuses.append(check_status)
        cleanup.append(check["cleanup_ok"])
        measurements = check["measurements"]
        extra = _RELOCATION_EXTRA if name == "same_version_relocation" else set()
        if not isinstance(measurements, dict) or measurements.keys() != counts.keys() | extra:
            raise ValueError(f"{name} must have exactly the measurement fields")
        for key, maximum in counts.items():
            value = measurements[key]
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError(f"invalid {name} count: {key}")
            if check_status == "passed" and value != maximum:
                raise ValueError(f"incomplete {name} observation: {key}")
        if name == "same_version_relocation":
            for key in ("data_files", "data_bytes"):
                value = measurements[key]
                if type(value) is not int or value < 0 or (check_status == "passed" and value == 0):
                    raise ValueError(f"invalid relocation copy size: {key}")
            if type(measurements["old_paths_unavailable"]) is not bool:
                raise ValueError("old_paths_unavailable must be an observed boolean")
            for key in _DIGESTS:
                value = measurements[key]
                if value is None and check_status != "passed":
                    continue
                if not isinstance(value, str) or re.fullmatch("[a-f0-9]{64}", value) is None:
                    raise ValueError(f"invalid relocation digest: {key}")
            if check_status == "passed" and (
                not measurements["old_paths_unavailable"]
                or measurements["source_data_sha256"] != measurements["copied_data_sha256"]
                or measurements["frozen_code_sha256"] != measurements["relocated_code_sha256"]
            ):
                raise ValueError("relocation requires isolated old paths and identical trees")
    if payload["cleanup_ok"] and not all(cleanup):
        raise ValueError("storage cleanup cannot hide subcheck cleanup failures")
    if status == "passed" and any(item != "passed" for item in statuses):
        raise ValueError("storage pass requires every subcheck passed")
    if status == "unknown" and "failed" in statuses:
        raise ValueError("storage unknown cannot hide a failed subcheck")
    return deepcopy(payload)


def scale_validation_manifest_sha256(manifest: Mapping[str, tuple[int, str]]) -> str:
    """Hash a valid workspace manifest as sorted, compact, unescaped UTF-8 JSON."""
    if not isinstance(manifest, Mapping) or not manifest:
        raise ValueError("workspace manifest must be a nonempty mapping")
    validated: dict[str, tuple[int, str]] = {}
    for path, metadata in manifest.items():
        if not isinstance(path, str) or not isinstance(metadata, (list, tuple)) or len(metadata) != 2:
            raise ValueError("workspace manifest entries require a path, size and SHA-256")
        member = PurePosixPath(path)
        if (
            "\\" in path or member.is_absolute() or not member.parts or str(member) != path
            or any(part in {"", ".", ".."} or ":" in part for part in member.parts)
            or member.parts[0].strip().upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        ):
            raise ValueError("workspace manifest paths must be safe and canonical")
        size, digest = metadata
        if (
            type(size) is not int or size < 0 or not isinstance(digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", digest) is None
        ):
            raise ValueError("workspace manifest requires nonnegative sizes and lowercase SHA-256")
        validated[path] = (size, digest)
    canonical = json.dumps(validated, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
