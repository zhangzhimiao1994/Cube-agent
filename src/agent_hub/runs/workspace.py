"""Workspace and sandbox selection helpers for run submission."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

SandboxProfile = str

SANDBOX_PROFILES = frozenset({"none", "read_only", "restricted", "workspace_write"})
REQUESTED_PERMISSIONS = frozenset(
    {
        "workspace.read",
        "workspace.write",
        "network.read",
        "command.run",
        "docker.run",
        "server.run",
    }
)
DEFAULT_PROJECT_ID = "default"
DEFAULT_PROJECT_LABEL = "默认项目"
_SAFE_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class WorkspaceSelection:
    project_id: str
    project_label: str
    session_id: str
    sandbox_profile: SandboxProfile
    requested_permissions: tuple[str, ...]

    @property
    def project_path(self) -> str:
        return f"projects/{self.project_id}"

    @property
    def session_path(self) -> str:
        return f"{self.project_path}/sessions/{self.session_id}"

    @property
    def artifacts_path(self) -> str:
        return f"{self.session_path}/artifacts"

    def routing_payload(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "project_label": self.project_label,
            "workspace_session_id": self.session_id,
            "workspace_project_path": self.project_path,
            "workspace_session_path": self.session_path,
            "workspace_artifacts_path": self.artifacts_path,
            "sandbox_profile": self.sandbox_profile,
            "requested_permissions": list(self.requested_permissions),
            "sandbox_permission_policy": "ask_on_escalation",
        }


def workspace_selection(
    *,
    project_id: str | None,
    session_id: str | None,
    project_label: str | None = None,
    sandbox_profile: SandboxProfile | None = None,
    requested_permissions: Iterable[str] | None = None,
) -> WorkspaceSelection:
    profile = _sandbox_profile(sandbox_profile)
    normalized_project_id = _normalize_segment(
        project_id,
        name="project_id",
        default=DEFAULT_PROJECT_ID,
    )
    normalized_session_id = _normalize_segment(
        session_id,
        name="workspace_session_id",
        default="session-default",
    )
    return WorkspaceSelection(
        project_id=normalized_project_id,
        project_label=_project_label(project_label, normalized_project_id),
        session_id=normalized_session_id,
        sandbox_profile=profile,
        requested_permissions=_permissions_for_profile(profile, requested_permissions),
    )


def _sandbox_profile(value: str | None) -> SandboxProfile:
    profile = (value or "none").strip()
    if profile not in SANDBOX_PROFILES:
        raise ValueError("sandbox_profile must be one of none, read_only, restricted, workspace_write")
    return profile


def _normalize_segment(value: str | None, *, name: str, default: str) -> str:
    raw = (value or default).strip()
    if not raw:
        raw = default
    if "/" in raw or "\\" in raw or ".." in raw:
        raise ValueError(f"{name} must be a single safe path segment")
    normalized = re.sub(r"[^a-z0-9_-]+", "-", raw.casefold())
    normalized = re.sub(r"[-_]{2,}", "-", normalized).strip("-_")
    if not normalized:
        normalized = default
    if _SAFE_SEGMENT.fullmatch(normalized) is None:
        raise ValueError(f"{name} must be a safe path segment")
    return normalized


def _project_label(value: str | None, project_id: str) -> str:
    label = (value or "").strip()
    if not label:
        return DEFAULT_PROJECT_LABEL if project_id == DEFAULT_PROJECT_ID else project_id
    return label[:80]


def _permissions_for_profile(
    profile: SandboxProfile,
    requested_permissions: Iterable[str] | None,
) -> tuple[str, ...]:
    permissions = tuple(dict.fromkeys(item.strip() for item in (requested_permissions or ()) if item.strip()))
    if not permissions:
        return _default_permissions(profile)
    unknown = [item for item in permissions if item not in REQUESTED_PERMISSIONS]
    if unknown:
        raise ValueError(f"unknown requested permission: {unknown[0]}")
    allowed = _allowed_permissions(profile)
    denied = [item for item in permissions if item not in allowed]
    if denied:
        raise ValueError(f"{denied[0]} is not allowed by sandbox_profile={profile}")
    return permissions


def _default_permissions(profile: SandboxProfile) -> tuple[str, ...]:
    if profile == "none":
        return ()
    if profile == "read_only":
        return ("workspace.read",)
    if profile == "restricted":
        return ("workspace.read", "network.read", "command.run")
    return ("workspace.read", "workspace.write", "network.read", "command.run")


def _allowed_permissions(profile: SandboxProfile) -> frozenset[str]:
    if profile == "none":
        return frozenset()
    if profile == "read_only":
        return frozenset({"workspace.read"})
    if profile == "restricted":
        return frozenset({"workspace.read", "network.read", "command.run"})
    return REQUESTED_PERMISSIONS
