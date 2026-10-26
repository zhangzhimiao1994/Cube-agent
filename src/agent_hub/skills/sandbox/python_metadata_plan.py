"""Compact pre-capture metadata plans; values are not approval or IO authority.

Only a trusted Root observer may supply the catalog/cache observations. This pure
builder does not read the cache, authenticate a caller, fetch, publish or install.
The future executor must rederive the full catalog from current Root controls.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Literal

from agent_hub.skills.sandbox.python_dependency_metadata import PythonDependencyMetadataIndex
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
    PythonMetadataPlan,
    PythonMetadataPlanningRequest,
)
from agent_hub.skills.sandbox.python_project_backend import RootConfig


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


def _identities(entries: tuple[WheelDownload, ...]) -> list[dict[str, object]]:
    return [{"filename": w.filename, "name": w.name, "version": w.version,
        "sha256": w.sha256, "size_bytes": w.size_bytes}
        for w in sorted(entries, key=lambda e: (e.name, e.version, e.filename, e.sha256))]


def build_python_metadata_plan(
    request: PythonMetadataPlanningRequest, *, root: RootConfig, supply: RootSupplyConfig,
    cached: tuple[WheelDownload, ...], index: PythonDependencyMetadataIndex | None = None,
) -> PythonMetadataPlan:
    """Bind complete catalog and exact missing subset, never a selected lock only."""
    try:
        if (type(request) is not PythonMetadataPlanningRequest or type(root) is not RootConfig
                or type(supply) is not RootSupplyConfig or type(cached) is not tuple):
            raise ValueError
        request = PythonMetadataPlanningRequest.model_validate(request, strict=True)
        root = replace(root)
        supply = replace(supply)
        root_sha = _digest(root.to_payload())
        if root_sha != request.expected_root_config_sha256 or supply.digest != request.expected_catalog_sha256:
            raise ValueError("metadata_plan_catalog_changed")
        catalog = {entry.filename: entry for entry in supply.catalog}
        seen: set[str] = set()
        for entry in cached:
            if type(entry) is not WheelDownload:
                raise ValueError
            checked = replace(entry)
            if checked.filename in seen or catalog.get(checked.filename) != checked:
                raise ValueError
            seen.add(checked.filename)
        total = sum(entry.size_bytes for entry in supply.catalog)
        index_sha = None
        mode: Literal["ready", "publish_cached", "download_required"]
        if index is not None:
            if type(index) is not PythonDependencyMetadataIndex:
                raise ValueError
            # Round-trip immutable values as well; __new__/object.__setattr__ are not validation.
            index = PythonDependencyMetadataIndex.from_bytes(index.to_bytes())
            index.require_catalog(supply.catalog, supply.digest)
            index_sha = index.sha256
            missing: tuple[WheelDownload, ...] = ()
            mode = "ready"
        else:
            if total > min(supply.max_total_bytes, root.wheel_max_bytes):
                raise ValueError("metadata_plan_quota")
            missing = tuple(entry for entry in supply.catalog if entry.filename not in seen)
            mode = "download_required" if missing else "publish_cached"
        return PythonMetadataPlan(scope=request.scope, draft=request.draft,
            root_config_sha256=root_sha, catalog_sha256=supply.digest,
            catalog_identity_sha256=_digest(_identities(supply.catalog)),
            catalog_count=len(supply.catalog), catalog_bytes=total,
            missing_identity_sha256=_digest(_identities(missing)),
            missing_count=len(missing), missing_bytes=sum(entry.size_bytes for entry in missing),
            max_total_bytes=supply.max_total_bytes, wheel_max_bytes=root.wheel_max_bytes,
            min_free_bytes=supply.min_free_bytes, fetch_timeout_seconds=supply.fetch_timeout_seconds,
            mode=mode, metadata_index_sha256=index_sha)
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError) as error:
        code = str(error)
        raise ValueError(code if code in {"metadata_plan_catalog_changed", "metadata_plan_quota"}
                         else "metadata_plan_request_invalid") from None
