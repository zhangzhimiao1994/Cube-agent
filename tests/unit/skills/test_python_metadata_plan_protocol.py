"""Read-only metadata plans bind approval content without transport authority."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest

from agent_hub.skills.sandbox.project import ContractProjectInvocation, ProjectInvocation
from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileScope

MODULE = "agent_hub.skills.sandbox.python_metadata_plan_protocol"
EMPTY = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
MIB = 1024**2
ERRORS = (
    "metadata_plan_request_invalid", "metadata_plan_unavailable", "metadata_plan_draft_changed",
    "metadata_plan_catalog_changed", "metadata_plan_metadata_untrusted",
    "metadata_plan_cache_untrusted", "metadata_plan_quota", "metadata_plan_timeout",
)


def api() -> Any:
    assert importlib.util.find_spec(MODULE) is not None, "metadata plan DTO protocol missing"
    return importlib.import_module(MODULE)


def invocation(**changes: Any) -> ProjectInvocation:
    fields: dict[str, Any] = {
        "execution_id": "metadata_1", "tenant_id": UUID(int=1), "run_id": UUID(int=2),
        "project_id": "example", "session_id": "session_1", "sandbox_profile": "restricted",
        "install_allowed": False, "timeout_seconds": 120,
    }
    return ProjectInvocation.model_validate(fields | changes, strict=True)


def scope_fields() -> dict[str, Any]:
    return {
        "tenant_id": UUID(int=1), "run_id": UUID(int=2), "user_id": UUID(int=3),
        "project_id": "example", "session_id": "session_1", "actor": "generated.agent-1",
        "actor_role": "operator", "request_sha256": "a" * 64, "permissions_sha256": "b" * 64,
    }


def draft_fields() -> dict[str, Any]:
    return {name: char * 64 for name, char in (
        ("source_sha256", "a"), ("acceptance_sha256", "b"),
        ("manifest_sha256", "c"), ("lock_sha256", "d"),
    )}


def plan_fields(mode: str = "download_required") -> dict[str, Any]:
    return {
        "schema_version": 1, "purpose": "python_dependency_metadata", "scope": scope_fields(),
        "draft": draft_fields(), "root_config_sha256": "c" * 64, "catalog_sha256": "d" * 64,
        "catalog_identity_sha256": "e" * 64, "catalog_count": 2, "catalog_bytes": 200,
        "missing_identity_sha256": "f" * 64 if mode == "download_required" else EMPTY,
        "missing_count": 1 if mode == "download_required" else 0,
        "missing_bytes": 100 if mode == "download_required" else 0,
        "max_total_bytes": 64 * MIB, "wheel_max_bytes": 64 * MIB, "min_free_bytes": 0,
        "fetch_timeout_seconds": 30, "extractor_version": 1, "mode": mode,
        "metadata_index_sha256": "1" * 64 if mode == "ready" else None,
    }


def request(**changes: Any) -> Any:
    fields = {
        "invocation": invocation(), "scope": scope_fields(), "draft": draft_fields(),
        "expected_root_config_sha256": "c" * 64, "expected_catalog_sha256": "d" * 64,
    }
    return api().PythonMetadataPlanningRequest.model_validate(fields | changes, strict=True)


def response(**changes: Any) -> Any:
    fields = {
        "scope": PythonProjectProfileScope.for_invocation(invocation()), "ok": True,
        "plan": plan_fields(),
    }
    return api().PythonMetadataPlanningResponse.model_validate(fields | changes, strict=True)


def test_protocol_is_available() -> None:
    api()


@pytest.mark.parametrize("mode", ["ready", "publish_cached", "download_required"])
def test_canonical_plan_round_trip_and_hash(mode: str) -> None:
    cls = api().PythonMetadataPlan
    plan = cls.model_validate(plan_fields(mode), strict=True)
    encoded = json.dumps(plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("ascii")
    assert plan.sha256 == hashlib.sha256(encoded).hexdigest()
    assert cls.model_validate_json(encoded, strict=True) == plan
    assert len(encoded) < 4096
    assert plan.sha256 == cls.model_validate(dict(reversed(list(plan_fields(mode).items())))).sha256


def test_transport_execution_and_deadline_do_not_change_approval_hash() -> None:
    first = request()
    second = request(invocation=invocation(execution_id="metadata_retry", timeout_seconds=1))
    plan = response().plan
    assert plan is not None
    bound = response(scope=PythonProjectProfileScope.for_invocation(second.invocation), plan=plan)
    bound.require_for(second)
    assert plan.sha256 == response().plan.sha256
    assert first.scope == second.scope
    with pytest.raises(ValueError):
        bound.require_for(first)


@pytest.mark.parametrize("field,valid,invalid", [
    ("catalog_count", 1000, [0, -1, 1001]),
    ("catalog_bytes", 2**63 - 1, [0, -1, 2**63]),
    ("missing_count", 0, [-1, 1001]),
    ("missing_bytes", 0, [-1, 2**63]),
    ("max_total_bytes", 64 * MIB, [0, -1, 64 * MIB + 1]),
    ("wheel_max_bytes", 2**63 - 1, [0, -1, 2**63]),
    ("min_free_bytes", 2**63 - 1, [-1, 2**63]),
    ("fetch_timeout_seconds", 600, [0, -1, 601]),
])
def test_integer_boundaries(field: str, valid: int, invalid: list[int]) -> None:
    cls = api().PythonMetadataPlan
    fields = plan_fields("ready")
    if field == "catalog_count":
        fields["catalog_bytes"] = 1000
    assert getattr(cls.model_validate(fields | {field: valid}), field) == valid
    for value in [*invalid, True, False, 1.0, "1", None]:
        with pytest.raises(ValueError):
            cls.model_validate(fields | {field: value}, strict=True)


@pytest.mark.parametrize("field", ["schema_version", "extractor_version"])
@pytest.mark.parametrize("value", [True, False, 1.0, "1", 0, 2])
def test_plan_versions_are_strict_in_python_and_json(field: str, value: object) -> None:
    cls = api().PythonMetadataPlan
    fields = plan_fields() | {field: value}
    with pytest.raises(ValueError):
        cls.model_validate(fields, strict=True)
    with pytest.raises(ValueError):
        cls.model_validate_json(json.dumps(fields, default=str), strict=True)


@pytest.mark.parametrize("mode,changes", [
    ("ready", {"metadata_index_sha256": None}),
    ("ready", {"missing_count": 1}), ("ready", {"missing_bytes": 1}),
    ("ready", {"missing_identity_sha256": "f" * 64}),
    ("publish_cached", {"metadata_index_sha256": "1" * 64}),
    ("publish_cached", {"missing_count": 1}), ("publish_cached", {"missing_bytes": 1}),
    ("publish_cached", {"missing_identity_sha256": "f" * 64}),
    ("download_required", {"metadata_index_sha256": "1" * 64}),
    ("download_required", {"missing_count": 0}),
    ("download_required", {"missing_count": 3}),
    ("download_required", {"missing_bytes": 0}),
    ("download_required", {"missing_bytes": 201}),
    ("download_required", {"missing_identity_sha256": EMPTY}),
])
def test_mode_missing_identity_and_counts_are_consistent(mode: str, changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        api().PythonMetadataPlan.model_validate(plan_fields(mode) | changes)


@pytest.mark.parametrize("mode", ["publish_cached", "download_required"])
@pytest.mark.parametrize("quota", ["max_total_bytes", "wheel_max_bytes"])
def test_only_nonready_plans_require_full_catalog_within_both_quotas(mode: str, quota: str) -> None:
    cls = api().PythonMetadataPlan
    assert cls.model_validate(plan_fields(mode) | {quota: 200}).catalog_bytes == 200
    with pytest.raises(ValueError):
        cls.model_validate(plan_fields(mode) | {quota: 199})
    assert cls.model_validate(plan_fields("ready") | {
        "catalog_bytes": 2**63 - 1, "max_total_bytes": 1, "wheel_max_bytes": 1,
    }).mode == "ready"


@pytest.mark.parametrize("changes", [
    {"missing_count": 2, "missing_bytes": 200, "missing_identity_sha256": "e" * 64},
    {"catalog_bytes": 64 * MIB, "missing_bytes": 64 * MIB - 1, "wheel_max_bytes": 2**63 - 1},
    {"max_total_bytes": 1, "wheel_max_bytes": 1, "catalog_count": 1, "catalog_bytes": 1,
     "missing_count": 1, "missing_bytes": 1, "fetch_timeout_seconds": 1,
     "missing_identity_sha256": "e" * 64},
])
def test_valid_download_limits(changes: dict[str, Any]) -> None:
    assert api().PythonMetadataPlan.model_validate(plan_fields() | changes).mode == "download_required"


@pytest.mark.parametrize("changes", [
    {"catalog_identity_sha256": EMPTY},
    {"missing_count": 2, "missing_bytes": 199, "missing_identity_sha256": "e" * 64},
    {"missing_count": 2, "missing_bytes": 200},
    {"missing_bytes": 200},
    {"missing_identity_sha256": "e" * 64},
    {"catalog_count": 201},
    {"missing_count": 101},
    {"catalog_count": 100, "missing_bytes": 150},
])
@pytest.mark.parametrize("entry", ["python", "json", "hash", "response"])
def test_impossible_catalog_subset_arithmetic_is_rejected(
    changes: dict[str, Any], entry: str,
) -> None:
    cls = api().PythonMetadataPlan
    with pytest.raises(ValueError):
        if entry == "python":
            cls.model_validate(plan_fields() | changes)
        elif entry == "json":
            cls.model_validate_json(json.dumps(plan_fields() | changes, default=str))
        else:
            forged = cls.model_validate(plan_fields()).model_copy(update=changes)
            if entry == "hash":
                _ = forged.sha256
            else:
                response().model_copy(update={"plan": forged}).require_for(request())


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "00000000-0000-0000-0000-000000000001"),
    ("run_id", 2), ("user_id", "invalid"),
    ("project_id", "../example"), ("project_id", "x" * 65), ("project_id", "Example"),
    ("session_id", ""), ("session_id", ".hidden"), ("session_id", "x" * 65),
    ("actor", "../actor"), ("actor", "A"), ("actor", "x" * 129), ("actor", "a\n"),
    ("actor_role", "viewer"), ("actor_role", True),
    ("request_sha256", "A" * 64), ("permissions_sha256", "a" * 63),
])
def test_scope_rejects_unsafe_or_coerced_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        api().PythonMetadataScope.model_validate(scope_fields() | {field: value})


@pytest.mark.parametrize("role", ["super_admin", "admin", "operator"])
def test_scope_allows_generated_actor_and_roles(role: str) -> None:
    scope = api().PythonMetadataScope.model_validate(scope_fields() | {
        "actor": "a" + "_.-0" * 31 + "xyz", "actor_role": role,
        "project_id": "a" * 64, "session_id": "0" + "_-0" * 21,
    })
    assert len(scope.actor) == 128


@pytest.mark.parametrize("field", ["tenant_id", "run_id", "project_id", "session_id"])
def test_scope_requires_invocation_binding(field: str) -> None:
    scope = api().PythonMetadataScope.model_validate(scope_fields())
    scope.require_invocation(invocation())
    changed = UUID(int=99) if field in {"tenant_id", "run_id"} else "foreign"
    with pytest.raises(ValueError):
        scope.require_invocation(invocation(**{field: changed}))
    with pytest.raises(ValueError):
        request(scope=scope_fields() | {field: changed})


@pytest.mark.parametrize("kind", ["PythonMetadataDraft", "PythonMetadataPlan"])
def test_all_digest_fields_reject_malformed_values(kind: str) -> None:
    cls = getattr(api(), kind)
    fields = draft_fields() if kind == "PythonMetadataDraft" else plan_fields("ready")
    for field in fields:
        if not field.endswith("sha256"):
            continue
        for bad in ["", "A" * 64, "a" * 63, "a" * 65, "g" * 64, True, 123]:
            with pytest.raises(ValueError):
                cls.model_validate(fields | {field: bad})


@pytest.mark.parametrize("kind", ["scope", "draft", "plan", "request", "response"])
@pytest.mark.parametrize("extra", [
    "original_reference", "recipe", "url", "urls", "artifacts", "install_allowed",
    "execution_id", "remaining_timeout_seconds",
])
def test_closed_shapes_reject_execution_or_supply_fields(kind: str, extra: str) -> None:
    module = api()
    factories: dict[str, Callable[[], Any]] = {
        "scope": lambda: module.PythonMetadataScope.model_validate(scope_fields()),
        "draft": lambda: module.PythonMetadataDraft.model_validate(draft_fields()),
        "plan": lambda: module.PythonMetadataPlan.model_validate(plan_fields()),
        "request": request, "response": response,
    }
    model = factories[kind]()
    with pytest.raises(ValueError):
        type(model).model_validate(model.model_dump() | {extra: []})
    with pytest.raises(ValueError):
        setattr(model, next(iter(type(model).model_fields)), None)


@pytest.mark.parametrize("kind", ["request", "response"])
@pytest.mark.parametrize("value", [True, False, 9.0, "9", 1, 8, 10])
def test_v9_envelope_rejects_invalid_version(kind: str, value: object) -> None:
    model = request() if kind == "request" else response()
    with pytest.raises(ValueError):
        type(model).model_validate(model.model_dump() | {"version": value})
    with pytest.raises(ValueError):
        type(model).model_validate_json(json.dumps(model.model_dump(mode="json") | {"version": value}))


def test_request_and_response_round_trip() -> None:
    req = request()
    res = response()
    assert req.version == res.version == 9
    assert req.action == res.action == "plan_python_dependency_metadata"
    assert req.purpose == "python_dependency_metadata"
    assert type(req.invocation) is ProjectInvocation
    req = type(req).model_validate_json(req.model_dump_json(), strict=True)
    res = type(res).model_validate_json(res.model_dump_json(), strict=True)
    res.require_for(req)


@pytest.mark.parametrize("changes", [
    {"sandbox_profile": "workspace_write"}, {"install_allowed": True},
    {"install_allowed": 0}, {"timeout_seconds": True}, {"timeout_seconds": 0},
    {"timeout_seconds": 3601}, {"tenant_id": "invalid"}, {"original_reference": {}},
])
@pytest.mark.parametrize("forge", ["dict", "copy", "construct"])
def test_request_revalidates_base_invocation(changes: dict[str, Any], forge: str) -> None:
    base = invocation()
    value = base.model_dump() | changes
    candidate = (base.model_copy(update=changes) if forge == "copy" else
                 ProjectInvocation.model_construct(**value) if forge == "construct" else value)
    # model_construct silently discards forbidden extras, so retain them in __dict__.
    if forge == "construct":
        candidate.__dict__.update(changes)
    with pytest.raises(ValueError):
        request(invocation=candidate)


def test_request_rejects_contract_subclass_even_after_copy() -> None:
    contract = ContractProjectInvocation.model_validate(invocation().model_dump() | {
        "source_kind": "original",
        "original_reference": {"contract_sha256": "a" * 64, "input_source_sha256": "b" * 64,
                               "file_count": 1},
    }, strict=True)
    with pytest.raises(ValueError):
        request(invocation=contract)
    with pytest.raises(ValueError):
        response().require_for(request().model_copy(update={"invocation": contract}))


@pytest.mark.parametrize("code", ERRORS)
def test_failure_codes_are_finite_and_scope_bound(code: str) -> None:
    failed = response(ok=False, error=code, plan=None)
    failed.require_for(request())
    assert failed.error == code
    with pytest.raises(ValueError):
        failed.require_for(request(invocation=invocation(execution_id="different")))


@pytest.mark.parametrize("changes", [
    {"ok": True, "plan": None}, {"ok": True, "error": "metadata_plan_timeout"},
    {"ok": False}, {"ok": False, "plan": None},
    {"ok": False, "error": "metadata_plan_timeout"},
    {"ok": False, "plan": None, "error": "private_path_or_unknown_error"},
    {"ok": 1}, {"ok": "true"}, {"action": "supply_python_project_inputs"},
])
def test_response_outcomes_are_exclusive(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        response(**changes)


@pytest.mark.parametrize("field", [
    "tenant_id", "run_id", "project_id", "session_id", "execution_id",
])
def test_response_requires_all_five_transport_scope_fields(field: str) -> None:
    scope = PythonProjectProfileScope.for_invocation(invocation()).model_dump()
    scope[field] = UUID(int=99) if field in {"tenant_id", "run_id"} else "foreign"
    with pytest.raises(ValueError):
        response(scope=scope).require_for(request())


@pytest.mark.parametrize("field", list(scope_fields()))
def test_response_binds_every_plan_scope_field(field: str) -> None:
    fields = scope_fields()
    fields[field] = UUID(int=99) if field in {"tenant_id", "run_id", "user_id"} else (
        "f" * 64 if field.endswith("sha256") else "admin" if field == "actor_role" else "foreign")
    with pytest.raises(ValueError):
        response(plan=plan_fields() | {"scope": fields}).require_for(request())


@pytest.mark.parametrize("field", list(draft_fields()))
def test_response_binds_every_draft_digest(field: str) -> None:
    with pytest.raises(ValueError):
        response(plan=plan_fields() | {
            "draft": draft_fields() | {field: "f" * 64},
        }).require_for(request())


@pytest.mark.parametrize("field", ["root_config_sha256", "catalog_sha256"])
def test_response_binds_expected_root_and_catalog(field: str) -> None:
    with pytest.raises(ValueError):
        response(plan=plan_fields() | {field: "f" * 64}).require_for(request())


@pytest.mark.parametrize("forge", ["copy", "construct"])
@pytest.mark.parametrize("target,changes", [
    ("request", {"version": True}), ("request", {"action": "run_project"}),
    ("request", {"purpose": "install"}), ("request", {"recipe": {}}),
    ("invocation", {"install_allowed": True}), ("invocation", {"timeout_seconds": True}),
    ("invocation", {"sandbox_profile": "workspace_write"}),
    ("invocation", {"url": "https://untrusted.invalid"}),
    ("request_scope", {"actor_role": "viewer"}), ("request_scope", {"tenant_id": UUID(int=99)}),
    ("request_draft", {"source_sha256": "bad"}),
    ("response", {"version": True}), ("response", {"ok": 1}),
    ("response", {"ok": False}), ("response", {"error": "unknown"}),
    ("response", {"error": "metadata_plan_timeout"}), ("response", {"plan": None}),
    ("response", {"artifacts": []}),
    ("response_scope", {"execution_id": "../bad"}),
    ("response_scope", {"extra": True}),
    ("plan", {"schema_version": True}), ("plan", {"extractor_version": True}),
    ("plan", {"catalog_count": True}), ("plan", {"missing_count": 0}),
    ("plan", {"catalog_bytes": 2**63}), ("plan", {"mode": "install"}),
    ("plan", {"remaining_timeout_seconds": 1}),
    ("plan_scope", {"actor": "bad actor"}), ("plan_draft", {"lock_sha256": "bad"}),
])
def test_require_for_revalidates_forged_model_trees(
    forge: str, target: str, changes: dict[str, Any],
) -> None:
    req, res = request(), response()
    targets = {
        "request": req, "invocation": req.invocation, "request_scope": req.scope,
        "request_draft": req.draft, "response": res, "response_scope": res.scope,
        "plan": res.plan, "plan_scope": res.plan.scope, "plan_draft": res.plan.draft,
    }
    original = targets[target]
    forged = (original.model_copy(update=changes) if forge == "copy" else
              type(original).model_construct(**(original.model_dump() | changes)))
    if forge == "construct":
        forged.__dict__.update(changes)
    if target == "request":
        req = forged
    elif target in {"invocation", "request_scope", "request_draft"}:
        req = req.model_copy(update={target.removeprefix("request_"): forged})
    elif target == "response":
        res = forged
    elif target == "response_scope":
        res = res.model_copy(update={"scope": forged})
    elif target == "plan":
        res = res.model_copy(update={"plan": forged})
    else:
        res = res.model_copy(update={
            "plan": res.plan.model_copy(update={target.removeprefix("plan_"): forged}),
        })
    with pytest.raises(ValueError):
        res.require_for(req)


def test_hash_rejects_forged_plan_instead_of_approving_it() -> None:
    plan = api().PythonMetadataPlan.model_validate(plan_fields())
    with pytest.raises(ValueError):
        _ = plan.model_copy(update={"catalog_count": True}).sha256
