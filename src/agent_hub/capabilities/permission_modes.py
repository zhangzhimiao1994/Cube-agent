"""Permission modes come only from the persisted, actor-bound run policy."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import TYPE_CHECKING, cast

from agent_hub.auth.models import Role
from agent_hub.capabilities.policy import normalize_resource
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.domain.runs import RunStatus, TaskMode

if TYPE_CHECKING:
    from agent_hub.runs.workspace import PermissionMode


async def run_permission_mode(
    repository: object, request: CapabilityRequest, *, role: Role,
) -> PermissionMode | None:
    mode, _ = await run_permission_policy(repository, request, role=role)
    return mode


async def run_permission_policy(
    repository: object, request: CapabilityRequest, *, role: Role,
) -> tuple[PermissionMode | None, bool]:
    """Read permission and exact-consent policy from the same persisted run."""
    from agent_hub.runs.workspace import PERMISSION_MODES, PermissionMode, workspace_selection

    getter = getattr(repository, "get", None)
    if not callable(getter):
        return None, False  # Legacy adapters without a run policy store.
    record = getter(request.tenant_id, request.run_id)
    if isinstance(record, Awaitable):
        record = await record
    route = getattr(record, "routing_decision", None)
    if not isinstance(route, Mapping):
        return None, False
    version = route.get("engineering_concrete_approval_version")
    opted_in = type(version) is int and version == 1
    stored_mode = getattr(record, "mode", None)
    if opted_in and stored_mode not in tuple(TaskMode):
        raise ValueError("run permission policy is unavailable")
    concrete = opted_in and stored_mode == TaskMode.DISCUSS
    if "permission_mode" not in route:
        if concrete:
            raise ValueError("run permission policy is unavailable")
        return None, False
    mode = route["permission_mode"]
    if (not isinstance(mode, str) or mode not in PERMISSION_MODES
        or getattr(record, "tenant_id", None) != request.tenant_id
        or getattr(record, "id", None) != request.run_id
        or getattr(record, "actor_id", None) != request.user_id
        or getattr(record, "actor_role", None) != role):
        raise ValueError("run permission policy is unavailable")
    profile = route.get("sandbox_profile")
    permissions = route.get("requested_permissions")
    if (not isinstance(profile, str) or not isinstance(permissions, (list, tuple))
        or not all(isinstance(item, str) for item in permissions)):
        raise ValueError("run permission policy is unavailable")
    workspace_selection(project_id=None, session_id=None, sandbox_profile=profile,
                        requested_permissions=permissions, permission_mode=cast(PermissionMode, mode))
    if mode != "read_only":
        if getattr(record, "status", None) != RunStatus.RUNNING:
            raise ValueError("run permission policy is unavailable")
        resource = normalize_resource(request.resource)
        required: set[str] = set()
        if resource == "workspace" or (resource is not None and resource.startswith("workspace/")):
            required.add("workspace.read")
            if request.capability == "file" and request.operation in {"write", "create"}:
                required.add("workspace.write")
            if request.capability == "command" and request.operation == "run":
                required.add("command.run")
        if not required.issubset(permissions):
            raise ValueError("run permission policy is unavailable")
    return cast(PermissionMode, mode), concrete


async def submission_capability_allowed(repository: object, request: CapabilityRequest, *, role: Role) -> bool:
    """Only built-in workspace actions covered by the validated selected policy."""
    if request.tool_origin != "builtin":
        return False
    getter = getattr(repository, "get", None)
    if not callable(getter):
        return False
    record = getter(request.tenant_id, request.run_id)
    if isinstance(record, Awaitable):
        record = await record
    route = getattr(record, "routing_decision", None)
    if (not isinstance(route, Mapping) or not all(isinstance(route.get(key), str) and route[key]
        for key in ("conversation_id", "project_id", "workspace_session_id"))
        or getattr(record, "tenant_id", None) != request.tenant_id
        or getattr(record, "id", None) != request.run_id
        or getattr(record, "actor_id", None) != request.user_id
        or getattr(record, "actor_role", None) != role
        or route.get("permission_mode") not in {"default", "auto_review"}
        or getattr(record, "status", None) != RunStatus.RUNNING):
        return False
    permissions = route.get("requested_permissions", ())
    if not isinstance(permissions, (list, tuple)) or "workspace.read" not in permissions:
        return False
    resource = normalize_resource(request.resource)
    if (request.capability == "command" and request.operation == "run"
        and resource == "workspace/current/project-verification"):
        return {"command.run", "network.read"}.issubset(permissions)
    return bool(route.get("sandbox_profile") == "workspace_write"
        and "workspace.write" in permissions and request.capability == "file" and (
        (request.operation == "write" and resource == "workspace/current")
        or (request.operation == "create" and resource == "workspace/bundle")
        or (request.operation == "create" and resource in {
            "generated/document.generate_docx", "generated/presentation.generate_pptx",
            "generated/project.generate_zip",
        })
    ))


def read_only_capability_allowed(request: CapabilityRequest) -> bool:
    if request.tool_origin != "builtin":
        return False
    resource = normalize_resource(request.resource)
    if resource is None:
        return False
    # External annotations are not yet trusted here. Unknown capabilities fail closed.
    return (
        (request.capability == "file" and request.operation == "read"
         and (resource == "workspace" or resource.startswith("workspace/")))
        or (request.capability == "context" and request.operation == "read"
            and (resource == "context" or resource.startswith("context/")))
        or (request.capability == "calculator" and request.operation == "evaluate"
            and resource == "calculator")
    )
