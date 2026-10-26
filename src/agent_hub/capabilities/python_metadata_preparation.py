"""Pre-capture metadata preparation binding; never supply or install authority."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Literal, cast
from uuid import UUID

from agent_hub.capabilities.generated_project_service import (
    STATE,
    generated_authority,
)
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runtime.contracts import JsonValue, _freeze_object
from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
    PythonMetadataPlan,
    PythonMetadataScope,
)

METADATA_KEY = "python_metadata_preparation"
METADATA_PREFIX = "workspace/current/project-metadata/python/"
_INVALID = "metadata_prepare_scope_changed"


def _encoded(value: object) -> bytes:
    return json.dumps(value, default=dict, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def metadata_scope(record: object, *, actor: str, live: bool = False) -> PythonMetadataScope:
    """Derive scope only from authoritative run state, never a model plan."""
    try:
        tenant, run = getattr(record, "tenant_id", None), getattr(record, "id", None)
        if not isinstance(tenant, UUID) or not isinstance(run, UUID):
            raise TypeError
        authority = generated_authority(record, tenant, run, readonly=not live)
        route = getattr(record, "routing_decision", None)
        if (not isinstance(route, Mapping) or STATE in route
                or any(type(route.get(key)) is not int or route[key] != 1 for key in (
                    "python_dependency_preflight_version", "python_dependency_preflight_submission_version"))):
            raise ValueError
        permissions = route.get("requested_permissions")
        if (not isinstance(permissions, (list, tuple))
                or not all(type(p) is str for p in permissions)):
            raise ValueError
        bound = {"sandbox_profile": route["sandbox_profile"],
            "execution_backend": route.get("execution_backend", "systemd"),
            "permissions": sorted(set(permissions))}
        return PythonMetadataScope(tenant_id=authority.tenant_id, run_id=authority.run_id,
            user_id=authority.user_id, project_id=authority.project_id, session_id=authority.session_id,
            actor=actor, actor_role=cast(Literal['super_admin', 'admin', 'operator'], authority.role.value),
            request_sha256=authority.request_sha256,
            permissions_sha256=hashlib.sha256(_encoded(bound)).hexdigest())
    except (ValueError, TypeError, AttributeError, KeyError, RuntimeError):
        raise ValueError(_INVALID) from None


def metadata_state_plan(value: object) -> PythonMetadataPlan:
    """Validate durable exact shapes; executing/unknown outcomes never become planned."""
    try:
        if not isinstance(value, Mapping) or len(_encoded(value)) > 32 * 1024:
            raise ValueError
        phase = value.get("phase")
        keys = {"schema_version", "phase", "plan", "plan_sha256"}
        if phase in {"executing", "prepared"}:
            keys.add("execution_id")
            if (type(value.get("execution_id")) is not str
                    or re.fullmatch(r"metadata_prepare_[a-f0-9]{32}", value["execution_id"]) is None):
                raise ValueError
        if phase == "prepared":
            keys.add("receipt")
        if (set(value) != keys or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1 or phase not in {"planned", "executing", "prepared"}):
            raise ValueError
        plan = PythonMetadataPlan.model_validate_json(_encoded(value["plan"]), strict=True)
        if plan.mode == "ready" or plan.sha256 != value["plan_sha256"]:
            raise ValueError
        if phase == "prepared":
            from agent_hub.skills.sandbox.python_metadata_preparation_protocol import (
                PythonMetadataPreparationReceipt,
            )

            receipt = PythonMetadataPreparationReceipt.model_validate_json(_encoded(value["receipt"]), strict=True)
            if receipt.plan != plan or receipt.preparation_execution_id != value["execution_id"]:
                raise ValueError
        return plan
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, OverflowError):
        raise ValueError("metadata_prepare_request_invalid") from None


def validate_metadata_transition(
    before: object, after: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue]:
    new = metadata_state_plan(after)
    if before is None:
        allowed = after["phase"] == "planned"
    else:
        old = metadata_state_plan(before)
        assert isinstance(before, Mapping)
        pair = (before["phase"], after["phase"])
        allowed = (pair == ("planned", "planned")
            or pair == ("planned", "executing") and old == new
            or pair == ("executing", "prepared") and old == new
                and before["execution_id"] == after["execution_id"]
            or pair == ("prepared", "prepared") and before == after)
    if not allowed:
        raise ValueError("metadata_prepare_state_changed")
    return _freeze_object(after, name="metadata preparation")


def metadata_preparation_request(plan: PythonMetadataPlan, *, idempotency_key: str) -> CapabilityRequest:
    if type(plan) is not PythonMetadataPlan:
        raise ValueError(_INVALID)
    plan = PythonMetadataPlan.model_validate(plan, strict=True)
    if plan.mode == "ready":
        raise ValueError(_INVALID)
    return CapabilityRequest(tenant_id=plan.scope.tenant_id, run_id=plan.scope.run_id,
        user_id=plan.scope.user_id, agent_id=plan.scope.actor, capability="command", operation="run",
        resource=METADATA_PREFIX + plan.sha256, idempotency_key=idempotency_key,
        arguments={"schema_version": 1, "purpose": "python_dependency_metadata", "plan_sha256": plan.sha256,
            "project_id": plan.scope.project_id, "workspace_session_id": plan.scope.session_id,
            "catalog_sha256": plan.catalog_sha256, "root_config_sha256": plan.root_config_sha256,
            "source_sha256": plan.draft.source_sha256, "catalog_count": plan.catalog_count,
            "catalog_bytes": plan.catalog_bytes, "download_count": plan.missing_count,
            "download_bytes": plan.missing_bytes})


def metadata_preparation_binding(
    record: object, *, planned: bool = False, live: bool = False,
) -> CapabilityRequest:
    try:
        route = getattr(record, "routing_decision", None)
        if not isinstance(route, Mapping):
            raise TypeError
        state = route.get(METADATA_KEY)
        plan = metadata_state_plan(state)
        if (not isinstance(state, Mapping) or planned and state["phase"] != "planned"
                or plan.scope != metadata_scope(record, actor=plan.scope.actor, live=live)):
            raise ValueError
        return metadata_preparation_request(plan, idempotency_key="metadata-preparation-binding")
    except (ValueError, TypeError, AttributeError, KeyError, RuntimeError):
        raise ValueError(_INVALID) from None
