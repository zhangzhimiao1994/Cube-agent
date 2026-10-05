"""Standalone, stdlib-only workspace manifest canonicalization."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath


def workspace_manifest_sha256(manifest: Mapping[str, tuple[int, str]]) -> str:
    """Match the independently shippable scale-validation protocol exactly."""
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

