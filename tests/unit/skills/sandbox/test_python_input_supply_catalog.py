"""Planning IO doubles are not native root, network or installation evidence."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.capabilities.project_baselines import ProjectBaselineReference, StoredProjectBaseline
from agent_hub.capabilities.project_contracts import build_project_check_contract
from agent_hub.capabilities.python_input_preparation import validate_supply_declaration
from agent_hub.skills.sandbox import python_dependency_inspection, python_original_root_recipe
from agent_hub.skills.sandbox.python_dependency_metadata import PythonDependencyMetadataIndex
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectProbeInvocation
from agent_hub.skills.sandbox.python_project_wheel_cache import PythonWheelCacheError, _Code
from tests.unit.capabilities.test_python_project_recipe import LOCK, MANIFEST
from tests.unit.capabilities.test_standard_python_original_intent import FILES
from tests.unit.capabilities.test_standard_python_original_intent import (
    MANIFEST as STANDARD_MANIFEST,
)
from tests.unit.capabilities.test_standard_python_private_recipe import ENVIRONMENT, _wheel
from tests.unit.skills.sandbox.test_python_project_backend import configuration_payload
from tests.unit.skills.sandbox.test_python_project_staging import fixture


@pytest.fixture
def api() -> Any:
    return importlib.import_module("agent_hub.skills.sandbox.python_input_supply_catalog")


def catalog() -> tuple[WheelDownload, ...]:
    recipe, _, _, _ = fixture()
    return tuple(WheelDownload(w.filename, w.name, w.version, w.sha256, 123,
        "https://files.pythonhosted.org/packages/ab/cd/" + w.filename) for w in recipe.wheels)


def payload() -> dict[str, Any]:
    return {"schema_version": 1, "state_root": "/var/lib/python-input-supply",
        "catalog": [{key: getattr(w, key) for key in (
            "filename", "name", "version", "sha256", "size_bytes", "url")} for w in catalog()],
        "max_total_bytes": 1024, "min_free_bytes": 4096, "fetch_timeout_seconds": 60}


def test_config_roundtrip_and_digest_bind_every_safe_configuration_field(api: Any) -> None:
    raw = payload()
    value = api.RootSupplyConfig.from_payload(raw)
    assert value.to_payload() == raw
    assert value.catalog == catalog()
    expected = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()
    assert value.digest == expected
    for key, changed in (("state_root", "/var/lib/other-supply"), ("max_total_bytes", 2048),
                         ("min_free_bytes", 8192), ("fetch_timeout_seconds", 59)):
        assert api.RootSupplyConfig.from_payload(raw | {key: changed}).digest != value.digest
    raw["catalog"][0]["url"] = raw["catalog"][0]["url"].replace("/ab/", "/ef/")
    assert api.RootSupplyConfig.from_payload(raw).digest != value.digest


def test_network_supply_budget_has_an_inclusive_64_mib_ceiling(api: Any) -> None:
    maximum = 64 * 1024**2
    configured = api.RootSupplyConfig.from_payload(payload() | {"max_total_bytes": maximum})
    assert configured.max_total_bytes == maximum
    with pytest.raises(api.InputSupplyError, match="^input_supply_configuration_untrusted$"):
        api.RootSupplyConfig.from_payload(payload() | {"max_total_bytes": maximum + 1})
    with pytest.raises(api.InputSupplyError, match="^input_supply_configuration_untrusted$"):
        replace(configured, max_total_bytes=maximum + 1)


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"schema_version": 2}, {"extra": True},
    {"state_root": "relative"}, {"state_root": "/a/../b"}, {"state_root": "/a//b"},
    {"state_root": "/"}, {"state_root": "/a\\b"}, {"state_root": "/a\nb"},
    {"max_total_bytes": True}, {"max_total_bytes": 0}, {"max_total_bytes": 2**63},
    {"min_free_bytes": -1}, {"min_free_bytes": float("inf")},
    {"fetch_timeout_seconds": 1.5}, {"fetch_timeout_seconds": False},
    {"fetch_timeout_seconds": 0}, {"fetch_timeout_seconds": 601},
    {"catalog": []}, {"catalog": ()},
])
def test_config_rejects_coercion_paths_and_unbounded_data(api: Any, change: dict[str, Any]) -> None:
    with pytest.raises(api.InputSupplyError, match="^input_supply_configuration_untrusted$"):
        api.RootSupplyConfig.from_payload(payload() | change)


@pytest.mark.parametrize("case", ["duplicate", "too-many", "url", "extra", "size", "hash"])
def test_catalog_is_exact_bounded_unique_and_official_only(api: Any, case: str) -> None:
    raw = payload()
    if case == "duplicate":
        raw["catalog"].append(raw["catalog"][0].copy())
    elif case == "too-many":
        raw["catalog"] = raw["catalog"][:1] * 1001
    else:
        key, value = {"url": ("url", "https://evil.invalid/a.whl"),
            "extra": ("command", "pip install"), "size": ("size_bytes", True),
            "hash": ("sha256", "x" * 64)}[case]
        raw["catalog"][0][key] = value
    with pytest.raises(api.InputSupplyError, match="^input_supply_configuration_untrusted$"):
        api.RootSupplyConfig.from_payload(raw)


@pytest.fixture
def planned(api: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    recipe, old, original, _ = fixture()
    original = replace(original, snapshot=tmp_path)
    (tmp_path / "pyproject.toml").write_bytes(MANIFEST)
    (tmp_path / "requirements.lock").write_bytes(LOCK)
    invocation = PythonProjectProbeInvocation(**{key: getattr(old, key) for key in (
        "execution_id", "tenant_id", "run_id", "project_id", "session_id", "sandbox_profile",
        "timeout_seconds", "original_reference")})
    backend = importlib.import_module("agent_hub.skills.sandbox.python_project_backend")
    config = backend.RootConfig.from_payload(configuration_payload())
    supply = api.RootSupplyConfig.from_payload(payload())
    calls: list[str] = []

    def runtime(root: Path, **kwargs: Any) -> Any:
        assert root == config.runtime_root
        assert kwargs["interpreter_relative"] == "bin/python3.12"
        assert kwargs["limits"].max_bytes == config.runtime_max_bytes
        return SimpleNamespace(binary_sha256=recipe.interpreter.binary_sha256,
            content_sha256=recipe.interpreter.base_runtime_sha256)

    def wheels(root: Path, requirements: Any, **kwargs: Any) -> Any:
        assert root == config.wheel_root and len(requirements) == 1
        assert kwargs["limits"].max_bytes == config.wheel_max_bytes
        calls.append(requirements[0].name)
        raise PythonWheelCacheError("wheel_cache_wheel_missing")

    monkeypatch.setattr(api, "observe_python_distribution", runtime)
    monkeypatch.setattr(api, "observe_python_wheel_cache", wheels)

    def run(**changes: Any) -> Any:
        return api.plan_inputs(original=changes.pop("original", original),
            invocation=changes.pop("invocation", invocation),
            configuration=changes.pop("configuration", config),
            supply=changes.pop("supply", supply), deadline=changes.pop("deadline", time.monotonic()+30),
            **changes)

    return SimpleNamespace(run=run, recipe=recipe, original=original, invocation=invocation,
        config=config, supply=supply, calls=calls)


def test_empty_cache_plan_binds_measured_inputs_and_full_selected_total(api: Any, planned: Any) -> None:
    result = planned.run()
    assert result.plan.artifacts and result.downloads == catalog()
    assert result.missing_wheels == tuple(sorted(w.filename for w in catalog()))
    assert result.plan.declaration_sha256 == planned.recipe.declaration.sha256
    assert result.plan.manifest_sha256 == hashlib.sha256(MANIFEST).hexdigest()
    assert result.plan.lock_sha256 == hashlib.sha256(LOCK).hexdigest()
    assert result.plan.catalog_sha256 == planned.supply.digest
    assert result.plan.interpreter.model_dump() == planned.recipe.interpreter.to_payload()
    result.plan.require_scope(planned.invocation)
    assert planned.calls == [w.name for w in catalog()]
    with pytest.raises((AttributeError, TypeError)):
        result.downloads = ()


def test_standard_original_missing_cached_wheel_produces_exact_bound_download(
    planned: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "standard"
    snapshot.mkdir()
    content = dict.fromkeys(FILES, b"pass\n") | {"pyproject.toml": STANDARD_MANIFEST}
    for name, data in content.items():
        path = snapshot / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    files = {name: hashlib.sha256(b"f\0" + data).hexdigest() for name, data in content.items()}
    source_sha = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()
    invocation = planned.invocation
    contract = build_project_check_contract(
        tenant_id=invocation.tenant_id, run_id=invocation.run_id, project_id=invocation.project_id,
        session_id=invocation.session_id, input_source_sha256=source_sha,
        editable_paths=("src/expense_cli/cli.py",), source_files=files,
    )
    reference = ProjectBaselineReference("a" * 64, source_sha, len(files))
    original = StoredProjectBaseline(reference, contract, snapshot, files)
    invocation = PythonProjectProbeInvocation.model_validate({
        **invocation.model_dump(), "original_reference": reference.to_payload(),
    }, strict=True)
    wheels = (_wheel("click", "8.1.8"), _wheel("setuptools", "80.9.0"), _wheel("wheel", "0.45.1"))
    supply = replace(planned.supply, catalog=tuple(item.candidate.download for item in wheels), max_total_bytes=4096)
    index = PythonDependencyMetadataIndex(supply.digest, tuple(item.metadata for item in wheels))
    monkeypatch.setattr(python_original_root_recipe, "read_dependency_metadata_index", lambda *args, **kwargs: index)
    monkeypatch.setattr(python_dependency_inspection, "_target_environment", lambda config: {
        **ENVIRONMENT, "python_full_version": config.interpreter.version,
        "implementation_version": config.interpreter.version,
    })
    result = planned.run(original=original, invocation=invocation, supply=supply)
    validate_supply_declaration(result.plan, invocation, original)
    assert result.downloads == supply.catalog
    assert result.plan.manifest_sha256 == hashlib.sha256(STANDARD_MANIFEST).hexdigest()
    assert result.plan.catalog_sha256 == supply.digest
    assert not (snapshot / "requirements.lock").exists()
    assert (snapshot / "pyproject.toml").read_bytes() == STANDARD_MANIFEST
    tampered = result.plan.model_copy(update={"lock_sha256": "0" * 64})
    with pytest.raises(ValueError, match="input_supply_plan_changed"):
        validate_supply_declaration(tampered, invocation, original)


def test_missing_is_not_plan_identity_or_an_approval_change(
    api: Any, planned: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = planned.run()
    monkeypatch.setattr(api, "observe_python_wheel_cache", lambda root, req, **kw:
        tuple(w for w in planned.recipe.wheels if w.name == req[0].name))
    second = planned.run()
    assert second.plan == first.plan and second.plan.sha256 == first.plan.sha256
    assert second.missing_wheels == second.downloads == ()
    with pytest.raises(api.InputSupplyError, match="input_supply_quota"):
        planned.run(supply=replace(planned.supply, max_total_bytes=368))
    assert planned.run(supply=replace(planned.supply, max_total_bytes=369)).plan.artifacts


def test_complete_selection_over_64_mib_refuses_during_planning(api: Any, planned: Any) -> None:
    maximum = 64 * 1024**2
    entries = tuple(replace(w, size_bytes=maximum // 2) for w in catalog())
    supply = replace(planned.supply, catalog=entries, max_total_bytes=maximum)
    with pytest.raises(api.InputSupplyError, match="^input_supply_quota$"):
        planned.run(supply=supply)
    assert planned.calls == []


@pytest.mark.parametrize("code", ["wheel_cache_hash_mismatch", "wheel_cache_ambiguous",
    "wheel_cache_unsafe", "wheel_cache_missing", "wheel_cache_limit"])
def test_only_actual_wheel_missing_can_be_downloaded(
    api: Any, planned: Any, monkeypatch: pytest.MonkeyPatch, code: str,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise PythonWheelCacheError(cast(_Code, code))
    monkeypatch.setattr(api, "observe_python_wheel_cache", fail)
    with pytest.raises(api.InputSupplyError, match="^input_supply_artifact_integrity$"):
        planned.run()


def test_selected_existing_binding_must_equal_catalog(
    api: Any, planned: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong = replace(planned.recipe.wheels[0], sha256="d" * 64)
    monkeypatch.setattr(api, "observe_python_wheel_cache", lambda *a, **kw: (wrong,))
    with pytest.raises(api.InputSupplyError, match="input_supply_artifact_integrity"):
        planned.run()


@pytest.mark.parametrize("case", ["absent", "version", "hash"])
def test_catalog_must_supply_exact_lock_without_solving(api: Any, planned: Any, case: str) -> None:
    entries = list(catalog())
    if case == "absent":
        entries.pop(0)
    elif case == "version":
        entries[0] = replace(entries[0], filename="packaging-24.0-py3-none-any.whl",
            version="24.0", url="https://files.pythonhosted.org/packages/ab/cd/packaging-24.0-py3-none-any.whl")
    else:
        entries[0] = replace(entries[0], sha256="d" * 64)
    with pytest.raises(api.InputSupplyError, match="input_supply_artifact_(missing|integrity)"):
        planned.run(supply=replace(planned.supply, catalog=tuple(entries)))
    assert planned.calls == []


@pytest.mark.parametrize("key", ["tenant_id", "run_id", "project_id", "session_id", "original_reference"])
def test_exact_scope_and_reference_are_required_before_observation(api: Any, planned: Any, key: str) -> None:
    value: Any = UUID(int=99) if key.endswith("_id") and key in {"tenant_id", "run_id"} else "other"
    if key == "original_reference":
        value = planned.invocation.original_reference.model_copy(update={"contract_sha256": "f"*64})
    with pytest.raises(api.InputSupplyError, match="input_supply_scope_changed"):
        planned.run(invocation=planned.invocation.model_copy(update={key: value}))
    assert planned.calls == []


def test_changed_controls_cannot_match_original_contract(api: Any, planned: Any) -> None:
    (planned.original.snapshot / "pyproject.toml").write_bytes(MANIFEST + b"\n")
    with pytest.raises(api.InputSupplyError, match="input_supply_scope_changed"):
        planned.run()


def test_runtime_must_be_measured_not_just_declared(api: Any, planned: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "observe_python_distribution", lambda *a, **kw:
        SimpleNamespace(binary_sha256="f" * 64, content_sha256="e" * 64))
    with pytest.raises(api.InputSupplyError, match="input_supply_unavailable"):
        planned.run()
    assert planned.calls == []


def test_unsafe_original_read_has_a_finite_path_free_error(
    api: Any, planned: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.capabilities.scoped_read import ScopedReadError

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ScopedReadError("secret /private/original")

    monkeypatch.setattr(api, "_read_no_links", fail)
    with pytest.raises(api.InputSupplyError, match="^input_supply_scope_changed$"):
        planned.run()


def test_no_install_boolean_can_be_smuggled_into_planning(api: Any, planned: Any) -> None:
    bad = planned.invocation.model_copy(update={"install_allowed": True})
    with pytest.raises(api.InputSupplyError, match="input_supply_request_invalid"):
        planned.run(invocation=bad)
    assert planned.calls == []


def test_both_runtime_and_cache_observer_timeouts_are_finite(
    api: Any, planned: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise PythonWheelCacheError("wheel_cache_timeout")
    monkeypatch.setattr(api, "observe_python_wheel_cache", fail)
    with pytest.raises(api.InputSupplyError, match="input_supply_timeout"):
        planned.run()


@pytest.mark.parametrize("first", ["catalog", "backend"])
def test_fresh_subprocess_imports_do_not_cycle(first: str) -> None:
    catalog_name = "agent_hub.skills.sandbox.python_input_supply_catalog"
    backend_name = "agent_hub.skills.sandbox.python_project_backend"
    modules = (catalog_name, backend_name) if first == "catalog" else (backend_name, catalog_name)
    result = subprocess.run([sys.executable, "-I", "-B", "-c",
        "import importlib; " + "; ".join(f"importlib.import_module({name!r})" for name in modules)],
        capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == b""


@pytest.mark.parametrize("deadline", [0, -1, float("inf"), float("nan"), True, 10**1000])
def test_deadline_is_finite_and_not_boolean(api: Any, planned: Any, deadline: Any) -> None:
    with pytest.raises(api.InputSupplyError, match="input_supply_(timeout|request_invalid)"):
        planned.run(deadline=deadline)
    assert planned.calls == []


def test_controls_share_the_source_budget(api: Any, planned: Any) -> None:
    invocation = planned.invocation.model_copy(update={
        "source_limit_bytes": len(MANIFEST) + len(LOCK) - 1})
    with pytest.raises(api.InputSupplyError, match="input_supply_quota"):
        planned.run(invocation=invocation)
    assert planned.calls == []


def test_timeout_after_slow_observer_still_refuses_plan(api: Any, planned: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([1.0, 1.0, 1.0, 100.0])
    monkeypatch.setattr(api.time, "monotonic", lambda: next(clock, 100.0))
    with pytest.raises(api.InputSupplyError, match="input_supply_timeout"):
        planned.run(deadline=10.0)


@pytest.fixture
def root_profile() -> Any:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("real root-owned Linux FD/path checks only; not a Windows native claim")
    root = Path(tempfile.mkdtemp(prefix="input-supply-catalog-test-", dir="/run"))
    root.chmod(0o700)
    try:
        yield root
    finally:
        shutil.rmtree(root)


def root_config(api: Any, root: Path) -> Path:
    state = root / "state"
    state.mkdir(mode=0o700)
    raw = payload() | {"state_root": str(state)}
    path = root / "supply.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_real_linux_root_profile_read_is_bounded_and_readonly(api: Any, root_profile: Path) -> None:
    path = root_config(api, root_profile)
    before = path.stat()
    result = api.read_supply_configuration(path)
    assert result.state_root == root_profile / "state"
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert sorted(p.name for p in root_profile.iterdir()) == ["state", "supply.json"]


@pytest.mark.parametrize("case", ["mode", "owner", "hardlink", "symlink", "ancestor-link",
    "ancestor-writable", "state-mode", "state-owner", "state-link", "duplicate", "nan",
    "oversize", "fifo", "missing"])
def test_real_linux_config_refuses_unsafe_profiles(api: Any, root_profile: Path, case: str) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux-only filesystem APIs")
    path = root_config(api, root_profile)
    if case == "mode":
        path.chmod(0o644)
    elif case == "owner":
        os.chown(path, 10001, 10001)
    elif case == "hardlink":
        os.link(path, root_profile / "alias")
    elif case in {"symlink", "ancestor-link"}:
        alias = root_profile / "alias"
        alias.symlink_to(path if case == "symlink" else root_profile, target_is_directory=case != "symlink")
        path = alias if case == "symlink" else alias / "supply.json"
    elif case == "ancestor-writable":
        root_profile.chmod(0o722)
    elif case == "state-mode":
        (root_profile / "state").chmod(0o755)
    elif case == "state-owner":
        os.chown(root_profile / "state", 10001, 10001)
    elif case == "state-link":
        (root_profile / "state").rmdir()
        (root_profile / "state").symlink_to(root_profile, target_is_directory=True)
    elif case == "duplicate":
        path.write_text(path.read_text().replace("{", '{"schema_version":1,', 1))
    elif case == "nan":
        path.write_text(path.read_text().replace('"min_free_bytes": 4096', '"min_free_bytes": NaN'))
    elif case == "oversize":
        path.write_bytes(b" " * (512 * 1024 + 1))
    elif case == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    else:
        path.unlink()
    with pytest.raises(api.InputSupplyError) as caught:
        api.read_supply_configuration(path)
    assert caught.value.code in {"input_supply_configuration_untrusted", "input_supply_configuration_missing"}
    assert str(root_profile) not in str(caught.value)
