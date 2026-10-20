"""Trusted outer-runner sequencing; fake phases are not native acceptance."""

from __future__ import annotations

import importlib
import importlib.util
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.capability_installer.python_attestation import ScanLimits
from agent_hub.skills.sandbox.python_project_environment import _scan_limits
from agent_hub.skills.sandbox.python_project_process import PythonPhaseCleanupError
from tests.unit.capabilities.test_python_project_inputs import bind


@pytest.fixture
def api() -> Any:
    name = "agent_hub.skills.sandbox.python_project_runner"
    assert importlib.util.find_spec(name) is not None, "Python outer runner missing"
    return importlib.import_module(name)


def result(phase: str, **changes: Any) -> Any:
    detail: dict[str, object] = {
        "status": "completed",
        "phase": phase,
        "evidence": "native_process_only",
    }
    if phase in {"install", "check_environment"}:
        detail["requires_dist_check"] = "completed"
    elif phase == "build":
        detail["wheel"] = {
            "filename": "billing-1.0.0-py3-none-any.whl",
            "sha256": "a" * 64,
            "size": 100,
        }
    else:
        detail.update(
            test_count=5, **{"import": {"module": "billing", "origin": "billing/__init__.py"}}
        )
    return SimpleNamespace(
        **(
            {
                "exit_code": 0,
                "timed_out": False,
                "stdout": json.dumps(detail),
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
                "stdout_observed_bytes": len(json.dumps(detail)),
                "stderr_observed_bytes": 0,
            }
            | changes
        )
    )


@pytest.mark.parametrize("phase", ["install", "check_environment", "build", "test"])
def test_fixed_phase_result_validates_real_exit_and_matching_metadata(api: Any, phase: str) -> None:
    assert api.python_phase_detail(result(phase), phase=phase, import_module="billing") is not None
    for bad in (
        result(phase, exit_code=1),
        result(phase, timed_out=True),
        result(phase, stdout_truncated=True),
        result("test" if phase != "test" else "install"),
        result(phase, stdout='{"status":"completed"}'),
    ):
        assert api.python_phase_detail(bad, phase=phase, import_module="billing") is None


def test_verbose_stderr_is_not_an_execution_failure(api: Any) -> None:
    observed = result(
        "build",
        stderr="head\n[truncated]\ntail",
        stderr_truncated=True,
        stderr_observed_bytes=200000,
    )
    assert api.python_phase_detail(observed, phase="build", import_module="billing") is not None


def test_phase_count_and_import_are_strict_not_truthy_metadata(api: Any) -> None:
    for patch in (
        {"test_count": True},
        {"test_count": 0},
        {"import": {"module": "different", "origin": "billing/__init__.py"}},
        {"import": {"module": "billing", "origin": "/host/module.py"}},
    ):
        observed = result("test")
        observed.stdout = json.dumps(json.loads(observed.stdout) | patch)
        assert api.python_phase_detail(observed, phase="test", import_module="billing") is None


def test_result_body_cannot_add_an_execution_command_or_claim_business_proof(api: Any) -> None:
    for patch in ({"command": "shell"}, {"evidence": "business_correctness"}, {"extra": 1}):
        observed = result("install")
        observed.stdout = json.dumps(json.loads(observed.stdout) | patch)
        assert api.python_phase_detail(observed, phase="install", import_module="billing") is None


def test_outer_runner_refuses_unisolated_invocation_before_touching_files(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX", raising=False)
    with pytest.raises(ValueError, match="python_project_runner_rejected"):
        api.run_python_project(None)


def test_input_read_atime_is_not_a_content_change(api: Any) -> None:
    attributes = {
        "st_dev": 1,
        "st_ino": 2,
        "st_uid": 0,
        "st_gid": 0,
        "st_mode": 0o100444,
        "st_nlink": 1,
        "st_size": 100,
        "st_mtime_ns": 4,
        "st_ctime_ns": 5,
        "st_atime_ns": 6,
    }
    initial = SimpleNamespace(**attributes)
    assert api._file_identity(initial) == api._file_identity(
        SimpleNamespace(**(attributes | {"st_atime_ns": 7}))
    )
    assert api._file_identity(initial) != api._file_identity(
        SimpleNamespace(**(attributes | {"st_mtime_ns": 7}))
    )


def plan_payload(*, reuse: bool = False) -> dict[str, Any]:
    inputs = bind(importlib.import_module("agent_hub.capabilities.python_project_inputs"))
    invocation = {
        "execution_id": "python_current",
        "tenant_id": inputs.contract.tenant_id,
        "run_id": inputs.contract.run_id,
        "project_id": inputs.contract.project_id,
        "session_id": inputs.contract.session_id,
        "original_reference": inputs.original_reference.to_payload(),
        "sandbox_profile": "workspace_write",
        "timeout_seconds": 180,
        "source_kind": "original",
        "install_allowed": not reuse,
        "recipe_sha256": inputs.sha256,
        "environment_input_sha256": inputs.environment_input_sha256,
    }
    payload = {
        "invocation": invocation,
        "inputs": inputs.to_payload(),
        "source_sha256": inputs.original_reference.input_source_sha256,
    }
    if reuse:
        payload["reused_environment"] = {
            "scope": {
                key: invocation[key]
                for key in (
                    "tenant_id",
                    "run_id",
                    "project_id",
                    "session_id",
                    "original_reference",
                )
            },
            "recipe_sha256": inputs.sha256,
            "environment_input_sha256": inputs.environment_input_sha256,
            "environment_sha256": "e" * 64,
            "security_manifest_sha256": "f" * 64,
            "origin_execution_id": "python_previous",
        }
    return payload


@pytest.mark.parametrize("reuse", [False, True])
def test_plan_roundtrips_actual_inputs_without_turning_reuse_into_install_authority(
    api: Any, reuse: bool
) -> None:
    payload = plan_payload(reuse=reuse)
    plan = api.PythonProjectRunnerPlan.model_validate(payload, strict=True)
    assert (
        api.PythonProjectRunnerPlan.model_validate_json(plan.model_dump_json(), strict=True) == plan
    )
    assert plan.invocation.install_allowed is not reuse
    assert (plan.reused_environment is not None) == reuse


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", UUID(int=99)),
        ("run_id", UUID(int=99)),
        ("project_id", "sibling"),
        ("session_id", "sibling"),
        ("recipe_sha256", "a" * 64),
        ("environment_input_sha256", "a" * 64),
        (
            "original_reference",
            {
                "schema_version": 1,
                "contract_sha256": "a" * 64,
                "input_source_sha256": "b" * 64,
                "file_count": 4,
            },
        ),
    ],
)
def test_plan_rejects_scope_reference_and_input_binding_changes(
    api: Any, field: str, value: object
) -> None:
    payload = plan_payload()
    payload["invocation"][field] = value
    with pytest.raises(ValueError):
        api.PythonProjectRunnerPlan.model_validate(payload, strict=True)


@pytest.mark.parametrize("level", ["plan", "inputs", "invocation", "reference"])
def test_plan_rejects_unknown_fields_at_each_authority_boundary(api: Any, level: str) -> None:
    payload = plan_payload(reuse=True)
    target = {
        "plan": payload,
        "inputs": payload["inputs"],
        "invocation": payload["invocation"],
        "reference": payload["reused_environment"],
    }[level]
    target["model_path"] = "/host/private"
    with pytest.raises(ValueError):
        api.PythonProjectRunnerPlan.model_validate(payload, strict=True)


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "run_id",
        "project_id",
        "session_id",
        "original_reference",
        "recipe_sha256",
        "environment_input_sha256",
        "origin_execution_id",
    ],
)
def test_reused_reference_is_bound_to_scope_inputs_and_a_previous_execution(
    api: Any, field: str
) -> None:
    payload = plan_payload(reuse=True)
    reference = payload["reused_environment"]
    if field in {"tenant_id", "run_id"}:
        reference["scope"][field] = UUID(int=99)
    elif field in {"project_id", "session_id"}:
        reference["scope"][field] = "sibling"
    elif field == "original_reference":
        reference["scope"][field] = reference["scope"][field] | {"contract_sha256": "a" * 64}
    elif field == "origin_execution_id":
        reference[field] = payload["invocation"]["execution_id"]
    else:
        reference[field] = "a" * 64
    with pytest.raises(ValueError):
        api.PythonProjectRunnerPlan.model_validate(payload, strict=True)


def test_fresh_environment_without_explicit_install_permission_is_rejected(api: Any) -> None:
    payload = plan_payload()
    payload["invocation"]["install_allowed"] = False
    with pytest.raises(ValueError):
        api.PythonProjectRunnerPlan.model_validate(payload, strict=True)


def test_original_plan_cannot_relabel_a_different_source_as_the_original(api: Any) -> None:
    payload = plan_payload()
    payload["source_sha256"] = "9" * 64
    with pytest.raises(ValueError):
        api.PythonProjectRunnerPlan.model_validate(payload, strict=True)
    payload["invocation"]["source_kind"] = "candidate"
    assert (
        api.PythonProjectRunnerPlan.model_validate(payload, strict=True).source_sha256 == "9" * 64
    )


@pytest.mark.parametrize(
    "operation", ["_seal_environment", "_wheel_actual", "main", "run_python_project"]
)
def test_native_entrypoints_reject_windows_before_filesystem_access(
    api: Any, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setattr(api, "sys", SimpleNamespace(platform="win32", argv=["runner"]))
    args: tuple[object, ...]
    kwargs: dict[str, Any]
    if operation == "_seal_environment":
        args, kwargs = (Path("/never-read"),), {"deadline": 100.0}
    elif operation == "_wheel_actual":
        args, kwargs = (Path("/never-read"), {}), {"deadline": 100.0}
    elif operation == "run_python_project":
        args, kwargs = (None,), {}
    else:
        args, kwargs = (), {}
    with pytest.raises(ValueError, match="^python_project_runner_rejected$"):
        getattr(api, operation)(*args, **kwargs)


def test_cli_accepts_no_model_selected_plan_argument(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("CLI opened a path before validating its fixed invocation")

    monkeypatch.setattr(
        api, "sys", SimpleNamespace(platform="linux", argv=["runner", "--plan", "/other"])
    )
    monkeypatch.setattr(
        api,
        "os",
        SimpleNamespace(
            environ={"AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX": "systemd"}, open=forbidden
        ),
    )
    with pytest.raises(ValueError, match="^python_project_runner_rejected$"):
        api.main()


def orchestrator(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reuse: bool = False,
    failed_phase: str | None = None,
    fault: str | None = None,
    file_limit: int = 10000,
) -> Any:
    """Pure orchestration fixture: no systemd, bwrap, environment Python or hooks."""
    payload = plan_payload(reuse=reuse)
    payload["invocation"]["file_limit"] = file_limit
    plan = api.PythonProjectRunnerPlan.model_validate(payload, strict=True)
    events: list[str] = []
    phases: list[str] = []
    paths = {
        name: tmp_path / name.lower()
        for name in ("INPUT", "ORIGINAL", "RUNTIME", "WHEELS", "REUSED", "EXPORT", "WORK")
    }
    for name, path in paths.items():
        monkeypatch.setattr(api, "_" + name, path)
        if name != "WORK":
            path.mkdir()
    monkeypatch.setattr(api, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(
        api,
        "os",
        SimpleNamespace(
            environ={"AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX": "systemd"},
            getuid=lambda: 1000,
        ),
    )

    def snapshot(source: Path, destination: Path, deadline: float) -> str:
        assert source == paths["INPUT"]
        destination.mkdir()
        (destination / "dummy-source").write_bytes(b"opaque source bytes")
        digest = plan.source_sha256
        assert isinstance(digest, str)
        return digest

    @contextmanager
    def mounts(phase: str, **kwargs: Any) -> Iterator[Any]:
        assert (
            kwargs["runtime_root"] == paths["RUNTIME"] and kwargs["wheel_root"] == paths["WHEELS"]
        )
        assert kwargs["environment_root"] == (
            paths["REUSED"] if reuse else paths["WORK"] / "environment"
        )
        events.append("mount:" + phase)
        try:
            yield SimpleNamespace(argv=(phase,), pass_fds=())
        finally:
            events.append("closed:" + phase)

    def phase(command: tuple[str, ...], **kwargs: Any) -> Any:
        selected = command[0]
        phases.append(selected)
        if fault == "cleanup" and selected == "test":
            raise PythonPhaseCleanupError("phase cleanup incomplete")
        if fault == "source" and selected == "test":
            (paths["WORK"] / "source" / "dummy-source").write_bytes(b"changed")
        return result(
            selected,
            exit_code=1 if selected == failed_phase else 0,
            stderr="[truncated] test output",
            stderr_truncated=True,
        )

    def normalize(root: Path, **kwargs: Any) -> Any:
        assert not reuse and kwargs["normalize_bytecode"] is True
        _scan_limits(kwargs["limits"])
        events.append("normalize")
        return SimpleNamespace(environment_sha256="e" * 64)

    def seal(root: Path, **kwargs: Any) -> None:
        assert "normalize" in events and "closed:install" in events
        events.append("seal")

    def observe(root: Path, input_sha: str, limits: ScanLimits, deadline: float) -> Any:
        _scan_limits(limits)
        assert input_sha == plan.invocation.environment_input_sha256
        assert reuse or "seal" in events
        events.append("observe")
        digest = "d" * 64 if fault == "environment" and events.count("observe") > 1 else "e" * 64
        return SimpleNamespace(environment_sha256=digest, security_manifest={"receipt": 1})

    def protect(**kwargs: Any) -> None:
        events.append("protect")
        assert kwargs["expected_source_sha256"] == plan.source_sha256
        assert kwargs["expected_contract_sha256"] == plan.inputs["contract"]["protected_sha256"]
        if (paths["WORK"] / "source" / "dummy-source").read_bytes() != b"opaque source bytes":
            raise ValueError("protected source changed")

    def export(source: Path, destination: Path, **kwargs: Any) -> None:
        assert "protect" in events and "seal" in events
        assert kwargs["expected_environment_sha256"] == "e" * 64
        events.append("export")
        destination.mkdir()
        (destination / "dummy-installed").write_bytes(b"installed dependency bytes")

    monkeypatch.setattr(api, "_copy_snapshot", snapshot)
    monkeypatch.setattr(api, "build_python_phase_mounts", mounts)
    monkeypatch.setattr(api, "run_python_phase", phase)
    monkeypatch.setattr(api, "observe_python_project_environment", normalize)
    monkeypatch.setattr(api, "_seal_environment", seal)
    monkeypatch.setattr(api, "_observe", observe)
    monkeypatch.setattr(api, "validate_contract_work", protect)
    monkeypatch.setattr(api, "copy_python_project_environment", export)
    monkeypatch.setattr(api, "_wheel_actual", lambda output, wheel, **kwargs: dict(wheel))
    return SimpleNamespace(plan=plan, events=events, phases=phases, paths=paths)


@pytest.mark.parametrize("failed_phase", ["install", "build", "test", None])
def test_fresh_install_failure_has_no_export_but_later_failure_preserves_completed_dependencies(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_phase: str | None,
) -> None:
    fixture = orchestrator(api, tmp_path, monkeypatch, failed_phase=failed_phase)
    report = api.run_python_project(fixture.plan)
    exported = fixture.paths["EXPORT"] / "ready" / "environment" / "dummy-installed"
    assert report["status"] == ("passed" if failed_phase is None else "failed")
    assert report["exported_environment"] is (failed_phase != "install")
    assert exported.is_file() is (failed_phase != "install")
    assert report["environment_sha256"] == (None if failed_phase == "install" else "e" * 64)
    assert "environment_reference" not in report
    assert fixture.phases[0] == "install"
    if failed_phase != "install":
        assert (
            fixture.events.index("normalize")
            < fixture.events.index("seal")
            < fixture.events.index("export")
        )


def test_reuse_runs_only_pip_check_and_never_reinstalls_or_reexports(
    api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = orchestrator(api, tmp_path, monkeypatch, reuse=True)
    report = api.run_python_project(fixture.plan)
    assert fixture.phases == ["check_environment", "build", "test"]
    assert report["status"] == "passed" and report["exported_environment"] is False
    assert report["dependency_install"] == {
        "mode": "reused",
        "origin_execution_id": "python_previous",
    }
    assert not (fixture.paths["EXPORT"] / "ready").exists()
    assert not {"normalize", "seal", "export"} & set(fixture.events)


def test_failed_reuse_dependency_check_has_no_environment_receipt_to_publish(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = orchestrator(api, tmp_path, monkeypatch, reuse=True, failed_phase="check_environment")
    report = api.run_python_project(fixture.plan)
    assert fixture.phases == ["check_environment"]
    assert report["status"] == "failed" and report["checks"][0]["status"] == "failed"
    assert report["environment_sha256"] is None
    assert report["exported_environment"] is False
    assert not (fixture.paths["EXPORT"] / "ready").exists()


@pytest.mark.parametrize("fault", ["source", "environment", "cleanup"])
def test_source_integrity_environment_change_and_unconfirmed_cleanup_never_export(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    fixture = orchestrator(api, tmp_path, monkeypatch, fault=fault)
    with pytest.raises((ValueError, RuntimeError)):
        api.run_python_project(fixture.plan)
    assert not (fixture.paths["EXPORT"] / "ready").exists()


def test_maximum_valid_source_file_quota_still_uses_supported_environment_scan_limits(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = orchestrator(api, tmp_path, monkeypatch, file_limit=100000)
    assert api.run_python_project(fixture.plan)["status"] == "passed"
