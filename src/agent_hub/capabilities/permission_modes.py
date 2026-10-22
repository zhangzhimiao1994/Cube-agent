"""Permission modes come only from the persisted, actor-bound run policy."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import TYPE_CHECKING, cast

from agent_hub.auth.models import Role
from agent_hub.capabilities.policy import normalize_resource
from agent_hub.capabilities.types import CapabilityRequest

if TYPE_CHECKING:
    from agent_hub.runs.workspace import PermissionMode


async def run_permission_mode(
    repository: object, request: CapabilityRequest, *, role: Role,
) -> PermissionMode | None:
    from agent_hub.runs.workspace import PERMISSION_MODES, PermissionMode, workspace_selection

    getter = getattr(repository, "get", None)
    if not callable(getter):
        return None  # Legacy adapters without a run policy store.
    record = getter(request.tenant_id, request.run_id)
    if isinstance(record, Awaitable):
        record = await record
    route = getattr(record, "routing_decision", None)
    if not isinstance(route, Mapping) or "permission_mode" not in route:
        return None
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
    return cast(PermissionMode, mode)


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
