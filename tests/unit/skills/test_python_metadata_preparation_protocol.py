"""v10 preparation values bind metadata only, including untrusted model trees."""

from __future__ import annotations

import importlib
import importlib.util
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import BaseModel

from agent_hub.skills.sandbox.project import ContractProjectInvocation, ProjectInvocation
from agent_hub.skills.sandbox.python_metadata_plan_protocol import (
    PythonMetadataPlan,
    PythonMetadataPlanningRequest,
)
from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileScope

MODULE = "agent_hub.skills.sandbox.python_metadata_preparation_protocol"
PREPARE = "prepare_python_dependency_metadata"
INSPECT = "inspect_python_dependency_metadata_preparation"
PREP_ID = "metadata_prepare_" + "a" * 32
INSPECT_ID = "metadata_inspect_" + "b" * 32
EMPTY = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
ERRORS = tuple("metadata_prepare_" + suffix for suffix in (
    "request_invalid", "unavailable", "plan_changed", "scope_changed", "catalog_changed",
    "draft_changed", "artifact_integrity", "quota", "timeout", "consumed", "unknown",
    "cleanup_incomplete", "metadata_untrusted",
))


def api() -> Any:
    assert importlib.util.find_spec(MODULE) is not None, "v10 preparation DTO protocol missing"
    return importlib.import_module(MODULE)


def planning(inspect: bool = False) -> PythonMetadataPlanningRequest:
    return PythonMetadataPlanningRequest.model_validate({
        "invocation": {
            "execution_id": INSPECT_ID if inspect else PREP_ID,
            "tenant_id": UUID(int=1), "run_id": UUID(int=2), "project_id": "example",
            "session_id": "session_1", "sandbox_profile": "restricted",
            "install_allowed": False, "timeout_seconds": 120,
        },
        "scope": {
            "tenant_id": UUID(int=1), "run_id": UUID(int=2), "user_id": UUID(int=3),
            "project_id": "example", "session_id": "session_1", "actor": "agent-1",
            "actor_role": "operator", "request_sha256": "a" * 64,
            "permissions_sha256": "b" * 64,
        },
        "draft": {name: char * 64 for name, char in (
            ("source_sha256", "a"), ("acceptance_sha256", "b"),
            ("manifest_sha256", "c"), ("lock_sha256", "d"),
        )},
        "expected_root_config_sha256": "c" * 64, "expected_catalog_sha256": "d" * 64,
    }, strict=True)


def plan(mode: str = "download_required") -> PythonMetadataPlan:
    req = planning()
    return PythonMetadataPlan.model_validate({
        "scope": req.scope, "draft": req.draft, "root_config_sha256": "c" * 64,
        "catalog_sha256": "d" * 64, "catalog_identity_sha256": "e" * 64,
        "catalog_count": 2, "catalog_bytes": 200,
        "missing_identity_sha256": "f" * 64 if mode == "download_required" else EMPTY,
        "missing_count": 1 if mode == "download_required" else 0,
        "missing_bytes": 100 if mode == "download_required" else 0,
        "max_total_bytes": 64 * 1024**2, "wheel_max_bytes": 64 * 1024**2,
        "min_free_bytes": 0, "fetch_timeout_seconds": 30, "mode": mode,
        "metadata_index_sha256": "1" * 64 if mode == "ready" else None,
    }, strict=True)


def request(inspect: bool = False, **changes: Any) -> Any:
    return api().PythonMetadataPreparationRequest.model_validate({
        "action": INSPECT if inspect else PREPARE, "planning": planning(inspect),
        "expected_plan_sha256": plan().sha256,
        "preparation_execution_id": PREP_ID if inspect else None,
    } | changes, strict=True)


def receipt(**changes: Any) -> Any:
    return api().PythonMetadataPreparationReceipt.model_validate({
        "plan": plan(), "preparation_execution_id": PREP_ID,
        "index_sha256": "1" * 64, "index_record_count": 2,
    } | changes, strict=True)


def status(**changes: Any) -> Any:
    return api().PythonMetadataPreparationStatus.model_validate({
        "state": "prepared", "plan_sha256": plan().sha256,
        "preparation_execution_id": PREP_ID, "receipt": receipt(),
    } | changes, strict=True)


def response(inspect: bool = False, **changes: Any) -> Any:
    return api().PythonMetadataPreparationResponse.model_validate({
        "action": INSPECT if inspect else PREPARE,
        "scope": PythonProjectProfileScope.for_invocation(planning(inspect).invocation),
        "ok": True, "receipt": None if inspect else receipt(),
        "status": status() if inspect else None,
    } | changes, strict=True)


def replace(model: Any, path: str, changes: dict[str, Any], forge: str = "copy") -> Any:
    if path:
        head, _, tail = path.partition(".")
        return model.model_copy(update={head: replace(getattr(model, head), tail, changes, forge)})
    if forge == "construct":
        result = type(model).model_construct(**(vars(model) | changes))
        result.__dict__.update(changes)
        return result
    return model.model_copy(update=changes)


@pytest.mark.parametrize("inspect", [False, True])
def test_round_trip_and_request_binding(inspect: bool) -> None:
    req, res = request(inspect), response(inspect)
    assert req.version == res.version == 10
    assert req.invocation == req.planning.invocation
    assert req.planning.purpose == "python_dependency_metadata"
    res.require_for(req)
    for obj in (req, res, receipt(), status()):
        assert type(obj).model_validate_json(obj.model_dump_json(), strict=True) == obj
        assert type(obj).model_validate(obj, strict=True) == obj
        assert obj.model_config["revalidate_instances"] == "always"
        with pytest.raises(ValueError):
            obj.unexpected = True
    assert res.model_dump().keys() == {
        "version", "action", "scope", "ok", "error", "receipt", "status",
    }


@pytest.mark.parametrize("inspect", [False, True])
@pytest.mark.parametrize("execution_id", [
    "metadata_1", "metadata_prepare_" + "a" * 31, "metadata_prepare_" + "a" * 33,
    "metadata_inspect_" + "b" * 31, "metadata_inspect_" + "b" * 33,
    "metadata_prepare_" + "g" * 32, "metadata_inspect_" + "g" * 32,
])
def test_request_rejects_wrong_invocation_ids(inspect: bool, execution_id: str) -> None:
    bad = replace(planning(inspect), "invocation", {"execution_id": execution_id})
    with pytest.raises(ValueError):
        request(inspect, planning=bad)


@pytest.mark.parametrize("inspect", [False, True])
def test_action_execution_id_namespaces_cannot_be_swapped(inspect: bool) -> None:
    with pytest.raises(ValueError):
        request(inspect, planning=planning(not inspect))


@pytest.mark.parametrize("value", [None, INSPECT_ID, "metadata_prepare_a", "../bad", True])
def test_inspect_requires_preparation_id(value: Any) -> None:
    with pytest.raises(ValueError):
        request(True, preparation_execution_id=value)


def test_prepare_forbids_preparation_id_and_install_authority() -> None:
    with pytest.raises(ValueError):
        request(preparation_execution_id=PREP_ID)
    contract = ContractProjectInvocation.model_validate(vars(planning().invocation) | {
        "source_kind": "original", "original_reference": {
            "contract_sha256": "a" * 64, "input_source_sha256": "b" * 64, "file_count": 1,
        },
    }, strict=True)
    with pytest.raises(ValueError):
        request(planning=planning().model_copy(update={"invocation": contract}))


@pytest.mark.parametrize("mode", ["publish_cached", "download_required"])
def test_receipt_binds_index_to_nonready_plan(mode: str) -> None:
    obj = receipt(plan=plan(mode))
    assert obj.purpose == "python_dependency_metadata"
    assert obj.index_record_count == obj.plan.catalog_count
    assert obj.plan.root_config_sha256 == planning().expected_root_config_sha256
    assert obj.plan.catalog_sha256 == planning().expected_catalog_sha256


@pytest.mark.parametrize("changes", [
    {"plan": "ready"}, {"schema_version": True}, {"schema_version": 1.0},
    {"index_record_count": True}, {"index_record_count": "2"},
    {"index_record_count": 0}, {"index_record_count": 1}, {"index_record_count": 1001},
    {"index_sha256": "A" * 64}, {"index_sha256": b"1" * 64},
    {"preparation_execution_id": INSPECT_ID}, {"preparation_execution_id": "metadata_prepare_a"},
    {"purpose": "install"},
])
def test_receipt_rejects_inconsistent_or_coerced_fields(changes: dict[str, Any]) -> None:
    if changes.get("plan") == "ready":
        changes = {"plan": plan("ready")}
    with pytest.raises(ValueError):
        receipt(**changes)


@pytest.mark.parametrize("state", ["absent", "in_progress", "unknown"])
def test_nonprepared_status_forbids_receipt(state: str) -> None:
    assert status(state=state, receipt=None).state == state
    with pytest.raises(ValueError):
        status(state=state)


@pytest.mark.parametrize("changes", [
    {"receipt": None}, {"plan_sha256": "f" * 64},
    {"preparation_execution_id": "metadata_prepare_" + "c" * 32},
    {"preparation_execution_id": INSPECT_ID}, {"state": "supplied"},
])
def test_prepared_status_requires_matching_hash_and_id(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        status(**changes)


@pytest.mark.parametrize("inspect", [False, True])
@pytest.mark.parametrize("code", ERRORS)
def test_failure_codes_are_finite_and_bound(inspect: bool, code: str) -> None:
    obj = response(inspect, ok=False, receipt=None, status=None, error=code)
    obj.require_for(request(inspect))
    with pytest.raises(ValueError):
        obj.require_for(request(not inspect))


@pytest.mark.parametrize("inspect", [False, True])
@pytest.mark.parametrize("changes", [
    {"ok": 1}, {"ok": "true"}, {"version": True}, {"version": 10.0}, {"version": "10"},
    {"ok": False}, {"error": "private exception"}, {"error": "metadata_prepare_timeout"},
    {"receipt": None, "status": None}, {"receipt": "receipt", "status": "status"},
    {"action": "plan_python_dependency_metadata"},
])
def test_response_requires_exclusive_outcome(inspect: bool, changes: dict[str, Any]) -> None:
    if "receipt" in changes and changes["receipt"] == "receipt":
        changes = {"receipt": receipt(), "status": status()}
    with pytest.raises(ValueError):
        response(inspect, **changes)


@pytest.mark.parametrize("inspect", [False, True])
@pytest.mark.parametrize("field", [
    "original_reference", "recipe", "install_allowed", "url", "urls", "artifacts", "plan",
])
def test_foreign_authority_and_artifact_fields_are_forbidden(inspect: bool, field: str) -> None:
    for obj in (request(inspect), response(inspect), receipt(), status()):
        with pytest.raises(ValueError):
            type(obj).model_validate(vars(obj) | {field: {}}, strict=True)


@pytest.mark.parametrize("inspect", [False, True])
@pytest.mark.parametrize("field", [
    "tenant_id", "run_id", "project_id", "session_id", "execution_id",
])
def test_every_transport_scope_field_is_bound(inspect: bool, field: str) -> None:
    value = UUID(int=99) if field in {"tenant_id", "run_id"} else "foreign"
    candidate = PythonProjectProfileScope.model_validate(
        vars(response(inspect).scope) | {field: value}, strict=True,
    )
    with pytest.raises(ValueError):
        response(inspect, scope=candidate).require_for(request(inspect))


@pytest.mark.parametrize("inspect", [False, True])
@pytest.mark.parametrize("path,field", [
    *(("scope", field) for field in vars(planning().scope)),
    *(("draft", field) for field in vars(planning().draft)),
    ("", "root_config_sha256"), ("", "catalog_sha256"),
])
def test_receipt_plan_is_bound_beyond_expected_hash(inspect: bool, path: str, field: str) -> None:
    value: Any = (UUID(int=99) if field.endswith("_id") and field not in {
        "project_id", "session_id",
    } else "f" * 64 if field.endswith("sha256") else "admin" if field == "actor_role"
        else "foreign")
    foreign = replace(plan(), path, {field: value})
    foreign_receipt = receipt(plan=foreign)
    req = request(inspect, expected_plan_sha256=foreign.sha256)
    if inspect:
        res = response(True, status=status(plan_sha256=foreign.sha256, receipt=foreign_receipt))
    else:
        res = response(receipt=foreign_receipt)
    with pytest.raises(ValueError):
        res.require_for(req)


@pytest.mark.parametrize("inspect", [False, True])
def test_expected_plan_hash_and_preparation_identity_are_bound(inspect: bool) -> None:
    with pytest.raises(ValueError):
        response(inspect).require_for(request(inspect, expected_plan_sha256="f" * 64))
    other_id = "metadata_prepare_" + "c" * 32
    if inspect:
        req = request(True, preparation_execution_id=other_id)
    else:
        req = request(planning=replace(planning(), "invocation", {"execution_id": other_id}))
    with pytest.raises(ValueError):
        response(inspect).require_for(req)


@pytest.mark.parametrize("forge", ["copy", "construct"])
@pytest.mark.parametrize("target,path,changes", [
    ("request", "", {"version": True}), ("request", "", {"version": 10.0}),
    ("request", "", {"expected_plan_sha256": "bad"}),
    ("request", "", {"recipe": {}}),
    ("request", "planning", {"version": True}),
    ("request", "planning", {"purpose": "install"}),
    ("request", "planning", {"plan_artifacts": []}),
    ("request", "planning.invocation", {"install_allowed": True}),
    ("request", "planning.invocation", {"sandbox_profile": "workspace_write"}),
    ("request", "planning.invocation", {"timeout_seconds": True}),
    ("request", "planning.invocation", {"url": "https://foreign.invalid"}),
    ("request", "planning.scope", {"actor_role": "viewer"}),
    ("request", "planning.scope", {"extra": True}),
    ("request", "planning.draft", {"source_sha256": "bad"}),
    ("request", "planning.draft", {"extra": True}),
    ("response", "", {"version": True}), ("response", "", {"ok": 1}),
    ("response", "", {"status": None, "receipt": None}),
    ("response", "", {"extra": True}),
    ("response", "scope", {"extra": True}),
    ("response", "scope", {"execution_id": "../bad"}),
    ("response", "status", {"state": "unknown"}),
    ("response", "status", {"extra": True}),
    ("response", "status.receipt", {"index_record_count": True}),
    ("response", "status.receipt", {"schema_version": True}),
    ("response", "status.receipt", {"extra": True}),
    ("response", "status.receipt.plan", {"schema_version": True}),
    ("response", "status.receipt.plan", {"extractor_version": True}),
    ("response", "status.receipt.plan", {"catalog_count": True}),
    ("response", "status.receipt.plan", {"extra": True}),
    ("response", "status.receipt.plan.scope", {"actor": "bad actor"}),
    ("response", "status.receipt.plan.scope", {"extra": True}),
    ("response", "status.receipt.plan.draft", {"lock_sha256": "bad"}),
    ("response", "status.receipt.plan.draft", {"extra": True}),
])
def test_require_for_revalidates_entire_forged_tree(
    forge: str, target: str, path: str, changes: dict[str, Any],
) -> None:
    req, res = request(True), response(True)
    if target == "request":
        req = replace(req, path, changes, forge)
    else:
        res = replace(res, path, changes, forge)
    with pytest.raises(ValueError):
        res.require_for(req)


@pytest.mark.parametrize("factory", [request, response, receipt, status])
def test_direct_revalidation_rejects_copied_extra_fields(factory: Any) -> None:
    obj = factory().model_copy(update={"extra": True})
    with pytest.raises(ValueError):
        type(obj).model_validate(obj, strict=True)


def test_exact_types_are_required_at_request_response_and_nested_boundaries() -> None:
    for obj in (request(), response(), planning(), planning().invocation, receipt(), plan()):
        subclass = cast(type[BaseModel], type("ForeignDTO", (type(obj),), {}))
        foreign = subclass.model_construct(**vars(obj))
        with pytest.raises(ValueError):
            if isinstance(obj, PythonMetadataPlanningRequest):
                request(planning=foreign)
            elif isinstance(obj, ProjectInvocation):
                request(planning=planning().model_copy(update={"invocation": foreign}))
            elif isinstance(obj, PythonMetadataPlan):
                receipt(plan=foreign)
            elif isinstance(obj, api().PythonMetadataPreparationReceipt):
                response(receipt=foreign)
            elif isinstance(obj, api().PythonMetadataPreparationRequest):
                response().require_for(foreign)
            else:
                cast(Any, foreign).require_for(request())
    for invalid_request in ({}, object()):
        with pytest.raises(ValueError):
            response().require_for(invalid_request)


@pytest.mark.parametrize("state", ["absent", "in_progress", "unknown"])
def test_inspect_binds_nonprepared_status_without_receipt(state: str) -> None:
    res = response(True, status=status(state=state, receipt=None))
    res.require_for(request(True))
    with pytest.raises(ValueError):
        res.require_for(request(True, expected_plan_sha256="f" * 64))
    with pytest.raises(ValueError):
        res.require_for(request(True, preparation_execution_id="metadata_prepare_" + "c" * 32))


@pytest.mark.parametrize("count", [1, 1000])
def test_receipt_accepts_record_count_boundaries(count: int) -> None:
    candidate = plan("publish_cached").model_copy(update={
        "catalog_count": count, "catalog_bytes": count,
    })
    assert receipt(plan=candidate, index_record_count=count).index_record_count == count


@pytest.mark.parametrize("path", ["receipt", "status", "status.receipt"])
def test_nested_v10_subclasses_are_rejected(path: str) -> None:
    res = response(path != "receipt")
    nested = res
    for field in path.split("."):
        nested = getattr(nested, field)
    subclass = cast(type[BaseModel], type("ForeignDTO", (type(nested),), {}))
    forged = subclass.model_construct(**vars(nested))
    if "." in path:
        res = replace(res, "status", {"receipt": forged})
    else:
        res = res.model_copy(update={path: forged})
    with pytest.raises(ValueError):
        res.require_for(request(path != "receipt"))


@pytest.mark.parametrize("inspect", [False, True])
def test_failure_cannot_include_success_payload(inspect: bool) -> None:
    with pytest.raises(ValueError):
        response(inspect, ok=False, error="metadata_prepare_timeout")


@pytest.mark.parametrize("inspect", [False, True])
def test_success_cannot_include_payload_for_other_action(inspect: bool) -> None:
    with pytest.raises(ValueError):
        response(inspect, receipt=receipt() if inspect else None,
                 status=None if inspect else status())
