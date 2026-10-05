"""Frozen source-copy metadata shared with the privileged broker (stdlib only)."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import PurePosixPath
from typing import Self, cast

from agent_hub.previews.cleanup import PreviewIdentityV1, timestamp, wire_object
from agent_hub.workspace_manifest import workspace_manifest_sha256

STATIC_SELECTION_POLICY = "static-display-root-v1"
DYNAMIC_SELECTION_POLICY = "dynamic-staged-session-v1"
MANIFEST_ALGORITHM = "workspace-manifest-sha256-v1"
_LIMITS = {
    STATIC_SELECTION_POLICY: (10_000, 128 * 1024 * 1024),
    DYNAMIC_SELECTION_POLICY: (4096, 32 * 1024 * 1024),
}
DYNAMIC_EXCLUDED_COMPONENTS = frozenset({
    "node_modules", ".git", ".preview-staging", ".venv", ".npmrc", ".ssh", ".aws", ".codex",
})


class PreviewProvenanceUnavailable(RuntimeError):
    """Source metadata is unknown; the owned runtime can still be stopped."""


def _limits(policy: str) -> tuple[int, int]:
    if not isinstance(policy, str) or policy not in _LIMITS:
        raise ValueError("unknown snapshot selection policy")
    return _LIMITS[policy]


def _version(value: int) -> None:
    if type(value) is not int or value != 1:
        raise ValueError("invalid provenance schema version")


@dataclass(frozen=True, slots=True)
class SnapshotManifestV1:
    schema_version: int
    algorithm: str
    selection_policy: str
    manifest_sha256: str
    file_count: int
    total_bytes: int

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if self.algorithm != MANIFEST_ALGORITHM:
            raise ValueError("invalid snapshot manifest algorithm")
        max_files, max_bytes = _limits(self.selection_policy)
        if (not isinstance(self.manifest_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", self.manifest_sha256) is None):
            raise ValueError("invalid snapshot manifest digest")
        if type(self.file_count) is not int or not 1 <= self.file_count <= max_files:
            raise ValueError("invalid snapshot file count")
        if type(self.total_bytes) is not int or not 0 <= self.total_bytes <= max_bytes:
            raise ValueError("invalid snapshot total bytes")

    def to_wire(self) -> dict[str, object]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = wire_object(value, {f.name for f in fields(cls)})
        return cls(**data)  # type: ignore[arg-type]

    @classmethod
    def from_manifest(cls, manifest: Mapping[str, tuple[int, str]], *, selection_policy: str,
                      max_files: int | None = None, max_bytes: int | None = None) -> Self:
        policy_files, policy_bytes = _limits(selection_policy)
        for supplied in (max_files, max_bytes):
            if supplied is not None and (type(supplied) is not int or supplied <= 0):
                raise ValueError("invalid producer manifest budget")
        file_limit = min(policy_files, max_files) if max_files is not None else policy_files
        byte_limit = min(policy_bytes, max_bytes) if max_bytes is not None else policy_bytes
        if not isinstance(manifest, Mapping) or len(manifest) > file_limit:
            raise ValueError("snapshot manifest file budget exceeded")
        digest = workspace_manifest_sha256(manifest)
        total = sum(size for size, _ in manifest.values())
        if total > byte_limit:
            raise ValueError("snapshot manifest byte budget exceeded")
        return cls(1, MANIFEST_ALGORITHM, selection_policy, digest, len(manifest), total)


@dataclass(frozen=True, slots=True)
class PreviewProvenanceV1:
    schema_version: int
    identity: PreviewIdentityV1
    snapshot_manifest: SnapshotManifestV1
    captured_at: datetime

    def __post_init__(self) -> None:
        _version(self.schema_version)
        if not isinstance(self.identity, PreviewIdentityV1):
            raise ValueError("invalid provenance identity")  # noqa: TRY004
        PreviewIdentityV1.from_wire(self.identity.to_wire())
        if not isinstance(self.snapshot_manifest, SnapshotManifestV1):
            raise ValueError("invalid provenance snapshot manifest")  # noqa: TRY004
        SnapshotManifestV1.from_wire(self.snapshot_manifest.to_wire())
        expected = STATIC_SELECTION_POLICY if self.identity.kind == "static" else DYNAMIC_SELECTION_POLICY
        if self.snapshot_manifest.selection_policy != expected:
            raise ValueError("snapshot policy and preview kind disagree")
        if not isinstance(self.captured_at, datetime):
            raise ValueError("invalid provenance capture timestamp")  # noqa: TRY004
        timestamp(self.captured_at.isoformat())

    def to_wire(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "identity": self.identity.to_wire(),
                "snapshot_manifest": self.snapshot_manifest.to_wire(),
                "captured_at": self.captured_at.isoformat()}

    @classmethod
    def from_wire(cls, value: object) -> Self:
        data = wire_object(value, {"schema_version", "identity", "snapshot_manifest", "captured_at"})
        return cls(cast(int, data["schema_version"]), PreviewIdentityV1.from_wire(data["identity"]),
                   SnapshotManifestV1.from_wire(data["snapshot_manifest"]), timestamp(data["captured_at"]))


def snapshot_manifest_for_identity(manifest: Mapping[str, tuple[int, str]],
                                   identity: PreviewIdentityV1) -> SnapshotManifestV1:
    """Project a validated session manifest using the producer's versioned policy."""
    workspace_manifest_sha256(manifest)
    identity = PreviewIdentityV1.from_wire(identity.to_wire())
    if identity.kind == "static":
        prefix = "" if identity.display_root == "." else identity.display_root + "/"
        selected = {path[len(prefix):]: value for path, value in manifest.items()
                    if path.startswith(prefix)}
        if identity.display_entrypoint not in selected:
            raise ValueError("static snapshot entrypoint missing")
        policy = STATIC_SELECTION_POLICY
    else:
        selected = {path: value for path, value in manifest.items()
                    if not any(part in DYNAMIC_EXCLUDED_COMPONENTS or part == ".env"
                               or part.startswith(".env.") for part in PurePosixPath(path).parts)}
        policy = DYNAMIC_SELECTION_POLICY
    return SnapshotManifestV1.from_manifest(selected, selection_policy=policy)

