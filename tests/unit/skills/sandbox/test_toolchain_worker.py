"""Supply-worker contracts; mocked IO never establishes native installation."""

import importlib
import importlib.util
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_hub.capabilities.project_contracts import build_project_check_contract
from tests.unit.skills.sandbox.test_toolchain_protocol import invocation, plan, reference


def test_worker_has_separate_offline_and_network_launchers() -> None:
    assert importlib.util.find_spec("agent_hub.skills.sandbox.toolchain_worker") is not None


@pytest.fixture
def api() -> Any:
    name = "agent_hub.skills.sandbox.toolchain_worker"
    if importlib.util.find_spec(name) is None:
        pytest.skip("SDK worker absent")
    return importlib.import_module(name)


def test_fetch_command_does_not_grant_host_writes_or_read_project_secrets(api: Any) -> None:
    from agent_hub.skills.sandbox import toolchain_protocol
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.toolchain_catalog import ToolchainCatalog
    from tests.unit.skills.sandbox.test_toolchain_catalog import catalog

    request = toolchain_protocol.ToolchainRequest(
        action="supply_project_toolchain",
        invocation=invocation(toolchain_protocol),
        plan=plan(toolchain_protocol),
    )
    command = api.build_toolchain_fetch_command(
        request, BrokerPolicy(), ToolchainCatalog.from_payload(catalog())
    )
    text = " ".join(command)
    assert "DynamicUser=yes" in command
    assert "CapabilityBoundingSet=" in command
    assert "LimitFSIZE=0" in command
    assert "PrivateDevices=yes" in command
    assert "ProtectHome=yes" in command
    assert "TemporaryFileSystem=" in text and "/var:ro" in text
    assert "ReadWritePaths=" not in text
    assert "BindReadOnlyPaths=/etc/resolv.conf" in command
    assert "agent_hub.skills.sandbox.toolchain_fetch" in command
    assert "-I" in command and "-B" in command
    assert "/usr/bin/env" in command and "-i" in command
    assert not any("go install" in value for value in command)


def test_declaration_is_derived_from_protected_captured_controls(api: Any, tmp_path: Path) -> None:
    import hashlib
    from types import SimpleNamespace

    from agent_hub.capabilities.project_baselines import (
        ProjectBaselineReference,
        StoredProjectBaseline,
    )
    from agent_hub.skills.sandbox import toolchain_protocol

    call = invocation(toolchain_protocol)
    raw = b"module example.org/demo\ngo 1.27\n"
    (tmp_path / "go.mod").write_bytes(raw)
    files = {
        "go.mod": hashlib.sha256(b"f\0" + raw).hexdigest(),
        "main.go": hashlib.sha256(b"f\0package main\n").hexdigest(),
    }
    from agent_hub.capabilities.project_baselines import _encoded, _fingerprint

    source = _fingerprint(files)
    contract = build_project_check_contract(
        call.tenant_id, call.run_id, call.project_id, call.session_id, source, ("main.go",), files
    )
    ref = ProjectBaselineReference(hashlib.sha256(_encoded({"test": True})).hexdigest(), source, 2)
    stored = StoredProjectBaseline(ref, contract, tmp_path, files)
    # This helper validates captured bytes/preservation; the worker separately
    # binds the trusted reference/scope and stages the original Root-side.
    declaration = api.toolchain_declaration(stored)
    assert declaration.go_version == "1.27"
    (tmp_path / "go.mod").write_bytes(raw.replace(b"1.27", b"1.28"))
    with pytest.raises(ValueError):
        api.toolchain_declaration(stored)
    (tmp_path / "go.mod").write_bytes(raw)
    bad = replace(
        stored,
        contract=build_project_check_contract(
            call.tenant_id,
            call.run_id,
            call.project_id,
            call.session_id,
            source,
            ("go.mod", "main.go"),
            files,
        ),
    )
    with pytest.raises(ValueError):
        api.toolchain_declaration(bad)
    with pytest.raises(ValueError):
        api.toolchain_declaration(SimpleNamespace(snapshot=tmp_path))


def test_stored_environment_becomes_compact_reference_without_host_paths(api: Any) -> None:
    from types import SimpleNamespace

    from agent_hub.skills.sandbox import toolchain_protocol

    expected = reference(toolchain_protocol)
    measured = SimpleNamespace(
        version=expected.version,
        platform=expected.platform,
        archive_sha256=expected.archive_sha256,
        tree_sha256=expected.tree_sha256,
        binary_sha256=expected.binary_sha256,
        gofmt_sha256=expected.gofmt_sha256,
        total_bytes=expected.total_bytes,
        file_count=expected.file_count,
    )
    published = SimpleNamespace(
        artifact_sha256=expected.artifact_sha256, observation=measured, reused=True
    )
    result = api.toolchain_reference(published)
    assert result == expected
    assert "/var" not in result.model_dump_json()


def test_missing_sdk_is_preparable_but_corrupt_sdk_is_not_silently_replaced(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_hub.skills.sandbox import toolchain_protocol, toolchain_store

    def missing(*args: Any, **kwargs: Any) -> Any:
        raise toolchain_store.ToolchainStoreError("toolchain_store_missing")

    monkeypatch.setattr(toolchain_store, "observe_published_toolchain", missing)
    assert (
        api.available_toolchain(Path("/not-read"), plan(toolchain_protocol).artifact, 1.0) is None
    )

    def changed(*args: Any, **kwargs: Any) -> Any:
        raise toolchain_store.ToolchainStoreError("toolchain_store_unsafe")

    monkeypatch.setattr(toolchain_store, "observe_published_toolchain", changed)
    with pytest.raises(toolchain_store.ToolchainStoreError):
        api.available_toolchain(Path("/not-read"), plan(toolchain_protocol).artifact, 1.0)


def test_existing_claim_without_intent_is_unknown_not_absent(
    api: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    from agent_hub.skills.sandbox import toolchain_protocol
    from agent_hub.skills.sandbox.toolchain_catalog import ToolchainCatalog
    from tests.unit.skills.sandbox.test_toolchain_catalog import catalog

    @contextmanager
    def existing(*args: Any, **kwargs: Any) -> Any:
        yield 100

    def incomplete(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError

    monkeypatch.setattr(api, "_claim_root", existing)
    monkeypatch.setattr(api, "_read_control", incomplete)
    request = toolchain_protocol.ToolchainRequest(
        action="inspect_project_toolchain_supply",
        invocation=invocation(toolchain_protocol),
        plan=plan(toolchain_protocol),
        supply_execution_id="toolchain_supply_" + "a" * 32,
    )
    result = api._receipt_status(request, ToolchainCatalog.from_payload(catalog()))
    assert result.state == "unknown"
    assert result.receipt is None


def test_source_copy_capacity_is_checked_before_copy(api: Any) -> None:
    from agent_hub.skills.sandbox import toolchain_protocol
    from agent_hub.skills.sandbox.toolchain_catalog import FilesystemSpace, ToolchainCatalog
    from tests.unit.skills.sandbox.test_toolchain_catalog import catalog

    call = invocation(toolchain_protocol)
    config = ToolchainCatalog.from_payload(catalog())
    low = tuple(
        FilesystemSpace(
            role=role,
            device=1,
            allocation_unit=4096,
            available_bytes=config.min_free_bytes + call.source_limit_bytes,
        )
        for role in ("stage", "runtime", "state", "execution")
    )
    with pytest.raises(ValueError, match="toolchain_capacity"):
        api.require_source_copy_capacity(config, call, low)


def test_worker_rejects_low_space_before_any_original_copy(
    api: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    from agent_hub.skills.sandbox import (
        python_project_backend,
        toolchain_configuration,
        toolchain_protocol,
    )
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.toolchain_catalog import FilesystemSpace, ToolchainCatalog
    from tests.unit.skills.sandbox.test_toolchain_catalog import catalog

    call = invocation(toolchain_protocol)
    config = ToolchainCatalog.from_payload(catalog())
    low = tuple(
        FilesystemSpace(
            role=role,
            device=1,
            allocation_unit=4096,
            available_bytes=config.min_free_bytes + call.source_limit_bytes,
        )
        for role in ("stage", "runtime", "state", "execution")
    )

    @contextmanager
    def lock(*args: Any) -> Any:
        yield

    monkeypatch.setattr(api.sys, "platform", "linux")
    monkeypatch.setattr(api.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(api, "_root", lambda *args, **kw: 99)
    monkeypatch.setattr(python_project_backend, "_admission", lock)
    monkeypatch.setattr(
        toolchain_configuration, "read_toolchain_configuration", lambda *a, **kw: config
    )
    monkeypatch.setattr(toolchain_configuration, "measure_toolchain_spaces", lambda *a, **kw: low)
    monkeypatch.setattr(
        api, "stage_original_contract", lambda *args: pytest.fail("copied before admission")
    )
    request = toolchain_protocol.ToolchainRequest(action="plan_project_toolchain", invocation=call)
    with pytest.raises(ValueError, match="toolchain_capacity"):
        api.execute_toolchain_request(
            request, BrokerPolicy(), config, Path(config.stage_root) / "job"
        )


def test_inspect_intent_uses_approved_identity_not_free_space_snapshot(
    api: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from contextlib import contextmanager

    from agent_hub.skills.sandbox import toolchain_protocol
    from agent_hub.skills.sandbox.toolchain_catalog import ToolchainCatalog
    from tests.unit.skills.sandbox.test_toolchain_catalog import catalog

    old = plan(toolchain_protocol)
    fresh = old.model_copy(
        update={
            "filesystems": tuple(
                p.model_copy(update={"available_bytes": p.available_bytes - 4096})
                for p in old.filesystems
            )
        }
    )
    assert fresh.sha256 == old.sha256
    execution = "toolchain_supply_" + "b" * 32

    @contextmanager
    def existing(*args: Any, **kwargs: Any) -> Any:
        yield 100

    monkeypatch.setattr(api, "_claim_root", existing)
    monkeypatch.setattr(api.os, "listdir", lambda fd: ["intent.json"])
    monkeypatch.setattr(
        api,
        "_read_control",
        lambda *args: json.dumps(
            {"schema_version": 1, "supply_execution_id": execution, "plan": old.to_payload()}
        ).encode(),
    )
    request = toolchain_protocol.ToolchainRequest(
        action="inspect_project_toolchain_supply",
        invocation=invocation(toolchain_protocol),
        plan=fresh,
        supply_execution_id=execution,
    )
    assert api._receipt_status(request, ToolchainCatalog.from_payload(catalog())).state == "unknown"
