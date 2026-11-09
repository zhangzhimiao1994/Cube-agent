"""Root worker contracts/data flow; mocked IO is never native sandbox evidence."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import os
import signal
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.capabilities.project_baselines import StoredProjectBaseline
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.python_project_process import (
    PythonPhaseCleanupError,
    PythonPhaseProcessResult,
    python_native_report_limit,
    python_phase_capture_limit,
)
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonEnvironmentReference,
    PythonProjectBrokerRequest,
    PythonProjectInspectEnvironmentRequest,
    PythonProjectProbeInvocation,
    PythonProjectProbeRequest,
    PythonProjectScope,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.capabilities.test_python_project_recipe import LOCK, MANIFEST
from tests.unit.skills.sandbox.test_python_project_staging import fixture


def test_backend_module_exists() -> None:
    assert importlib.util.find_spec("agent_hub.skills.sandbox.python_project_backend") is not None


@pytest.fixture
def api() -> Any:
    return importlib.import_module("agent_hub.skills.sandbox.python_project_backend")


def configuration_payload() -> dict[str, Any]:
    recipe, _, _, _ = fixture()
    return {
        "schema_version": 1,
        "runtime_root": "/opt/python-project-runtime",
        "interpreter": recipe.interpreter.to_payload(),
        "wheel_root": "/opt/python-project-wheels",
        "environment_root": "/var/lib/python-project-cache",
        "stage_root": "/var/lib/python-project-work",
        "cache_policy": {
            "max_bytes": 2**30,
            "max_entries": 8,
            "disk_reserve_bytes": 2**20,
            "retention_seconds": 7200,
        },
        "runtime_max_bytes": 2**29,
        "wheel_max_bytes": 2**28,
        "max_environment_bytes": 2**28,
    }


def test_root_configuration_is_fixed_pure_data_without_installer_authority(api: Any) -> None:
    payload = configuration_payload()
    configured = api.RootConfig.from_payload(payload)
    assert configured.to_payload() == payload
    assert api._CONFIG_PATH.as_posix() == "/etc/agent-hub/python-project.json"
    assert configured.interpreter.to_payload() == payload["interpreter"]
    assert configured.stage_root.as_posix() == payload["stage_root"]


@pytest.mark.parametrize("filesystem", ["tmpfs", "ramfs", "devtmpfs", "hugetlbfs"])
def test_persistent_disk_check_uses_the_held_fd_mount_not_the_root_mount(
    api: Any, filesystem: str
) -> None:
    mountinfo = (
        f"1 0 8:1 / / rw - ext4 /dev/sda1 rw\n42 1 0:2 / /work rw - {filesystem} none rw\n"
    ).encode()
    with pytest.raises(api.PythonProjectBackendError, match="python_project_environment_quota"):
        api._mount_type(b"pos:\t0\nmnt_id:\t42\n", mountinfo)
    assert api._mount_type(b"mnt_id:\t1\n", mountinfo) == "ext4"


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "url",
        "relative",
        "parent",
        "alias",
        "overlap",
        "boolean-budget",
        "schema-bool",
        "unbounded",
        "metadata-space",
        "policy-extra",
        "pins-extra",
    ],
)
def test_configuration_rejects_coercion_aliases_authority_and_unbounded_values(
    api: Any,
    change: str,
) -> None:
    payload = configuration_payload()
    if change == "extra":
        payload["install_allowed"] = True
    elif change in {"url", "relative", "parent", "alias"}:
        payload["runtime_root"] = {
            "url": "https://example.invalid/runtime",
            "relative": "runtime",
            "parent": "/opt/../runtime",
            "alias": "/opt//runtime",
        }[change]
    elif change == "overlap":
        payload["wheel_root"] = payload["runtime_root"] + "/wheels"
    elif change == "boolean-budget":
        payload["max_environment_bytes"] = True
    elif change == "schema-bool":
        payload["schema_version"] = True
    elif change == "unbounded":
        payload["runtime_max_bytes"] = 2**63
    elif change == "metadata-space":
        payload["max_environment_bytes"] = payload["cache_policy"]["max_bytes"]
    elif change == "policy-extra":
        payload["cache_policy"]["command"] = "installer"
    else:
        payload["interpreter"]["url"] = "https://example.invalid/python"
    with pytest.raises(api.PythonProjectBackendError):
        api.RootConfig.from_payload(payload)


def test_wrong_protocol_is_rejected_before_native_or_path_access(api: Any, tmp_path: Path) -> None:
    with pytest.raises(api.PythonProjectBackendError, match="python_project_request_invalid"):
        api.execute_python_project_request(
            {"version": 3},
            policy=BrokerPolicy(),
            configuration=api.RootConfig.from_payload(configuration_payload()),
            stage=tmp_path,
        )
    assert list(tmp_path.iterdir()) == []


def test_non_linux_returns_finite_prerequisite_without_staging(api: Any, tmp_path: Path) -> None:
    if sys.platform == "linux":
        pytest.skip("unsupported host only")
    _, invocation, _, _ = fixture()
    request = PythonProjectBrokerRequest(invocation=invocation)
    response = api.execute_python_project_request(
        request,
        policy=BrokerPolicy(),
        configuration=api.RootConfig.from_payload(configuration_payload()),
        stage=tmp_path,
    )
    assert not response.ok and response.error == "python_project_unsupported"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("phase", ["policy", "configuration", "execution"])
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError,
        ValueError,
        PermissionError,
        FileNotFoundError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        AssertionError,
    ],
)
def test_cli_does_not_disguise_unknown_errors_as_missing_runtime(
    api: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    phase: str,
    error: type[Exception],
) -> None:
    # Only exception routing is injected; this is not a native root filesystem test.
    _, invocation, _, _ = fixture()
    request = PythonProjectBrokerRequest(invocation=invocation)
    configured = api.RootConfig.from_payload(configuration_payload())
    original_stat = os.stat

    def control_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if path == "request.json" and kwargs.get("dir_fd") == -1000:
            return SimpleNamespace(st_size=100)
        return original_stat(path, *args, **kwargs)

    def failed(*args: Any, **kwargs: Any) -> Any:
        raise error("secret /private/project source body")

    monkeypatch.setattr(api, "_native", lambda: None)
    monkeypatch.setattr(api, "_path", lambda value: tmp_path)
    monkeypatch.setattr(api, "_root", lambda *args, **kwargs: -1000)
    monkeypatch.setattr(api.os, "stat", control_stat)
    monkeypatch.setattr(api, "_read_control", lambda *args: request.model_dump_json().encode())
    monkeypatch.setattr(api, "load_broker_policy", lambda: BrokerPolicy())
    monkeypatch.setattr(api, "load_root_configuration", lambda: configured)
    monkeypatch.setattr(api, "execute_python_project_request", failed)
    if phase == "policy":
        monkeypatch.setattr(api, "load_broker_policy", failed)
    elif phase == "configuration":
        monkeypatch.setattr(api, "load_root_configuration", failed)
    result = api.main(["--stage", str(tmp_path)])
    captured = capsys.readouterr()
    if phase == "policy":
        assert result == 2 and captured.out == ""
    else:
        assert result == 0
        response = api.PythonProjectBrokerResponse.model_validate_json(captured.out, strict=True)
        assert response.error == "python_project_execution_unavailable"
    assert "secret" not in captured.out + captured.err
    assert "/private" not in captured.out + captured.err
    diagnostic = json.loads(captured.err.splitlines()[0])["python_project_diagnostic"]
    assert diagnostic == {"stage": phase, "exception_type": error.__name__}


def test_cli_preserves_actual_missing_configuration_error(
    api: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def missing() -> Any:
        raise api.PythonProjectBackendError("python_project_runtime_missing")

    # A real missing configuration is classified by the fixed loader, not a broad catch.
    monkeypatch.setattr(api, "_native", missing)
    assert api.main(["--stage", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "python_project_runtime_missing" in captured.err


@pytest.mark.parametrize(
    "error,code",
    [
        (FileNotFoundError, "python_project_runtime_missing"),
        (PermissionError, "python_project_runtime_untrusted"),
        (ValueError, "python_project_runtime_untrusted"),
        (RuntimeError, "python_project_runtime_untrusted"),
    ],
)
def test_fixed_loader_retains_safe_failure_diagnostics(
    api: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: type[Exception],
    code: str,
) -> None:
    def failed(*args: Any, **kwargs: Any) -> Any:
        raise error("secret /private/configuration")

    monkeypatch.setattr(api, "_native", lambda: None)
    monkeypatch.setattr(api, "_root_native", lambda: None)
    monkeypatch.setattr(api, "_root", failed)
    with pytest.raises(api.PythonProjectBackendError, match=f"^{code}$"):
        api.load_root_configuration()
    captured = capsys.readouterr()
    assert json.loads(captured.err)["python_project_diagnostic"] == {
        "stage": "configuration",
        "exception_type": error.__name__,
    }
    assert captured.out == "" and "secret" not in captured.err


@pytest.mark.parametrize("has_threads", [False, True])
def test_parent_can_read_root_metadata_but_cannot_execute_in_loop_or_threads(
    api: Any,
    monkeypatch: pytest.MonkeyPatch,
    has_threads: bool,
) -> None:
    touched: list[str] = []

    def missing(*args: Any, **kwargs: Any) -> Any:
        touched.append("metadata")
        raise FileNotFoundError("private configuration")

    # Native authority is not claimed: isolate the loop/thread guard from FD IO.
    monkeypatch.setattr(api, "_root_native", lambda: None, raising=False)
    monkeypatch.setattr(api.threading, "active_count", lambda: 2 if has_threads else 1)
    monkeypatch.setattr(api, "_root", missing)

    async def parent() -> None:
        with pytest.raises(api.PythonProjectBackendError, match="python_project_runtime_missing"):
            api.read_root_configuration()
        with pytest.raises(api.PythonProjectBackendError, match="python_project_unsupported"):
            api.load_root_configuration()
        with pytest.raises(api.PythonProjectBackendError, match="python_project_unsupported"):
            api._native()

    asyncio.run(parent())
    assert touched == ["metadata"]


def expected_reference(invocation: Any, *, sha: str = "a" * 64) -> PythonEnvironmentReference:
    return PythonEnvironmentReference(
        scope=PythonProjectScope(
            tenant_id=invocation.tenant_id,
            run_id=invocation.run_id,
            project_id=invocation.project_id,
            session_id=invocation.session_id,
            original_reference=invocation.original_reference,
        ),
        recipe_sha256=invocation.recipe_sha256,
        environment_input_sha256=invocation.environment_input_sha256,
        environment_sha256=sha,
        security_manifest_sha256="b" * 64,
        origin_execution_id="first",
    )


def report(
    invocation: Any, recipe: Any, *, failed: str | None = None, reused: bool = False
) -> dict[str, Any]:
    checks = []
    for phase in ("install", "build", "test"):
        detail: dict[str, Any] = {
            "status": "completed",
            "phase": "check_environment" if phase == "install" and reused else phase,
            "evidence": "native_process_only",
        }
        if phase == "install":
            detail["requires_dist_check"] = "completed"
        elif phase == "build":
            detail["wheel"] = {
                "filename": "billing-1.0.0-py3-none-any.whl",
                "sha256": "c" * 64,
                "size": 100,
            }
        else:
            detail.update(
                test_count=5, **{"import": {"module": "billing", "origin": "billing/__init__.py"}}
            )
        stdout = json.dumps(detail) if phase != failed else ""
        checks.append(
            {
                "phase": phase,
                "status": "failed" if phase == failed else "passed",
                "exit_code": 1 if phase == failed else 0,
                "timed_out": False,
                "stdout": stdout,
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
                "stdout_observed_bytes": len(stdout.encode()),
                "stderr_observed_bytes": 0,
            }
        )
        if phase == failed:
            break
    return {
        "schema_version": 2,
        "kind": "python_project",
        "evidence": "native_process_only",
        "status": "failed" if failed else "passed",
        "execution_id": invocation.execution_id,
        "source_sha256": recipe.original_reference.input_source_sha256,
        "recipe_sha256": recipe.sha256,
        "environment_input_sha256": recipe.environment_input_sha256,
        "environment_sha256": None if failed == "install" else "a" * 64,
        "original_check_contract": recipe.contract.protected_sha256,
        "runtime": recipe.interpreter.to_payload(),
        "checks": checks,
        "wheel": None
        if failed in {"install", "build"}
        else {"filename": "billing-1.0.0-py3-none-any.whl", "sha256": "c" * 64, "size": 100},
        "test_count": 5 if failed is None else None,
        "exported_environment": not reused and failed != "install",
        "dependency_install": {
            "mode": "reused" if reused else "created",
            "origin_execution_id": "first" if reused else invocation.execution_id,
        },
    }


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "scope-execution",
        "recipe",
        "runtime",
        "contract",
        "native-ref",
        "success-lie",
        "phase-order",
        "truncated-success",
        "bad-count",
        "export-lie",
    ],
)
def test_runner_report_is_bound_and_cannot_issue_root_references(api: Any, change: str) -> None:
    recipe, invocation, _, _ = fixture()
    payload = report(invocation, recipe)
    if change in {"source", "scope-execution", "recipe", "runtime", "contract"}:
        field = {
            "source": "source_sha256",
            "scope-execution": "execution_id",
            "recipe": "recipe_sha256",
            "runtime": "runtime",
            "contract": "original_check_contract",
        }[change]
        payload[field] = {} if field == "runtime" else "different"
    elif change == "native-ref":
        payload["environment_reference"] = expected_reference(invocation).model_dump(mode="json")
    elif change == "success-lie":
        payload["checks"][1]["status"] = "failed"
    elif change == "phase-order":
        payload["checks"].reverse()
    elif change == "truncated-success":
        payload["checks"][0]["stdout_truncated"] = True
    elif change == "bad-count":
        payload["test_count"] = True
    else:
        payload["exported_environment"] = False
    with pytest.raises(api.PythonProjectBackendError):
        api._report(
            json.dumps(payload),
            invocation,
            recipe,
            recipe.original_reference.input_source_sha256,
            None,
        )


@pytest.mark.parametrize("output", [1, 262144, 1048576])
def test_native_report_uses_shared_wire_budget_not_raw_log_budget(api: Any, output: int) -> None:
    recipe, invocation, _, _ = fixture()
    invocation = invocation.model_copy(update={"output_limit_bytes": output})
    payload = report(invocation, recipe, failed="install")
    check = payload["checks"][0]
    check["stderr"] = "\x00" * python_phase_capture_limit(output)
    check["stderr_observed_bytes"] = len(check["stderr"])
    raw = json.dumps(payload)
    assert len(raw.encode()) < python_native_report_limit(output)
    assert (
        api._report(raw, invocation, recipe, recipe.original_reference.input_source_sha256, None)
        == payload
    )
    raw += " " * (python_native_report_limit(output) + 1 - len(raw.encode()))
    with pytest.raises(api.PythonProjectBackendError):
        api._report(raw, invocation, recipe, recipe.original_reference.input_source_sha256, None)


def test_native_report_rejects_phase_log_over_shared_capture_limit(api: Any) -> None:
    recipe, invocation, _, _ = fixture()
    invocation = invocation.model_copy(update={"output_limit_bytes": 1048576})
    payload = report(invocation, recipe, failed="install")
    check = payload["checks"][0]
    check["stderr"] = "x" * (python_phase_capture_limit(invocation.output_limit_bytes) + 1)
    check["stderr_observed_bytes"] = len(check["stderr"])
    with pytest.raises(api.PythonProjectBackendError):
        api._report(
            json.dumps(payload),
            invocation,
            recipe,
            recipe.original_reference.input_source_sha256,
            None,
        )


def stubbed_context(api: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Any, ...]:
    """Use actual declarations and handlers; stub privileged IO, never execute a hook."""
    recipe, invocation, original, _ = fixture()
    source = tmp_path / "original"
    source.mkdir()
    (source / "pyproject.toml").write_bytes(MANIFEST)
    (source / "requirements.lock").write_bytes(LOCK)
    original = StoredProjectBaseline(
        original.reference, original.contract, source, original.source_files
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    events: list[str] = []

    @contextmanager
    def roots(*args: Any, **kwargs: Any) -> Any:
        yield SimpleNamespace(stage_root_fd=9, stage_fd=10, cache_fd=11, pins=[])

    @contextmanager
    def admission(*args: Any, **kwargs: Any) -> Any:
        yield

    def runtime(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["interpreter_relative"] == "bin/python3.12"
        events.append("runtime")
        return SimpleNamespace(
            binary_sha256=recipe.interpreter.binary_sha256,
            content_sha256=recipe.interpreter.base_runtime_sha256,
        )

    def wheels(*args: Any, **kwargs: Any) -> Any:
        events.append("wheels")
        return recipe.wheels

    # Native product IO is separately covered; this fixture models privileged boundaries.
    monkeypatch.setattr(api, "_minimum_environment_bytes", lambda *args: 3)
    monkeypatch.setattr(api, "retain_python_product", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_native", lambda: None)
    monkeypatch.setattr(api, "_roots", roots)
    monkeypatch.setattr(api, "_admission", admission, raising=False)
    monkeypatch.setattr(api, "_disk_budget", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "stage_original_contract", lambda *args, **kwargs: original)
    monkeypatch.setattr(
        api,
        "stage_python_project_inputs",
        lambda **kwargs: SimpleNamespace(original=original, inputs=recipe),
    )
    monkeypatch.setattr(api, "observe_python_distribution", runtime)
    monkeypatch.setattr(api, "observe_python_wheel_cache", wheels)
    monkeypatch.setattr(
        api,
        "stage_python_project_wheels",
        lambda *args, **kwargs: stage / "selected-wheels",
        raising=False,
    )
    monkeypatch.setattr(
        api, "verify_staged_python_wheels", lambda *args, **kwargs: None, raising=False
    )

    # Command construction has native tests elsewhere; these tests isolate data flow.
    def command(item: Any, *args: Any, **kwargs: Any) -> tuple[str, ...]:
        assert kwargs["wheels"] == stage / "selected-wheels"
        return (
            "/usr/bin/systemd-run",
            "--unit",
            f"agent-hub-project-{item.execution_id}",
        )

    monkeypatch.setattr(api, "build_python_project_systemd_command", command)
    return recipe, invocation, original, stage, events


def admission_request(action: str, invocation: Any) -> Any:
    invocation = invocation.model_copy(update={"install_allowed": False})
    if action == "probe":
        return PythonProjectProbeRequest(
            invocation=PythonProjectProbeInvocation.model_validate(
                {
                    name: value
                    for name, value in invocation.model_dump().items()
                    if name in PythonProjectProbeInvocation.model_fields
                },
                strict=True,
            )
        )
    if action == "inspect":
        return PythonProjectInspectEnvironmentRequest(
            invocation=invocation, expected_environment=expected_reference(invocation)
        )
    if action == "candidate":
        invocation = invocation.model_copy(update={"source_kind": "candidate"})
    return PythonProjectBrokerRequest(invocation=invocation)


@pytest.mark.parametrize("action", ["probe", "original", "candidate", "inspect"])
def test_disk_admission_rejects_before_staging_or_any_cache_write(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    _, invocation, _, stage, events = stubbed_context(api, monkeypatch, tmp_path)

    @contextmanager
    def admission(*args: Any, **kwargs: Any) -> Any:
        events.append("disk_lock")
        try:
            yield
        finally:
            events.append("disk_unlock")

    def denied(*args: Any, **kwargs: Any) -> Any:
        events.append("preflight")
        raise api.PythonProjectBackendError("python_project_environment_quota")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        events.append("forbidden_io")
        raise AssertionError("staging or cache write happened before admission")

    monkeypatch.setattr(api, "_admission", admission)
    monkeypatch.setattr(api, "_disk_budget", denied)
    monkeypatch.setattr(api, "stage_original_contract", forbidden)
    monkeypatch.setattr(api, "stage_python_project_inputs", forbidden)
    monkeypatch.setattr(api, "PythonProjectEnvironmentCache", forbidden)
    response = api.execute_python_project_request(
        admission_request(action, invocation),
        policy=BrokerPolicy(),
        configuration=api.RootConfig.from_payload(configuration_payload()),
        stage=stage,
    )
    assert response.error == "python_project_environment_quota"
    assert events == ["disk_lock", *["preflight"] * (2 if action in {"original", "candidate"} else 1),
                      "disk_unlock"]
    assert list(stage.iterdir()) == []


@pytest.mark.parametrize("action", ["probe", "original", "candidate", "inspect"])
def test_disk_lock_spans_preflight_through_handler_and_uses_source_copy_budget(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    _, invocation, _, stage, events = stubbed_context(api, monkeypatch, tmp_path)
    request = admission_request(action, invocation)
    configured = api.RootConfig.from_payload(configuration_payload())

    @contextmanager
    def admission(parent: int, deadline: float) -> Any:
        assert parent == 9 and deadline > time.monotonic()
        events.append("disk_lock")
        try:
            yield
        finally:
            events.append("disk_unlock")

    def budget(config: Any, roots: Any, size: int, copying: bool, *, source_bytes: int = 0,
               product_bytes: int = 0) -> None:
        assert events == ["disk_lock"] and config == configured
        assert source_bytes == invocation.source_limit_bytes * (2 if action == "candidate" else 1)
        assert copying is False and size == 0
        assert product_bytes == (
            invocation.workspace_limit_bytes if action in {"original", "candidate"} else 0
        )
        events.append("preflight")

    def handler(*args: Any, **kwargs: Any) -> Any:
        assert events == ["disk_lock", "preflight"]
        events.append("handler_finished")
        return api.PythonProjectBrokerResponse(
            action=request.action,
            execution_id=invocation.execution_id,
            ok=False,
            error="python_project_recipe_unavailable",
        )

    monkeypatch.setattr(api, "_admission", admission)
    monkeypatch.setattr(api, "_disk_budget", budget)
    for name in ("_probe", "_run", "_inspect"):
        monkeypatch.setattr(api, name, handler)
    response = api.execute_python_project_request(
        request,
        policy=BrokerPolicy(),
        configuration=configured,
        stage=stage,
    )
    assert response.error == "python_project_recipe_unavailable"
    assert events == ["disk_lock", "preflight", "handler_finished", "disk_unlock"]


@pytest.mark.parametrize("same_device,shortfall,initial_free,reuse", [
    (True, None, 1_511_653_376, False), (False, None, 1_511_653_376, False),
    (True, None, 1_840_000_000, False), (True, None, 1_950_000_000, False),
    (True, None, 1_511_653_376, True),
    (True, "before_staging", 2**30, False), (True, "after_staging", 1_511_653_376, False),
])
def test_python_execution_uses_a_smaller_enforced_workspace_when_default_exceeds_free_disk(
    api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_device: bool,
    shortfall: str | None, initial_free: int, reuse: bool,
) -> None:
    real_disk_budget = api._disk_budget
    _, invocation, _, stage, _ = stubbed_context(api, monkeypatch, tmp_path)
    invocation = invocation.model_copy(update={"install_allowed": True})
    request = PythonProjectBrokerRequest(invocation=invocation)
    payload = configuration_payload()
    payload["max_environment_bytes"] = 192 * 2**20
    payload["cache_policy"]["disk_reserve_bytes"] = 2**30
    config = api.RootConfig.from_payload(payload)
    free = initial_free
    monkeypatch.setattr(api, "os", SimpleNamespace(
        fstat=lambda fd: SimpleNamespace(st_dev=1 if same_device or fd == 10 else 2),
        fstatvfs=lambda fd: SimpleNamespace(f_bavail=free, f_frsize=1),
    ))
    monkeypatch.setattr(api, "_disk_budget", real_disk_budget)
    staged: list[str] = []
    fitted: list[int] = []
    minimum = 92_684_832

    def snapshot(*args: Any) -> str:
        staged.append("source")
        return str(invocation.original_reference.input_source_sha256)

    def wheels(*args: Any, **kwargs: Any) -> Path:
        nonlocal free
        staged.append("wheels")
        if shortfall == "after_staging":
            free -= 100 * 2**20
        return Path(stage) / "selected-wheels"

    class Cache:
        @contextmanager
        def reserve(self, scoped: Any, **kwargs: Any) -> Any:
            assert staged == ["source", "wheels"]
            # Cache chooses freshness and verifies READY before the execution
            # limit changes, so an existing environment keeps its full bound.
            assert scoped == invocation
            assert kwargs["minimum_bytes"] == minimum
            env = min(config.max_environment_bytes, scoped.workspace_limit_bytes)
            assert kwargs["next_bytes"] == env
            selected = env if reuse else kwargs["fresh_budget"](env)
            assert minimum <= selected <= env
            reference = expected_reference(scoped) if reuse else None
            yield SimpleNamespace(current=lambda **opts: reference, limits=SimpleNamespace(max_bytes=selected))

    def plan(payload: Any, *_args: Any) -> Any:
        # The actual runner plan must use the smaller workspace and preserve
        # the rest of the admitted request, including the exact recipe.
        scoped = payload.invocation
        assert scoped.model_dump(exclude={"workspace_limit_bytes"}) == (
            invocation.model_dump(exclude={"workspace_limit_bytes"}))
        floor = config.max_environment_bytes if reuse else minimum
        assert floor <= scoped.workspace_limit_bytes < invocation.workspace_limit_bytes
        assert (payload.reused_environment is not None) is reuse
        fitted.append(scoped.workspace_limit_bytes)
        raise api.PythonProjectBackendError("python_project_recipe_unavailable")

    monkeypatch.setattr(api, "_minimum_environment_bytes", lambda *args: minimum)
    monkeypatch.setattr(api, "_source_snapshot", snapshot)
    monkeypatch.setattr(api, "stage_python_project_wheels", wheels)
    monkeypatch.setattr(api, "PythonProjectEnvironmentCache", lambda *args: Cache())
    monkeypatch.setattr(api, "_write_plan", plan)
    result = api.execute_python_project_request(request, policy=BrokerPolicy(), configuration=config, stage=stage)
    if shortfall is not None:
        assert result.error == "python_project_environment_quota" and fitted == []
        assert staged == (["source", "wheels"] if shortfall == "after_staging" else [])
    else:
        assert result.error == "python_project_recipe_unavailable" and len(fitted) == 1


@pytest.mark.parametrize("same_device,has_cache", [(True, True), (False, True), (False, False)])
def test_disk_budget_counts_source_and_both_environment_copies_on_actual_devices(
    api: Any,
    monkeypatch: pytest.MonkeyPatch,
    same_device: bool,
    has_cache: bool,
) -> None:
    # Inject only statvfs arithmetic; native authority and filesystem locking are not claimed.
    config = api.RootConfig.from_payload(configuration_payload())
    reserve = config.cache_policy.disk_reserve_bytes
    source, size = 250, 100
    copying = has_cache
    free = {
        10: reserve
        + source
        + api._STAGE_CONTROL_ALLOWANCE
        + (size * (2 if same_device else 1) if copying else 0),
        11: reserve + size + api._CONTROL_ALLOWANCE,
    }
    fake_os = SimpleNamespace(
        fstat=lambda fd: SimpleNamespace(st_dev=1 if fd == 10 or same_device else 2),
        fstatvfs=lambda fd: SimpleNamespace(f_bavail=free[fd], f_frsize=1),
    )
    monkeypatch.setattr(api, "os", fake_os)
    roots = SimpleNamespace(stage_fd=10, cache_fd=11 if has_cache else None)
    api._disk_budget(config, roots, size if copying else 0, copying, source_bytes=source)
    free[10] -= 1
    with pytest.raises(api.PythonProjectBackendError, match="python_project_environment_quota"):
        api._disk_budget(config, roots, size if copying else 0, copying, source_bytes=source)
    if has_cache and not same_device:
        free[10] += 1
        free[11] -= 1
        with pytest.raises(api.PythonProjectBackendError, match="python_project_environment_quota"):
            api._disk_budget(config, roots, size, copying, source_bytes=source)


def test_probe_selects_real_data_handler_without_reserving_or_launching(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipe, invocation, _, stage, events = stubbed_context(api, monkeypatch, tmp_path)
    probe = PythonProjectProbeInvocation.model_validate(
        {
            name: value
            for name, value in invocation.model_dump().items()
            if name in PythonProjectProbeInvocation.model_fields
        }
        | {"install_allowed": False},
        strict=True,
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("probe consumed a reservation or launched code")

    monkeypatch.setattr(api, "PythonProjectEnvironmentCache", forbidden)
    monkeypatch.setattr(api, "run_python_phase", forbidden)
    response = api.execute_python_project_request(
        PythonProjectProbeRequest(invocation=probe),
        policy=BrokerPolicy(),
        configuration=api.RootConfig.from_payload(configuration_payload()),
        stage=stage,
    )
    assert response.ok and response.observation is not None
    assert response.observation.declaration == recipe.declaration.to_payload()
    assert response.observation.interpreter == recipe.interpreter.to_payload()
    assert response.environment_reference is None and response.result is None
    assert events == ["runtime", "wheels", "runtime", "wheels"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", UUID(int=90)),
        ("run_id", UUID(int=91)),
        ("project_id", "sibling"),
        ("session_id", "sibling"),
    ],
)
def test_probe_refuses_staged_scope_drift_before_measurement(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    _, invocation, _, stage, events = stubbed_context(api, monkeypatch, tmp_path)
    probe = PythonProjectProbeInvocation.model_validate(
        {
            name: item
            for name, item in invocation.model_dump().items()
            if name in PythonProjectProbeInvocation.model_fields
        }
        | {"install_allowed": False, field: value},
        strict=True,
    )
    response = api.execute_python_project_request(
        PythonProjectProbeRequest(invocation=probe),
        policy=BrokerPolicy(),
        configuration=api.RootConfig.from_payload(configuration_payload()),
        stage=stage,
    )
    assert not response.ok and response.error == "python_project_recipe_unavailable"
    assert not events


def test_missing_service_configuration_is_finite_and_never_launches(
    api: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(api, "_native", lambda: None)

    def missing_policy() -> Any:
        raise KeyError("/private/account configuration secret")

    monkeypatch.setattr(api, "load_broker_policy", missing_policy)
    assert api.main(["--stage", "/var/lib/python-project-work/private"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    diagnostic, code = captured.err.splitlines()
    assert json.loads(diagnostic)["python_project_diagnostic"] == {
        "stage": "policy",
        "exception_type": "KeyError",
    }
    assert code == "python_project_execution_unavailable"


@pytest.mark.parametrize(
    "failed,reused",
    [
        (None, False),
        ("install", False),
        ("build", False),
        ("test", False),
        (None, True),
        ("install", True),
        ("test", True),
    ],
)
@pytest.mark.parametrize("cleanup_fails", [False, True, "process", "wheels"])
def test_run_quiesces_before_publication_and_keeps_failure_environment_branch_distinct(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed: str | None,
    reused: bool,
    cleanup_fails: bool | str,
) -> None:
    recipe, invocation, _, stage, events = stubbed_context(api, monkeypatch, tmp_path)
    if reused:
        invocation = invocation.model_copy(
            update={
                "execution_id": "candidate",
                "source_kind": "candidate",
                "install_allowed": False,
            }
        )
    cached = expected_reference(invocation) if reused else None
    payload = report(invocation, recipe, failed=failed, reused=reused)

    class Lease:
        path = Path("/var/lib/python-project-cache/key")
        limits = api.ScanLimits(min(api.RootConfig.from_payload(configuration_payload()).max_environment_bytes,
            invocation.workspace_limit_bytes))

        def record_install_failure(self, phase: Any, *, execution: Any, deadline: float) -> None:
            assert type(phase) is PythonPhaseProcessResult and phase.exit_code == 1
            assert (
                execution.quiet is True
                and execution.invocation.execution_id == invocation.execution_id
            )
            assert events.index("quiet") < events.index("empty_export")
            events.append("failed_quiescent")

        def current(self, **kwargs: Any) -> Any:
            events.append("current")
            return cached

        def publish(self, source: Path, **kwargs: Any) -> Any:
            assert "quiet" in events
            events.append("publish")
            return expected_reference(invocation).model_copy(
                update={"origin_execution_id": invocation.execution_id}
            )

    cache_api = importlib.import_module("agent_hub.skills.sandbox.python_project_cache")
    routing_cache = cache_api.PythonProjectEnvironmentCache(
        tmp_path, api.RootConfig.from_payload(configuration_payload()).cache_policy
    )

    @contextmanager
    def resources(*args: Any, **kwargs: Any) -> Any:
        events.append("lock")
        try:
            yield None
        finally:
            events.append("unlock")

    monkeypatch.setattr(routing_cache, "_lock_resources", resources)

    class Cache:
        @contextmanager
        def reserve(self, item: Any, **kwargs: Any) -> Any:
            assert item == invocation
            with routing_cache._locked(kwargs["deadline"]):
                yield Lease()

    def launch(*args: Any, **kwargs: Any) -> Any:
        events.append("launch")
        assert kwargs["capture_limit_bytes"] == python_native_report_limit(
            invocation.output_limit_bytes
        )
        if cleanup_fails == "process":
            raise PythonPhaseCleanupError("unconfirmed /private/process")
        return PythonPhaseProcessResult(0, False, json.dumps(payload), "", False, False, 1000, 0)

    def quiesce(*args: Any) -> None:
        events.append("quiet")
        if cleanup_fails is True:
            raise ProcessCleanupIncomplete("python_project_cleanup_incomplete")
        args[1].quiet = True

    monkeypatch.setattr(api, "PythonProjectEnvironmentCache", lambda *args: Cache())
    monkeypatch.setattr(
        api, "_source_snapshot", lambda *args: recipe.original_reference.input_source_sha256
    )
    monkeypatch.setattr(api, "_recheck_source", lambda *args: None)
    monkeypatch.setattr(api, "_write_plan", lambda *args: stage / "plan.json")
    monkeypatch.setattr(api, "_disk_budget", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_make_export", lambda *args: stage / "export")
    monkeypatch.setattr(api, "_quiesce", quiesce)
    monkeypatch.setattr(
        api, "_publish_export", lambda lease, *args, **kwargs: lease.publish(stage / "export")
    )
    monkeypatch.setattr(api, "_export_empty", lambda *args: events.append("empty_export"))
    monkeypatch.setattr(api, "run_python_phase", launch)
    if cleanup_fails == "wheels":

        def changed(*args: Any, **kwargs: Any) -> None:
            raise api.PythonWheelStageError("wheel_stage_changed")

        monkeypatch.setattr(api, "verify_staged_python_wheels", changed)
    request = PythonProjectBrokerRequest(invocation=invocation)
    if cleanup_fails and cleanup_fails != "wheels":
        with pytest.raises(ProcessCleanupIncomplete):
            api.execute_python_project_request(
                request,
                policy=BrokerPolicy(),
                configuration=api.RootConfig.from_payload(configuration_payload()),
                stage=stage,
            )
        assert "publish" not in events and stage.is_dir() and events[-1] == "unlock"
        return
    response = api.execute_python_project_request(
        request,
        policy=BrokerPolicy(),
        configuration=api.RootConfig.from_payload(configuration_payload()),
        stage=stage,
    )
    if cleanup_fails == "wheels":
        assert not response.ok and response.error == "python_project_wheel_integrity"
        assert "publish" not in events and "failed_quiescent" not in events
        return
    assert response.ok and response.result is not None
    assert json.loads(response.result.stdout)["status"] == ("failed" if failed else "passed")
    assert (response.environment_reference is not None) is (failed != "install")
    assert ("publish" in events) is (not reused and failed != "install")
    assert ("failed_quiescent" in events) is (not reused and failed == "install")
    assert events.index("launch") < events.index("quiet") < events.index("unlock")
    assert events[-1] == "unlock"


def test_inspect_calls_only_existing_cache_measurement_and_returns_exact_expected(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, invocation, _, stage, _ = stubbed_context(api, monkeypatch, tmp_path)
    expected = expected_reference(invocation)

    class Cache:
        def inspect(self, item: Any, reference: Any, **kwargs: Any) -> Any:
            assert item == invocation and reference == expected
            return expected

    monkeypatch.setattr(api, "PythonProjectEnvironmentCache", lambda *args: Cache())
    response = api.execute_python_project_request(
        PythonProjectInspectEnvironmentRequest(
            invocation=invocation, expected_environment=expected
        ),
        policy=BrokerPolicy(),
        configuration=api.RootConfig.from_payload(configuration_payload()),
        stage=stage,
    )
    assert response.ok and response.environment_reference == expected
    assert response.result is None


@pytest.mark.parametrize("confirmed", [False, True])
def test_quiescence_uses_existing_manager_confirmation_without_stage_cleanup(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    confirmed: bool,
) -> None:
    _, invocation, _, _ = fixture()
    job = api._Execution(
        api.base_python_invocation(invocation), None, stage=tmp_path, launched=True
    )
    sentinel = tmp_path / "keep"
    sentinel.write_bytes(b"UNKNOWN must be retained")
    calls: list[str] = []

    async def confirmation(self: Any, candidate: Any) -> None:
        assert candidate is job
        calls.append(candidate.unit)
        if not confirmed:
            raise RuntimeError("/private/manager details")

    monkeypatch.setattr(api.ProjectExecutionBroker, "_kill_and_confirm", confirmation)
    if confirmed:
        api._quiesce(BrokerPolicy(), job)
    else:
        with pytest.raises(ProcessCleanupIncomplete, match="^python_project_cleanup_incomplete$"):
            api._quiesce(BrokerPolicy(), job)
    assert job.quiet is confirmed
    assert calls == [f"agent-hub-project-{invocation.execution_id}.service"]
    assert sentinel.read_bytes() == b"UNKNOWN must be retained"


native = pytest.mark.skipif(
    sys.platform != "linux", reason="requires real root-owned Linux FD checks"
)


@native
def test_actual_admission_lock_blocks_with_deadline_and_releases_after_caller_error(
    api: Any,
    tmp_path: Path,
) -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires actual Linux root")
    tmp_path.chmod(0o700)
    parent = os.open(tmp_path, api._directory_flags())
    expected = RuntimeError("trusted caller")
    try:
        with pytest.raises(RuntimeError) as caught, api._admission(parent, time.monotonic() + 2):
            info = (tmp_path / "active.lock").lstat()
            assert info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600
            assert info.st_nlink == 1 and info.st_size == 0
            started = time.monotonic()
            with (
                pytest.raises(api.PythonProjectBackendError),
                api._admission(parent, time.monotonic() + 0.03),
            ):
                pytest.fail("another open description bypassed the admission lock")
            assert time.monotonic() - started < 1
            raise expected
        assert caught.value is expected
        with api._admission(parent, time.monotonic() + 2):
            pass
    finally:
        os.close(parent)


@native
@pytest.mark.parametrize("change", ["symlink", "hardlink", "mode", "directory", "nonempty"])
def test_actual_admission_rejects_unsafe_lock_without_mutating_target(
    api: Any,
    tmp_path: Path,
    change: str,
) -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires actual Linux root")
    tmp_path.chmod(0o700)
    target = tmp_path / "target"
    target.write_bytes(b"sentinel")
    target.chmod(0o600)
    lock = tmp_path / "active.lock"
    if change == "symlink":
        lock.symlink_to(target)
    elif change == "hardlink":
        os.link(target, lock)
    elif change == "directory":
        lock.mkdir(mode=0o700)
    else:
        lock.write_bytes(b"invalid" if change == "nonempty" else b"")
        lock.chmod(0o644 if change == "mode" else 0o600)
    before = target.stat()
    parent = os.open(tmp_path, api._directory_flags())
    try:
        with (
            pytest.raises((OSError, api.PythonProjectBackendError)),
            api._admission(parent, time.monotonic() + 2),
        ):
            pytest.fail("unsafe admission lock accepted")
    finally:
        os.close(parent)
    assert target.read_bytes() == b"sentinel"
    after = target.stat()
    assert (after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )


@native
def test_admission_remains_held_by_forked_observer_until_its_exit(api: Any, tmp_path: Path) -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires actual Linux root and fork/flock semantics")
    tmp_path.chmod(0o700)
    parent = os.open(tmp_path, api._directory_flags())
    read_fd, write_fd = os.pipe()
    pid = -1
    try:
        with api._admission(parent, time.monotonic() + 2):
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(write_fd)
                    os.read(read_fd, 1)
                finally:
                    os._exit(0)
        with (
            pytest.raises(api.PythonProjectBackendError),
            api._admission(parent, time.monotonic() + 0.03),
        ):
            pytest.fail("parent released admission while its forked observer still held it")
    finally:
        try:
            if pid > 0:
                os.write(write_fd, b"x")
                until = time.monotonic() + 2
                while os.waitpid(pid, os.WNOHANG)[0] == 0:
                    if time.monotonic() >= until:
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                        pytest.fail("forked observer did not finish")
                    time.sleep(0.01)
        finally:
            os.close(read_fd)
            os.close(write_fd)
            os.close(parent)


@native
@pytest.mark.parametrize("change", ["symlink", "mode", "hardlink", "missing"])
def test_fixed_configuration_loader_rejects_unsafe_control_file(
    api: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires actual Linux root")
    root = tmp_path / "etc"
    root.mkdir(mode=0o700)
    config = root / "python-project.json"
    payload = json.dumps(configuration_payload()).encode()
    if change != "missing":
        config.write_bytes(payload)
        config.chmod(0o600)
    if change == "symlink":
        config.rename(root / "target")
        config.symlink_to(root / "target")
    elif change == "mode":
        config.chmod(0o644)
    elif change == "hardlink":
        os.link(config, root / "alias")
    monkeypatch.setattr(api, "_CONFIG_PATH", config)
    with pytest.raises(api.PythonProjectBackendError):
        api.load_root_configuration()
    assert not (tmp_path / "stage").exists()


@native
def test_export_root_is_root_sticky_and_nonempty_export_is_never_silently_accepted(
    api: Any,
    tmp_path: Path,
) -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires actual Linux root")
    tmp_path.chmod(0o700)
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        export = api._make_export(parent, tmp_path)
        assert stat.S_IMODE(export.stat().st_mode) == 0o1777 and export.stat().st_uid == 0
        api._export_empty(parent, time.monotonic() + 2)
        (export / "unexpected").write_bytes(b"caller retains this")
        with pytest.raises(api.PythonProjectBackendError):
            api._export_empty(parent, time.monotonic() + 2)
        assert (export / "unexpected").read_bytes() == b"caller retains this"
    finally:
        os.close(parent)


@native
@pytest.mark.parametrize("change", [None, "symlink", "hardlink"])
def test_actual_sealed_source_measurement_rejects_aliases_without_executing_bytes(
    api: Any,
    tmp_path: Path,
    change: str | None,
) -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires actual Linux root")
    root = tmp_path / "source"
    root.mkdir(mode=0o700)
    path = root / "module.py"
    path.write_bytes(b"dummy bytes; never execute")
    path.chmod(0o444)
    if change == "symlink":
        (root / "alias").symlink_to(path)
    elif change == "hardlink":
        os.link(path, root / "alias")
    root.chmod(0o555)
    _, invocation, _, _ = fixture()
    if change:
        with pytest.raises((api.PythonProjectBackendError, RuntimeError)):
            api._source_files(root, invocation, time.monotonic() + 2)
    else:
        assert api._source_files(root, invocation, time.monotonic() + 2) == {
            "module.py": hashlib.sha256(b"f\0dummy bytes; never execute").hexdigest()
        }
