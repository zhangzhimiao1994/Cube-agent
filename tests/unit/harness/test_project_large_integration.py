"""Synthetic protocol evidence tests; no native or generated-project acceptance credit."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from agent_hub.harness import project_requirements as requirements
from agent_hub.harness import project_scale as scale
from agent_hub.harness import project_scale_runner as runner
from agent_hub.harness import project_validation_result as protocol
from agent_hub.harness import project_validation_sandbox as sandbox


def synthetic_result(status: str = "passed") -> dict[str, Any]:
    fixture = Path(__file__).resolve().parents[2] / "fixtures/project_business/large_module_result.json"
    result: dict[str, Any] = json.loads(fixture.read_text(encoding="utf-8"))
    result.update(status=status, reasons=[] if status == "passed" else ["incomplete observation"])
    return result


def bundle() -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("package.json", "{}")
    return buffer.getvalue()


def test_large_protocol_accepts_exact_observations_and_returns_independent_copy() -> None:
    payload = synthetic_result()
    result = protocol.validate_scale_validation_result(payload, expected_profile="large-module-v1")
    assert result == payload and result is not payload
    assert result["measurements"] is not payload["measurements"]
    assert protocol.scale_validation_passed(result, expected_profile="large-module-v1")


@pytest.mark.parametrize(("tier", "profile"), [
    ("large", "large-module-v1"), ("ultra", "ultra-load-storage-v1"),
    ("small", None), ("medium", None), ("LARGE", None), ("", None),
])
def test_scale_profile_mapping(tier: str, profile: str | None) -> None:
    mapping = getattr(protocol, "scale_validation_profile", None)
    assert callable(mapping), "central scale/profile mapping is required"
    assert mapping(tier) == profile


@pytest.mark.parametrize("status", ["passed", "failed", "unknown"])
@pytest.mark.parametrize("field", synthetic_result()["measurements"])
@pytest.mark.parametrize("invalid", [True, False, -1, "1", None, float("inf"), float("nan")])
def test_large_measurement_types_fail_closed(status: str, field: str, invalid: object) -> None:
    result = synthetic_result(status)
    result["measurements"][field] = invalid
    with pytest.raises(ValueError):
        protocol.validate_scale_validation_result(result)
    assert not protocol.scale_validation_passed(result)


@pytest.mark.parametrize("field", [k for k in synthetic_result()["measurements"]
                                   if k != "elapsed_seconds"])
def test_large_counts_are_exact_for_pass_bounded_for_partial(field: str) -> None:
    result = synthetic_result()
    result["measurements"][field] -= 1
    assert not protocol.scale_validation_passed(result)
    for status in ("failed", "unknown"):
        result.update(status=status, reasons=["partial"])
        assert protocol.validate_scale_validation_result(result) == result
        result["measurements"][field] += 2
        with pytest.raises(ValueError):
            protocol.validate_scale_validation_result(result)
        result["measurements"][field] -= 2


@pytest.mark.parametrize(("field", "value"), [
    ("schema_version", True), ("schema_version", 1.0), ("schema_version", 2),
    ("scale", "ultra"), ("status", "success"), ("status", []),
    ("isolation_verified", False), ("isolation_verified", 1),
    ("cleanup_ok", False), ("cleanup_ok", 1), ("reasons", ["failure"]),
    ("reasons", ()), ("npm_start_module_binding", "passed"),
    ("npm_start_module_binding", True), ("extra", True),
])
def test_large_invalid_headers_rejected(field: str, value: object) -> None:
    result = synthetic_result()
    result[field] = value
    with pytest.raises(ValueError):
        protocol.validate_scale_validation_result(result)


@pytest.mark.parametrize("field", synthetic_result())
def test_large_requires_every_top_field(field: str) -> None:
    result = synthetic_result()
    del result[field]
    assert not protocol.scale_validation_passed(result)


@pytest.mark.parametrize("field", [*synthetic_result()["measurements"], "extra"])
def test_large_requires_exact_measurement_fields(field: str) -> None:
    result = synthetic_result()
    if field == "extra":
        result["measurements"][field] = 0
    else:
        del result["measurements"][field]
    assert not protocol.scale_validation_passed(result)


def test_large_unknown_records_no_invented_measurements() -> None:
    result = protocol.scale_validation_unknown("isolation unavailable", profile="large-module-v1")
    assert protocol.validate_scale_validation_result(result) == result
    assert result["scale"] == "large" and result["status"] == "unknown"
    assert result["cleanup_ok"] is False and result["isolation_verified"] is False
    assert result["npm_start_module_binding"] == "unknown"
    assert result["measurements"] == dict.fromkeys(synthetic_result()["measurements"], 0)
    assert not protocol.scale_validation_passed(result)


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_large_partial_results_require_reasons_and_unknown_npm_binding(status: str) -> None:
    result = synthetic_result(status)
    result.update(isolation_verified=False, cleanup_ok=False)
    assert protocol.validate_scale_validation_result(result) == result
    for reasons in ([], [""], [False]):
        with pytest.raises(ValueError):
            protocol.validate_scale_validation_result({**result, "reasons": reasons})
    with pytest.raises(ValueError):
        protocol.validate_scale_validation_result({**result, "npm_start_module_binding": "failed"})
    result["measurements"]["inventory_calls"] = 14.0
    with pytest.raises(ValueError):
        protocol.validate_scale_validation_result(result)


def test_large_gate_requires_bound_current_profile_and_identity() -> None:
    manifest = {"app.js": (1, "a" * 64)}
    result = runner.ProjectScaleCaseResult(
        case_id="large:direct", run_id="run-large", status="completed", evidence={},
        validated_workspace_manifest=manifest,
    )
    assert not result.scale_specific_evidence_ok
    bound = runner._bind_scale_validation(synthetic_result(), result.case_id, result.run_id, manifest)
    assert bound is not None
    result = replace(result, scale_validation=bound)
    assert result.scale_specific_evidence_ok
    assert not replace(result, case_id="large:hybrid").scale_specific_evidence_ok
    assert not replace(result, run_id="other").scale_specific_evidence_ok
    assert not replace(result, validated_workspace_manifest={"app.js": (2, "b" * 64)}).scale_specific_evidence_ok
    assert runner._bind_scale_validation(synthetic_result(), "ultra:direct", "run", manifest) is None
    assert runner._bind_scale_validation(synthetic_result(), "small:direct", "run", manifest) is None


@pytest.mark.parametrize("business_failure", [False, True])
def test_large_module_check_keeps_npm_start_business_mandatory(
    monkeypatch: pytest.MonkeyPatch, business_failure: bool,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(runner, "_run_generated_project_command", lambda *a, **k: None)

    def modules(root: Path, timeout: float) -> dict[str, Any]:
        assert (root / "package.json").exists() and 0 < timeout <= 10
        calls.append("modules")
        return synthetic_result()

    def business(root: Path, timeout_seconds: float) -> tuple[str, ...]:
        calls.append("business")
        return ("npm start business failed",) if business_failure else ()

    monkeypatch.setattr(runner, "validate_large_order_modules", modules, raising=False)
    monkeypatch.setattr(runner, "validate_large_order_ops_api", business)
    checked = runner._validate_generated_project_bundle(
        bundle(), commands=(("npm", "test"),), timeout_seconds=10,
        requirements_case_id="large:direct",
    )
    assert calls == ["modules", "business"]
    assert checked.passed is (not business_failure)
    assert checked.scale_validation == (None if business_failure else synthetic_result())


@pytest.mark.parametrize("exhausted", [False, True])
def test_large_modules_and_business_share_remaining_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch, exhausted: bool,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(runner, "_run_generated_project_command", lambda *a, **k: None)

    def modules(root: Path, timeout: float) -> dict[str, Any]:
        assert timeout == 10
        clock[0] = 111 if exhausted else 107
        return synthetic_result()

    budgets: list[float] = []

    def business(root: Path, timeout_seconds: float) -> tuple[str, ...]:
        budgets.append(timeout_seconds)
        return ()

    monkeypatch.setattr(runner, "validate_large_order_modules", modules)
    monkeypatch.setattr(runner, "validate_large_order_ops_api", business)
    checked = runner._validate_generated_project_bundle(
        bundle(), commands=(("npm", "test"),), timeout_seconds=20,
        absolute_deadline=110, requirements_case_id="large:direct",
    )
    assert budgets == ([] if exhausted else [3])
    assert checked.passed is (not exhausted)
    if exhausted:
        assert checked.scale_validation is None


@pytest.mark.parametrize("tier", ["small", "medium"])
def test_small_medium_business_does_not_require_module_evidence(
    monkeypatch: pytest.MonkeyPatch, tier: str,
) -> None:
    monkeypatch.setattr(runner, "_run_generated_project_command", lambda *a, **k: None)
    forbidden = Mock(side_effect=AssertionError("small/medium must not invoke scale modules"))
    monkeypatch.setattr(runner, "validate_large_order_modules", forbidden)
    monkeypatch.setattr(runner, "validate_ultra_portfolio_storage", forbidden)
    business = "validate_small_task_api" if tier == "small" else "validate_medium_crm_api"
    monkeypatch.setattr(runner, business, lambda *a, **k: ())
    checked = runner._validate_generated_project_bundle(
        bundle(), commands=(("npm", "test"),), timeout_seconds=10,
        requirements_case_id=f"{tier}:direct",
    )
    assert checked.passed and checked.scale_validation is None
    forbidden.assert_not_called()


@pytest.mark.parametrize("status", ["failed", "unknown", "malformed", "wrong_profile"])
def test_large_incomplete_module_check_blocks_credit_and_only_defects_are_repairable(
    monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    payload = synthetic_result(status if status in {"failed", "unknown"} else "passed")
    if status == "malformed":
        payload["measurements"]["inventory_calls"] = 13
    if status == "wrong_profile":
        payload["profile"] = "ultra-load-v1"
    monkeypatch.setattr(runner, "_run_generated_project_command", lambda *a, **k: None)
    monkeypatch.setattr(runner, "validate_large_order_modules", lambda *a: payload, raising=False)
    monkeypatch.setattr(runner, "validate_large_order_ops_api", lambda *a, **k: ())
    checked = runner._validate_generated_project_bundle(
        bundle(), commands=(("npm", "test"),), timeout_seconds=10,
        requirements_case_id="large:direct",
    )
    assert not checked.passed and checked.scale_validation is None
    assert runner._generated_project_validation_is_repairable(checked) is (status == "failed")


def test_large_public_wrapper_uses_sandbox_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    public = getattr(requirements, "validate_large_order_modules", None)
    assert callable(public), "public module validation wrapper is required"
    dispatch = Mock(return_value=synthetic_result("unknown"))
    monkeypatch.setattr(requirements, "_PLATFORM", "posix")
    monkeypatch.setattr(sandbox, "validate_scale_modules", dispatch, raising=False)
    assert public(Path("project"), 7.5) == synthetic_result("unknown")
    dispatch.assert_called_once_with(Path("project"), "large", 7.5)


def test_large_public_windows_fixture_keeps_isolation_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    result = synthetic_result("unknown")
    result.update(isolation_verified=False, reasons=["trusted Windows fixture only"])
    monkeypatch.setattr(requirements, "_PLATFORM", "nt")
    monkeypatch.setattr(requirements, "_validate_large_order_modules", lambda *a: result)
    observed = requirements.validate_large_order_modules(Path("fixture"), 7.5)
    assert observed == result
    assert protocol.validate_scale_validation_result(observed) == result
    assert not protocol.scale_validation_passed(observed)


def test_large_private_wrapper_uses_stdlib_sibling_runpy(tmp_path: Path) -> None:
    source = Path(requirements.__file__).resolve()
    script = """
import pathlib, runpy, sys
source = pathlib.Path(sys.argv[1])
module = runpy.run_path(str(source))
wrapper = module.get('_validate_large_order_modules')
assert callable(wrapper), 'isolated module wrapper is required'
def validate(root, timeout):
    assert root == pathlib.Path('project') and timeout == 7.5
    return {'status': 'unknown', 'sentinel': 42}
def load(path):
    assert pathlib.Path(path) == source.with_name('project_validation_modules.py')
    return {'validate_large_modules': validate}
runpy.run_path = load
assert wrapper(pathlib.Path('project'), 7.5) == {'status': 'unknown', 'sentinel': 42}
assert 'agent_hub.harness' not in sys.modules
"""
    (tmp_path / "project_validation_modules.py").write_text("raise RuntimeError('untrusted')")
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(source)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_large_initial_and_repair_prompts_share_complete_contract() -> None:
    guidance = getattr(scale, "PROJECT_LARGE_MODULE_GUIDANCE", None)
    assert isinstance(guidance, str) and guidance
    for flow in ("direct", "hybrid", "multi_agent"):
        plan = scale.build_project_scale_run_plan(
            benchmark_kind="capability", scales=("large",), flows=(flow,), execute=True,
        )
        request = plan.requests[0]
        assert guidance in str(request.body["message"])
        repaired = runner._deliverable_repair_body(
            {**request.body, "message": "context " * 1500}, request.case_id,
            benchmark_kind="capability", failed_reasons=("requirements: inventory failed",),
        )
        assert guidance in str(repaired["message"])
        assert len(str(repaired["message"])) <= 6000
