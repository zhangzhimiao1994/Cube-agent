"""Catalog/planning tests do not grant supply authority or prove Go execution."""

from __future__ import annotations

import importlib
import importlib.util
from copy import deepcopy
from typing import Any
from uuid import UUID

import pytest

from agent_hub.capabilities.go_project_recipe import parse_go_project_declaration


@pytest.fixture
def api() -> Any:
    name = "agent_hub.skills.sandbox.toolchain_catalog"
    if importlib.util.find_spec(name) is None:
        pytest.skip("catalog behavior awaits the missing trusted catalog")
    return importlib.import_module(name)


def test_trusted_sdk_catalog_exists() -> None:
    assert importlib.util.find_spec("agent_hub.skills.sandbox.toolchain_catalog") is not None


def artifact(version: str = "1.27.3") -> dict[str, object]:
    return {"stack": "go", "version": version, "platform": "linux_amd64",
            "url": f"https://dl.google.com/go/go{version}.linux-amd64.tar.gz",
            "sha256": "a" * 64, "archive_bytes": 70, "unpacked_bytes_limit": 1000,
            "archive_entries_limit": 100, "directory_entries_limit": 100, "tar_bytes_limit": 10240}


def catalog() -> dict[str, object]:
    return {"schema_version": 1, "runtime_root": "/var/lib/agent-hub-toolchains",
            "stage_root": "/var/lib/agent-hub-toolchain-stages",
            "state_root": "/var/lib/agent-hub-toolchain-state",
            "min_free_bytes": 3 * 1024**3, "artifacts": [artifact()]}


def scope(api: Any) -> Any:
    return api.ToolchainPlanScope(tenant_id=UUID(int=1), user_id=UUID(int=2), run_id=UUID(int=3),
        project_id="demo", session_id="session", actor_id="implementer", actor_role="engineer",
        source_sha256="b" * 64, contract_sha256="c" * 64)


def declaration(version: str = "1.27", toolchain: str | None = None) -> Any:
    raw = f"module example.org/demo\ngo {version}\n"
    if toolchain:
        raw += "toolchain " + toolchain + "\n"
    return parse_go_project_declaration({"go.mod": raw.encode(), "main.go": b"package main\n"})


def test_catalog_roundtrip_and_every_field_is_digest_bound(api: Any) -> None:
    config = api.ToolchainCatalog.from_payload(catalog())
    assert config.to_payload() == catalog()
    assert api.ToolchainCatalog.from_json(config.to_json()).sha256 == config.sha256
    for key, value in (("min_free_bytes", 4 * 1024**3),
                       ("runtime_root", "/var/lib/other-toolchains")):
        assert api.ToolchainCatalog.from_payload(catalog() | {key: value}).sha256 != config.sha256
    changed = catalog()
    changed["artifacts"] = [artifact() | {"unpacked_bytes_limit": 1001}]
    assert api.ToolchainCatalog.from_payload(changed).sha256 != config.sha256


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"schema_version": 2}, {"extra": "ignored"},
    {"runtime_root": "/"}, {"runtime_root": "relative"},
    {"runtime_root": "/var//sdk"}, {"runtime_root": "/var/a/../sdk"},
    {"runtime_root": "/var/sdk\n"}, {"runtime_root": "/var/sdk\\bin"},
    {"runtime_root": "/var/lib/agent-hub-toolchain-state/child"},
    {"stage_root": "/var/lib/agent-hub-toolchains"},
    {"min_free_bytes": 3 * 1024**3 - 1}, {"min_free_bytes": True},
    {"min_free_bytes": float("inf")}, {"min_free_bytes": 2**63},
    {"artifacts": []}, {"artifacts": [artifact(), artifact()]},
])
def test_catalog_is_strict_and_cannot_lower_existing_reserve(api: Any, change: dict[str, Any]) -> None:
    with pytest.raises(api.ToolchainCatalogError):
        api.ToolchainCatalog.from_payload(catalog() | change)


@pytest.mark.parametrize("change", [
    {"stack": "python"}, {"platform": "windows_amd64"}, {"platform": "linux_arm64"},
    {"version": "latest"}, {"version": "1.027.3"}, {"version": "1.27"},
    {"version": "1.27.3rc1"}, {"sha256": "A" * 64}, {"sha256": "x" * 64},
    {"archive_bytes": True}, {"archive_bytes": 0}, {"archive_bytes": 2**63},
    {"unpacked_bytes_limit": -1}, {"archive_entries_limit": 0},
    {"tar_bytes_limit": 10239}, {"tar_bytes_limit": 10241},
    {"url": "https://go.dev/dl/go1.27.3.linux-amd64.tar.gz"},
    {"url": "https://dl.google.com/go/go1.27.3.linux-amd64.tar.gz?token=secret"},
    {"url": "https://dl.google.com:443/go/go1.27.3.linux-amd64.tar.gz"},
    {"url": "https://dl.google.com/go/go1.27.4.linux-amd64.tar.gz"},
    {"command": "curl | sh"},
])
def test_artifact_is_exact_official_data_not_an_install_command(api: Any, change: dict[str, Any]) -> None:
    with pytest.raises(api.ToolchainCatalogError):
        api.ToolchainArtifact.from_payload(artifact() | change)


def test_duplicate_json_keys_nonfinite_and_oversize_fail_closed(api: Any) -> None:
    for raw in (b'{"schema_version":1,"schema_version":1}', b'{"min_free_bytes":NaN}',
                b" " * (512 * 1024 + 1), b"[]"):
        with pytest.raises(api.ToolchainCatalogError):
            api.ToolchainCatalog.from_json(raw)


def test_select_lowest_approved_compatible_not_latest_or_downloaded_version(api: Any) -> None:
    config = api.ToolchainCatalog.from_payload(catalog() | {
        "artifacts": [artifact("1.27.3"), artifact("1.27.4"), artifact("1.28.1")]})
    assert config.select(declaration()).version == "1.27.3"
    assert config.select(declaration("1.27.4")).version == "1.27.4"
    assert config.select(declaration("1.27", "go1.28.1")).version == "1.28.1"
    assert config.select(declaration("1.27", "default")).version == "1.27.3"
    for req in (declaration("1.29"), declaration("1.27", "go1.27.5"),
                declaration("1.28", "go1.27.3")):
        with pytest.raises(api.ToolchainCatalogError, match="toolchain_catalog_no_match"):
            config.select(req)


def test_catalog_order_is_canonical_and_empty_or_unsorted_versions_rejected(api: Any) -> None:
    with pytest.raises(api.ToolchainCatalogError):
        api.ToolchainCatalog.from_payload(catalog() | {
            "artifacts": [artifact("1.28.1"), artifact("1.27.3")]})


def spaces(api: Any, free: int = 10 * 1024**3, split: bool = False) -> tuple[Any, ...]:
    return tuple(api.FilesystemSpace(role=role, device=2 if split and role == "runtime" else 1,
        available_bytes=free, allocation_unit=4096) for role in ("runtime", "stage", "execution", "state"))


def test_plan_binds_scope_declaration_catalog_artifact_and_full_peak(api: Any) -> None:
    config = api.ToolchainCatalog.from_payload(catalog())
    plan = api.plan_toolchain_supply(config, declaration(), scope(api), spaces(api),
                                     execution_bytes=1024**2)
    assert plan.scope == scope(api)
    assert plan.catalog_sha256 == config.sha256
    assert plan.declaration_sha256 == declaration().sha256
    assert plan.artifact.version == "1.27.3"
    assert len(plan.filesystems) == 1
    # Two independently allocated trees, archive, controls and future execution,
    # not merely the compressed download size or an archive-wide multiplier.
    assert plan.filesystems[0].required_bytes >= 2 * 1000 + 70 + 1024**2 + 2 * 100 * 4096
    assert api.ToolchainSupplyPlan.from_json(plan.to_json()).sha256 == plan.sha256
    before = plan.sha256
    for change in ({"actor_id": "reviewer"}, {"run_id": UUID(int=4)},
                   {"source_sha256": "d" * 64}, {"contract_sha256": "e" * 64}):
        updated = api.ToolchainPlanScope.model_validate(scope(api).model_dump() | change, strict=True)
        other = api.plan_toolchain_supply(config, declaration(), updated, spaces(api),
                                          execution_bytes=1024**2)
        assert other.sha256 != before
    assert api.plan_toolchain_supply(config, declaration(), scope(api), spaces(api),
        execution_bytes=2 * 1024**2).sha256 != before


def test_peak_is_grouped_by_real_device_and_reserve_is_not_counted_twice(api: Any) -> None:
    config = api.ToolchainCatalog.from_payload(catalog())
    plan = api.plan_toolchain_supply(config, declaration(), scope(api), spaces(api, split=True),
                                     execution_bytes=1024**2)
    assert tuple(p.device for p in plan.filesystems) == (1, 2)
    for peak in plan.filesystems:
        assert peak.available_bytes - peak.required_bytes >= config.min_free_bytes
        assert peak.reserve_bytes == config.min_free_bytes


def test_exact_capacity_boundary_then_full_peak_failure_not_archive_only_admission(api: Any) -> None:
    config = api.ToolchainCatalog.from_payload(catalog())
    reference = api.plan_toolchain_supply(config, declaration(), scope(api), spaces(api),
                                          execution_bytes=1024**2)
    required = reference.filesystems[0].required_bytes
    api.plan_toolchain_supply(config, declaration(), scope(api),
        spaces(api, config.min_free_bytes + required), execution_bytes=1024**2)
    for free in (config.min_free_bytes + required - 1, config.min_free_bytes + 71):
        with pytest.raises(api.ToolchainCatalogError, match="toolchain_capacity"):
            api.plan_toolchain_supply(config, declaration(), scope(api), spaces(api, free),
                                       execution_bytes=1024**2)


@pytest.mark.parametrize("case", ["duplicate", "missing", "inconsistent", "bool", "zero", "huge"])
def test_capacity_inputs_cannot_be_ambiguous_or_coerced(api: Any, case: str) -> None:
    values = spaces(api)
    execution: object = 4096
    if case == "duplicate":
        values += (values[0],)
    elif case == "missing":
        values = values[:2]
    elif case == "inconsistent":
        values = (values[0].model_copy(update={"available_bytes": 100}), *values[1:])
    else:
        execution = {"bool": True, "zero": 0, "huge": 2**63}[case]
    with pytest.raises(api.ToolchainCatalogError):
        api.plan_toolchain_supply(api.ToolchainCatalog.from_payload(catalog()), declaration(),
            scope(api), values, execution_bytes=execution)


def test_plan_rejects_tampered_capacity_or_unknown_fields(api: Any) -> None:
    plan = api.plan_toolchain_supply(api.ToolchainCatalog.from_payload(catalog()), declaration(),
        scope(api), spaces(api), execution_bytes=4096)
    raw = plan.to_payload()
    for change in ({"execution_supported": True}, {"schema_version": True},
                   {"catalog_sha256": "bad"}):
        with pytest.raises(api.ToolchainCatalogError):
            api.ToolchainSupplyPlan.from_payload(raw | change)
    damaged = deepcopy(raw)
    damaged["filesystems"][0]["required_bytes"] = 1
    with pytest.raises(api.ToolchainCatalogError):
        api.ToolchainSupplyPlan.from_payload(damaged)


def test_state_proof_files_need_their_own_filesystem_peak(api: Any) -> None:
    values = tuple(s for s in spaces(api) if s.role != "state") + (api.FilesystemSpace(role="state", device=7,
        available_bytes=10 * 1024**3, allocation_unit=4096),)
    plan = api.plan_toolchain_supply(api.ToolchainCatalog.from_payload(catalog()), declaration(),
        scope(api), values, execution_bytes=4096)
    state = next(p for p in plan.filesystems if p.device == 7)
    assert state.roles == ("state",)
    assert state.required_bytes >= 1024**2


def test_implicit_directory_budget_is_independent_of_tar_header_count(api: Any) -> None:
    config = api.ToolchainCatalog.from_payload(catalog() | {"artifacts": [
        artifact() | {"archive_entries_limit": 100, "directory_entries_limit": 6000}]})
    plan = api.plan_toolchain_supply(config, declaration(), scope(api), spaces(api, split=True),
                                     execution_bytes=4096)
    runtime = next(p for p in plan.filesystems if p.device == 2)
    assert runtime.required_bytes >= 6000 * 4096 + 1000
