"""Root product ordering with native IO modeled, never project acceptance."""

from __future__ import annotations

import importlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_hub.capability_installer.python_attestation import ScanLimits
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.python_project_process import PythonPhaseProcessResult
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectBrokerRequest
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete
from tests.unit.skills.sandbox import test_python_project_backend as helpers


@pytest.mark.parametrize("reused", [False, True])
@pytest.mark.parametrize("failed", [None, "install", "build", "test"])
@pytest.mark.parametrize("uncertain", [False, True])
def test_root_retains_only_after_quiescence_and_final_rechecks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reused: bool,
    failed: str | None,
    uncertain: bool,
) -> None:
    backend = importlib.import_module("agent_hub.skills.sandbox.python_project_backend")
    recipe, invocation, _, stage, events = helpers.stubbed_context(backend, monkeypatch, tmp_path)
    reference = helpers.expected_reference(invocation) if reused else None
    report = helpers.report(invocation, recipe, failed=failed, reused=reused)

    class Lease:
        path = Path("/not-read")
        limits = ScanLimits(min(
            backend.RootConfig.from_payload(helpers.configuration_payload()).max_environment_bytes,
            invocation.workspace_limit_bytes,
        ))

        def current(self, **kwargs: Any) -> Any:
            return reference

        def publish(self, *args: Any, **kwargs: Any) -> Any:
            assert kwargs["execution"].quiet is True and "quiet" in events
            events.append("environment")
            return helpers.expected_reference(invocation)

        def record_install_failure(self, *args: Any, **kwargs: Any) -> None:
            pass

    class Cache:
        @contextmanager
        def reserve(self, *args: Any, **kwargs: Any) -> Any:
            yield Lease()

    async def quiet(_broker: object, _job: object) -> None:
        events.append("quiet")
        if uncertain:
            raise ProcessCleanupIncomplete("python_project_cleanup_incomplete")

    def retain(*args: Any, **kwargs: Any) -> None:
        assert args[:3] == (stage, invocation, report)
        assert events[-1] == "final-inputs" and "quiet" in events
        assert kwargs["service_uid"] == BrokerPolicy().allowed_uid
        events.append("product")

    measured = backend._measured_inputs

    def inputs(*args: Any) -> Any:
        value = measured(*args)
        events.append("final-inputs")
        return value

    monkeypatch.setattr(backend, "PythonProjectEnvironmentCache", lambda *args: Cache())
    monkeypatch.setattr(
        backend, "_source_snapshot", lambda *args: recipe.original_reference.input_source_sha256
    )
    monkeypatch.setattr(backend, "_recheck_source", lambda *args: None)
    monkeypatch.setattr(backend, "_write_plan", lambda *args: stage / "plan.json")
    monkeypatch.setattr(backend, "_make_export", lambda *args: stage / "export")
    monkeypatch.setattr(
        backend, "_publish_export", lambda lease, *args, **kwargs: lease.publish(*args, **kwargs)
    )
    monkeypatch.setattr(backend, "_export_empty", lambda *args: None)
    monkeypatch.setattr(backend.ProjectExecutionBroker, "_kill_and_confirm", quiet)
    monkeypatch.setattr(backend, "_measured_inputs", inputs)
    monkeypatch.setattr(backend, "retain_python_product", retain, raising=False)
    monkeypatch.setattr(
        backend,
        "run_python_phase",
        lambda *args, **kwargs: PythonPhaseProcessResult(
            0, False, json.dumps(report), "", False, False, 1000, 0
        ),
    )
    request = PythonProjectBrokerRequest(invocation=invocation)
    if uncertain:
        with pytest.raises(ProcessCleanupIncomplete):
            backend.execute_python_project_request(
                request,
                policy=BrokerPolicy(),
                configuration=backend.RootConfig.from_payload(helpers.configuration_payload()),
                stage=stage,
            )
    else:
        result = backend.execute_python_project_request(
            request,
            policy=BrokerPolicy(),
            configuration=backend.RootConfig.from_payload(helpers.configuration_payload()),
            stage=stage,
        )
        assert result.ok and result.product is None
    assert events.count("product") == (1 if failed is None and not uncertain else 0)


@pytest.mark.parametrize("shortfall", [0, 1])
def test_stage_disk_admission_accounts_one_bounded_product_copy(
    monkeypatch: pytest.MonkeyPatch,
    shortfall: int,
) -> None:
    backend = importlib.import_module("agent_hub.skills.sandbox.python_project_backend")
    configured = backend.RootConfig.from_payload(helpers.configuration_payload())
    minimum = (
        backend._STAGE_CONTROL_ALLOWANCE
        + 123
        + 456
        + 789
        + configured.cache_policy.disk_reserve_bytes
    )

    def disk(fd: int) -> Any:
        return SimpleNamespace(f_bavail=minimum - shortfall if fd == 1 else 2**60, f_frsize=1)

    monkeypatch.setattr(
        backend, "os", SimpleNamespace(fstatvfs=disk, fstat=lambda fd: SimpleNamespace(st_dev=fd))
    )
    roots = SimpleNamespace(stage_fd=1, cache_fd=2)
    if shortfall:
        with pytest.raises(backend.PythonProjectBackendError):
            backend._disk_budget(configured, roots, 123, True, source_bytes=456, product_bytes=789)
    else:
        backend._disk_budget(configured, roots, 123, True, source_bytes=456, product_bytes=789)
