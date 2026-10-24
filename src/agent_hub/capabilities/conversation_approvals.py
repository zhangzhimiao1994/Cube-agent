"""Versioned, hash-only bindings for conservative conversation approval grants."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import cast
from uuid import UUID

from agent_hub.auth.models import Role
from agent_hub.capabilities.policy import normalize_resource
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runs.workspace import PermissionMode, workspace_selection


def conversation_approval_context(record: object) -> str | None:
    route = getattr(record, "routing_decision", None)
    if not isinstance(route, Mapping):
        return None
    keys = ("conversation_id", "project_id", "workspace_session_id", "sandbox_profile", "permission_mode")
    if not all(isinstance(route.get(key), str) and route[key] for key in keys):
        return None
    if route["permission_mode"] not in {"default", "auto_review"}:
        return None
    permissions = route.get("requested_permissions")
    if not isinstance(permissions, (list, tuple)) or not all(isinstance(p, str) for p in permissions):
        return None
    if (not isinstance(getattr(record, "tenant_id", None), UUID)
        or not isinstance(getattr(record, "actor_id", None), UUID)
        or getattr(record, "actor_role", None) not in {Role.OPERATOR, Role.ADMIN, Role.SUPER_ADMIN}):
        return None
    try:
        workspace_selection(project_id=None, session_id=None,
                            sandbox_profile=cast(str, route["sandbox_profile"]),
                            requested_permissions=permissions,
                            permission_mode=cast(PermissionMode, route["permission_mode"]))
        context = {key: route.get(key) for key in (
            *keys, "workspace_project_path", "workspace_session_path", "workspace_artifacts_path",
            "sandbox_permission_policy", "execution_backend", "engineering_concrete_approval_version",
        )}
        context.update(tenant_id=str(getattr(record, "tenant_id", None)),
                       actor_id=str(getattr(record, "actor_id", None)),
                       actor_role=str(getattr(record, "actor_role", None)), permissions=sorted(set(permissions)))
        return _digest(context)
    except (TypeError, ValueError):
        return None


def conversation_approval_binding(record: object, request: CapabilityRequest) -> dict[str, object] | None:
    context = conversation_approval_context(record)
    resource = normalize_resource(request.resource)
    if (context is None or request.tool_origin != "builtin" or not request.arguments
        or getattr(record, "id", None) != request.run_id
        or getattr(record, "tenant_id", None) != request.tenant_id
        or getattr(record, "actor_id", None) != request.user_id
        or not ((request.capability == "command" and request.operation == "run"
                 and resource == "workspace/current/project-verification")
                or (request.capability == "file" and request.operation in {"write", "create"}
                    and resource in {"workspace/current", "workspace/bundle",
                                     "generated/document.generate_docx", "generated/presentation.generate_pptx",
                                     "generated/project.generate_zip"}))):
        return None
    try:
        action = _digest({"agent_id": request.agent_id, "tool_origin": request.tool_origin,
                          "capability": request.capability, "operation": request.operation,
                          "resource": resource, "arguments": request.arguments})
        return {"schema_version": 1, "context_sha256": context, "action_sha256": action}
    except (TypeError, ValueError):
        return None


def conversation_binding_valid(record: object, binding: object) -> bool:
    return bool(isinstance(binding, dict)
        and set(binding) == {"schema_version", "context_sha256", "action_sha256"}
        and type(binding.get("schema_version")) is int and binding["schema_version"] == 1
        and all(isinstance(binding.get(key), str) and re.fullmatch(r"[a-f0-9]{64}", binding[key])
                for key in ("context_sha256", "action_sha256"))
        and binding["context_sha256"] == conversation_approval_context(record))


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()
