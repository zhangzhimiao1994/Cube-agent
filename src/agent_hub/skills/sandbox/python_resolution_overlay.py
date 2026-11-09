"""Root-only immutable metadata selection. A binding selects data, never a grant."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import cast

from agent_hub.skills.sandbox import python_dependency_index_store as indexes
from agent_hub.skills.sandbox.project import ProjectInvocation
from agent_hub.skills.sandbox.python_input_supply_catalog import (
    RootSupplyConfig,
    read_supply_configuration,
)
from agent_hub.skills.sandbox.python_project_backend import read_root_configuration
from agent_hub.skills.sandbox.python_registry_protocol import (
    ResolutionDescriptor,
    digest,
    need,
    require_sha,
)
from agent_hub.skills.sandbox.python_resolution_protocol import (
    ResolutionClaim,
    ResolutionPlan,
    decode_descriptor,
    encoded,
    resolution_key,
)
from agent_hub.skills.sandbox.python_resolution_store import ResolutionStore, overlay_supply


def _check(deadline: float) -> None:
    indexes._check(deadline)


def _scope(invocation: ProjectInvocation, source_sha256: str) -> dict[str, str]:
    require_sha(source_sha256)
    # Only already validated broker invocations enter this Root data API. Preserve
    # derived probe/reference fields at the caller; they do not confer authority.
    need(isinstance(invocation, ProjectInvocation), "resolution_overlay_scope")
    base = ProjectInvocation.model_validate(
        {name: getattr(invocation, name) for name in ProjectInvocation.model_fields}, strict=True
    )
    return {
        "tenant_id": str(base.tenant_id),
        "run_id": str(base.run_id),
        "project_id": base.project_id,
        "session_id": base.session_id,
        "source_sha256": source_sha256,
    }


def _name(scope: dict[str, str], kind: str) -> str:
    need(kind in {"attempt", "binding"})
    return "resolution-" + kind + "-" + digest(scope) + ".json"


def _immutable(store: ResolutionStore, name: str, value: object, deadline: float) -> None:
    try:
        store._create(name, value, deadline)
    except FileExistsError:
        need(store._read(name, deadline) == value, "resolution_overlay_conflict")


def record_attempt(
    base: RootSupplyConfig,
    invocation: ProjectInvocation,
    *,
    source_sha256: str,
    resolution_key: str,
    deadline: float,
) -> None:
    """Persist before network/once intent. Failure leaves a visible pending dataset."""
    indexes._native()
    _check(deadline)
    require_sha(resolution_key)
    scope = _scope(invocation, source_sha256)
    value = {
        "schema_version": 1,
        "purpose": "python_resolution_attempt",
        "scope": scope,
        "resolution_key": resolution_key,
        "base_catalog_sha256": base.digest,
    }
    _immutable(ResolutionStore(base), _name(scope, "attempt"), value, deadline)
    _check(deadline)


def record_frozen_plan(base: RootSupplyConfig, plan: ResolutionPlan, *, deadline: float) -> None:
    """Append a source binding; actual publication/result is independently required."""
    indexes._native()
    _check(deadline)
    plan = ResolutionPlan.model_validate(plan, strict=True)
    need(plan.claim.planning.expected_catalog_sha256 == base.digest, "resolution_overlay_catalog")
    scope = _scope(plan.claim.planning.invocation, plan.claim.planning.draft.source_sha256)
    record_attempt(
        base,
        plan.claim.planning.invocation,
        source_sha256=scope["source_sha256"],
        resolution_key=plan.resolution_key,
        deadline=deadline,
    )
    value = {
        "schema_version": 1,
        "purpose": "python_resolution_binding",
        "scope": scope,
        "resolution_key": plan.resolution_key,
        "plan_sha256": plan.sha256,
        "base_catalog_sha256": base.digest,
    }
    _immutable(ResolutionStore(base), _name(scope, "binding"), value, deadline)
    _check(deadline)


def _read(store: ResolutionStore, name: str, deadline: float) -> object | None:
    try:
        return store._read(name, deadline)
    except FileNotFoundError:
        return None


def has_overlay(
    base: RootSupplyConfig, invocation: ProjectInvocation, *, source_sha256: str, deadline: float
) -> bool:
    """Internal presence test only. Known malformed/pending entries never mean base."""
    indexes._native()
    _check(deadline)
    scope = _scope(invocation, source_sha256)
    store = ResolutionStore(base)
    return (
        _read(store, _name(scope, "binding"), deadline) is not None
        or _read(store, _name(scope, "attempt"), deadline) is not None
    )


@dataclass(frozen=True, slots=True)
class PublishedResolution:
    supply: RootSupplyConfig
    descriptor: ResolutionDescriptor
    claim: ResolutionClaim
    plan_sha256: str
    descriptor_sha256: str


def verified_resolution_publication(
    base: RootSupplyConfig, invocation: ProjectInvocation, *, source_sha256: str, deadline: float
) -> PublishedResolution | None:
    """Re-read Root evidence for this scope/source, without adopting its old authority.

    The caller supplies a Root-observed draft hash, or a verified original contract
    hash after capture. Current actor/lease/grants and actual declaration/closure
    remain mandatory at the action boundary; this function never authorizes them.
    """
    indexes._native()
    _check(deadline)
    base = replace(base)
    scope = _scope(invocation, source_sha256)
    store = ResolutionStore(base)
    binding = _read(store, _name(scope, "binding"), deadline)
    attempt = _read(store, _name(scope, "attempt"), deadline)
    if binding is None:
        need(attempt is None, "resolution_overlay_pending")
        return None
    need(
        type(binding) is dict
        and set(binding)
        == {
            "schema_version",
            "purpose",
            "scope",
            "resolution_key",
            "plan_sha256",
            "base_catalog_sha256",
        },
        "resolution_overlay_invalid",
    )
    item = cast(dict[str, object], binding)
    need(
        type(item["schema_version"]) is int
        and item["schema_version"] == 1
        and item["purpose"] == "python_resolution_binding"
        and item["scope"] == scope
        and item["base_catalog_sha256"] == base.digest,
        "resolution_overlay_scope",
    )
    key, plan_sha = cast(str, item["resolution_key"]), cast(str, item["plan_sha256"])
    require_sha(key)
    require_sha(plan_sha)
    need(
        attempt
        == {
            "schema_version": 1,
            "purpose": "python_resolution_attempt",
            "scope": scope,
            "resolution_key": key,
            "base_catalog_sha256": base.digest,
        },
        "resolution_overlay_pending",
    )
    plan = ResolutionPlan.model_validate_json(
        encoded(store._read(store._name(key, "plan-" + plan_sha), deadline)), strict=True
    )
    original = ResolutionClaim.model_validate_json(
        encoded(store._read(store._name(key, "intent"), deadline)), strict=True
    )
    descriptor = decode_descriptor(store._read(store._name(key, "receipt"), deadline))
    need(
        plan.sha256 == plan_sha
        and plan.resolution_key == key
        and plan.descriptor_sha256 == descriptor.sha256
        and _scope(plan.claim.planning.invocation, plan.claim.planning.draft.source_sha256) == scope
        and descriptor.scope == original.resolution_scope()
        and key == resolution_key(original, descriptor.needs, dict(descriptor.environment))
        and plan.claim.data_identity() == original.data_identity()
        and plan.claim.original_deadline <= original.original_deadline
        and plan.claim.monotonic_deadline <= original.monotonic_deadline,
        "resolution_overlay_scope",
    )
    need(
        original.planning.expected_catalog_sha256 == base.digest
        and read_supply_configuration().digest == base.digest
        and digest(read_root_configuration().to_payload())
        == plan.claim.planning.expected_root_config_sha256,
        "resolution_overlay_catalog",
    )
    effect = overlay_supply(base, descriptor)
    need(effect.digest == plan.overlay_sha256, "resolution_overlay_catalog")
    intent = _read(store, store._name(key, "publish-" + plan_sha), deadline)
    result = _read(store, store._name(key, "result-" + plan_sha), deadline)
    need(
        intent == {"state": "unknown", "plan_sha256": plan_sha} and type(result) is dict,
        "resolution_overlay_pending",
    )
    receipt = cast(dict[str, object], result)
    need(
        set(receipt) == {"plan_sha256", "index_sha256"} and receipt["plan_sha256"] == plan_sha,
        "resolution_overlay_invalid",
    )
    index_sha = cast(str, receipt["index_sha256"])
    require_sha(index_sha)
    index = indexes.read_dependency_metadata_index(effect, deadline=deadline)
    need(index.sha256 == index_sha, "resolution_overlay_index")
    need(
        read_supply_configuration().digest == base.digest
        and digest(read_root_configuration().to_payload())
        == plan.claim.planning.expected_root_config_sha256,
        "resolution_overlay_catalog",
    )
    _check(deadline)
    return PublishedResolution(effect, descriptor, original, plan_sha, plan.descriptor_sha256)


def effective_supply(
    base: RootSupplyConfig, invocation: ProjectInvocation, *, source_sha256: str, deadline: float
) -> RootSupplyConfig:
    result = verified_resolution_publication(base, invocation, source_sha256=source_sha256, deadline=deadline)
    return replace(base) if result is None else result.supply
