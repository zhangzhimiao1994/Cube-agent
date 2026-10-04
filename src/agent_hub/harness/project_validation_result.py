"""Strict, stdlib-only result protocol for ultra portfolio load validation."""

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


def validate_scale_validation_result(payload: object) -> dict[str, object]:
    """Return an independent validated result, rejecting malformed success claims."""
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


def scale_validation_passed(payload: object) -> bool:
    """Fail closed for any result that does not satisfy the complete success contract."""
    try:
        return validate_scale_validation_result(payload)["status"] == "passed"
    except ValueError:
        return False


def scale_validation_unknown(reason: str) -> dict[str, object]:
    """Describe unavailable validation without inventing measurements or cleanup evidence."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("unknown scale validation requires a nonempty reason")
    return {
        "schema_version": 1,
        "profile": "ultra-load-v1",
        "scale": "ultra",
        "status": "unknown",
        "reasons": [reason],
        "cleanup_ok": False,
        "measurements": {**dict.fromkeys(_COUNTS, 0), "elapsed_seconds": 0.0},
    }


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
