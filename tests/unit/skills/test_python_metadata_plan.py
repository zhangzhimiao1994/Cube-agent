"""Plan construction from trusted catalog values, not native cache observations."""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.skills.sandbox.project import ProjectInvocation
from agent_hub.skills.sandbox.python_dependency_metadata import (
    PythonDependencyMetadataIndex,
    extract_python_wheel_metadata,
)
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from tests.unit.skills.test_python_dependency_index_store import root_configuration
from tests.unit.skills.test_python_dependency_metadata import wheel


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


@pytest.fixture
def api() -> Any:
    module = "agent_hub.skills.sandbox.python_metadata_plan"
    assert importlib.util.find_spec(module) is not None, "pre-capture metadata plan builder is missing"
    return importlib.import_module(module)


def inputs() -> tuple[Any, Any, RootSupplyConfig, tuple[Any, ...]]:
    protocol = importlib.import_module("agent_hub.skills.sandbox.python_metadata_plan_protocol")
    root = root_configuration()
    entries = tuple(wheel(name=name)[0] for name in ("one", "two", "unused"))
    supply = RootSupplyConfig(root.wheel_root.parent / "supply", entries, 64 * 1024**2, 0, 60)
    invocation = ProjectInvocation(execution_id="metadata_plan_1", tenant_id=UUID(int=1),
        run_id=UUID(int=2), project_id="project", session_id="session", sandbox_profile="restricted",
        install_allowed=False, timeout_seconds=60)
    scope = protocol.PythonMetadataScope(tenant_id=invocation.tenant_id, run_id=invocation.run_id,
        user_id=UUID(int=3), project_id="project", session_id="session", actor="main_agent",
        actor_role="operator", request_sha256="a" * 64, permissions_sha256="b" * 64)
    draft = protocol.PythonMetadataDraft(source_sha256="c" * 64, acceptance_sha256="d" * 64,
        manifest_sha256="e" * 64, lock_sha256="f" * 64)
    request = protocol.PythonMetadataPlanningRequest(invocation=invocation, scope=scope, draft=draft,
        expected_root_config_sha256=digest(root.to_payload()), expected_catalog_sha256=supply.digest)
    return request, root, supply, entries


def test_builder_exists(api: Any) -> None:
    assert callable(api.build_python_metadata_plan)


def test_cold_catalog_plan_is_full_compact_and_has_no_install_or_url(api: Any) -> None:
    request, root, supply, entries = inputs()
    plan = api.build_python_metadata_plan(request, root=root, supply=supply, cached=())
    assert plan.mode == "download_required"
    assert (plan.catalog_count, plan.missing_count) == (3, 3)
    assert plan.catalog_bytes == plan.missing_bytes == sum(e.size_bytes for e in entries)
    assert plan.catalog_identity_sha256 == plan.missing_identity_sha256
    raw = plan.model_dump_json()
    assert len(raw) < 4096
    for forbidden in ("https://", "original_reference", "recipe", "install_allowed", "unused"):
        assert forbidden not in raw
    assert plan.scope == request.scope and plan.draft == request.draft
    assert plan.sha256 == digest(plan.model_dump(mode="json"))


def test_partial_cache_is_not_a_complete_metadata_index(api: Any) -> None:
    request, root, supply, entries = inputs()
    cold = api.build_python_metadata_plan(request, root=root, supply=supply, cached=())
    partial = api.build_python_metadata_plan(request, root=root, supply=supply, cached=entries[:2])
    assert partial.mode == "download_required" and partial.missing_count == 1
    assert partial.missing_bytes == entries[-1].size_bytes
    assert partial.catalog_identity_sha256 == cold.catalog_identity_sha256
    assert partial.missing_identity_sha256 != cold.missing_identity_sha256
    assert partial.metadata_index_sha256 is None
    assert partial.sha256 != cold.sha256


def test_complete_cache_needs_publication_not_installation(api: Any) -> None:
    request, root, supply, entries = inputs()
    plan = api.build_python_metadata_plan(request, root=root, supply=supply, cached=entries)
    assert plan.mode == "publish_cached" and plan.missing_count == plan.missing_bytes == 0
    assert plan.missing_identity_sha256 == digest([]) and plan.metadata_index_sha256 is None


def test_existing_complete_index_is_readonly_fast_path_even_cold_cache(api: Any) -> None:
    request, root, supply, entries = inputs()
    records = tuple(extract_python_wheel_metadata(entry, wheel(name=entry.name)[1]) for entry in entries)
    index = PythonDependencyMetadataIndex(supply.digest, records)
    plan = api.build_python_metadata_plan(request, root=root, supply=supply, cached=(), index=index)
    assert plan.mode == "ready" and plan.metadata_index_sha256 == index.sha256
    assert plan.missing_count == plan.missing_bytes == 0


@pytest.mark.parametrize("kind", ["root", "catalog", "source", "scope", "install"])
def test_changed_or_forged_request_is_rejected(api: Any, kind: str) -> None:
    request, root, supply, entries = inputs()
    if kind == "root": root = replace(root, wheel_max_bytes=root.wheel_max_bytes + 1)
    if kind == "catalog": supply = replace(supply, fetch_timeout_seconds=61)
    if kind == "source": request = request.model_copy(update={"draft": request.draft.model_copy(update={"source_sha256": "invalid"})})
    if kind in {"scope", "install"}:
        patch = {"run_id": UUID(int=4)} if kind == "scope" else {"install_allowed": True}
        request = request.model_copy(update={"invocation": request.invocation.model_copy(update=patch)})
    with pytest.raises(ValueError):
        api.build_python_metadata_plan(request, root=root, supply=supply, cached=entries)


@pytest.mark.parametrize("kind", ["duplicate", "foreign", "changed_url", "changed_hash", "changed_size", "list"])
def test_cache_partition_must_match_exact_root_catalog_entries(api: Any, kind: str) -> None:
    request, root, supply, entries = inputs()
    bad: object = entries
    if kind == "duplicate": bad = (entries[0], entries[0])
    if kind == "foreign": bad = (wheel(name="foreign")[0],)
    if kind == "changed_url": bad = (replace(entries[0], url=entries[0].url.replace("/ab/cd/", "/ab/ef/")),)
    if kind == "changed_hash": bad = (replace(entries[0], sha256="0" * 64),)
    if kind == "changed_size": bad = (replace(entries[0], size_bytes=entries[0].size_bytes + 1),)
    if kind == "list": bad = list(entries)
    with pytest.raises(ValueError):
        api.build_python_metadata_plan(request, root=root, supply=supply, cached=bad)


def test_partial_or_foreign_index_never_becomes_ready(api: Any) -> None:
    request, root, supply, entries = inputs()
    record = extract_python_wheel_metadata(entries[0], wheel(name=entries[0].name)[1])
    for index in (PythonDependencyMetadataIndex(supply.digest, (record,)),
                  PythonDependencyMetadataIndex("0" * 64, (record,)), object()):
        with pytest.raises(ValueError):
            api.build_python_metadata_plan(request, root=root, supply=supply, cached=entries, index=index)


def test_full_catalog_quota_includes_unused_wheels_without_chunk_budget_refresh(api: Any) -> None:
    request, root, supply, entries = inputs()
    supply = replace(supply, max_total_bytes=sum(e.size_bytes for e in entries[:2]))
    request = request.model_copy(update={"expected_catalog_sha256": supply.digest})
    with pytest.raises(ValueError, match="metadata_plan_quota"):
        api.build_python_metadata_plan(request, root=root, supply=supply, cached=entries[:2])


def test_plan_hash_is_stable_for_equivalent_cache_order_and_shrinking_transport_time(api: Any) -> None:
    request, root, supply, entries = inputs()
    before = api.build_python_metadata_plan(request, root=root, supply=supply, cached=entries)
    request = request.model_copy(update={"invocation": request.invocation.model_copy(update={
        "timeout_seconds": 10, "execution_id": "metadata_plan_2"})})
    after = api.build_python_metadata_plan(request, root=root, supply=supply, cached=tuple(reversed(entries)))
    assert before == after and before.sha256 == after.sha256


@pytest.mark.parametrize("field", ["request", "invocation", "scope", "draft"])
def test_copied_forbidden_fields_are_not_erased_by_serialization(api: Any, field: str) -> None:
    request, root, supply, entries = inputs()
    if field == "request":
        request = request.model_copy(update={"original_reference": {"forged": True}})
    else:
        changed = getattr(request, field).model_copy(update={"url": "https://untrusted.invalid/"})
        request = request.model_copy(update={field: changed})
    with pytest.raises(ValueError, match="metadata_plan_request_invalid"):
        api.build_python_metadata_plan(request, root=root, supply=supply, cached=entries)
