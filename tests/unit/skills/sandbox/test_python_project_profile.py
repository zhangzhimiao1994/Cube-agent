"""Read-only profile tests: no workspace, process, download or native availability claim."""

from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_hub.capabilities.python_project_inputs import PythonInterpreterIdentity
from agent_hub.skills.sandbox.project import ProjectInvocation


def invocation() -> ProjectInvocation:
    return ProjectInvocation(execution_id="profile_1", tenant_id=UUID(int=1), run_id=UUID(int=2),
        project_id="greenfield", session_id="one", sandbox_profile="workspace_write", timeout_seconds=1)


def test_separate_profile_module_exists() -> None:
    assert importlib.util.find_spec("agent_hub.skills.sandbox.python_project_profile") is not None


@pytest.fixture
def api() -> Any:
    return importlib.import_module("agent_hub.skills.sandbox.python_project_profile")


@pytest.fixture
def configured(api: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    backend = importlib.import_module("agent_hub.skills.sandbox.python_project_backend")
    catalog = importlib.import_module("agent_hub.skills.sandbox.python_input_supply_catalog")
    fetch = importlib.import_module("agent_hub.skills.sandbox.python_input_fetch")
    interpreter = PythonInterpreterIdentity("cpython", "3.12.321", "cp312", "linux_aarch64",
                                            "a" * 64, "b" * 64)
    # Deliberately not the deployed fixture's versions/hashes; all configured versions must survive.
    wheels = tuple(fetch.WheelDownload(f"example_pkg-{version}-py3-none-any.whl", "example-pkg",
        version, digest, size,
        f"https://files.pythonhosted.org/packages/ab/cd/example_pkg-{version}-py3-none-any.whl")
        for version, digest, size in (("9.2", "d" * 64, 321), ("8.1", "e" * 64, 654)))
    supply = catalog.RootSupplyConfig(Path("/var/lib/private-secret-config"), wheels,
                                     1024 * 1024, 1024, 120)
    root = SimpleNamespace(interpreter=interpreter, runtime_root=Path("/private/runtime-secret"))
    root.to_payload = lambda: {'interpreter': root.interpreter.to_payload(),
        'runtime_root': str(root.runtime_root)}
    reads: list[str] = []
    def read_root() -> Any:
        reads.append("root")
        return root
    def read_supply() -> Any:
        reads.append("supply")
        return supply
    monkeypatch.setattr(backend, "read_root_configuration", read_root)
    monkeypatch.setattr(catalog, "read_supply_configuration", read_supply)
    return SimpleNamespace(root=root, supply=supply, reads=reads, backend=backend, catalog=catalog)


def request(api: Any) -> Any:
    return api.PythonProjectProfileRequest(invocation=invocation())


def response(api: Any) -> Any:
    return api.PythonProjectProfileResponse.model_validate_json(
        api.dispatch_profile_request(request(api).model_dump_json().encode()), strict=True)


def test_empty_workspace_profile_is_configuration_only_and_returns_actual_catalog(
    api: Any, configured: Any,
) -> None:
    value = response(api)
    assert value.ok and value.version == 6 and value.action == "inspect_python_project_profile"
    value.require_for(request(api))
    profile = value.profile
    assert profile.status == "configured_not_probed"
    assert profile.interpreter.model_dump() == configured.root.interpreter.to_payload()
    assert [(wheel.name, wheel.version, wheel.sha256, wheel.size_bytes) for wheel in profile.catalog] == [
        ("example-pkg", "8.1", "e" * 64, 654), ("example-pkg", "9.2", "d" * 64, 321)]
    assert profile.catalog_sha256 == configured.supply.digest
    assert configured.reads == ["root", "supply"]
    public = value.model_dump_json()
    assert all(secret not in public for secret in (
        "/var/lib", "/private", "https:", "files.pythonhosted", "available", "credentials"))
    assert set(profile.catalog[0].model_dump()) == {"filename", "name", "version", "sha256", "size_bytes"}


def test_profile_exposes_only_digest_of_root_config_for_metadata_plan(api: Any, configured: Any) -> None:
    from agent_hub.skills.sandbox.python_metadata_plan import _digest

    profile = response(api).profile
    assert getattr(profile, 'root_config_sha256', None) == _digest(configured.root.to_payload())
    old = profile.root_config_sha256
    configured.root.runtime_root = Path('/different/private/runtime')
    assert response(api).profile.root_config_sha256 != old
    assert '/different' not in response(api).model_dump_json()


def test_scope_echoes_all_five_independent_request_identifiers(api: Any, configured: Any) -> None:
    value = response(api)
    assert value.scope.model_dump() == {name: getattr(invocation(), name) for name in (
        "tenant_id", "run_id", "project_id", "session_id", "execution_id")}
    assert "original_reference" not in request(api).model_dump_json()
    assert request(api).invocation.install_allowed is False


def test_discovery_does_not_construct_execution_create_directories_or_fetch(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    base = importlib.import_module("agent_hub.skills.sandbox.base")
    project = importlib.import_module("agent_hub.skills.sandbox.project")
    fetch = importlib.import_module("agent_hub.skills.sandbox.python_input_fetch")
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("configuration discovery crossed a mutating/execution boundary")
    monkeypatch.setattr(os, "mkdir", forbidden)
    monkeypatch.setattr(base, "run_subprocess_with_limits", forbidden)
    monkeypatch.setattr(project.ProjectExecutionBroker, "__init__", forbidden)
    monkeypatch.setattr(fetch, "fetch_wheel", forbidden)
    assert response(api).ok and configured.reads == ["root", "supply"]


def test_profile_tracks_changed_config_instead_of_fixture_hashes(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = response(api).profile
    configured.root.interpreter = replace(configured.root.interpreter, binary_sha256="f" * 64)
    changed = replace(configured.supply,
        catalog=tuple(replace(entry, sha256="9" * 64) for entry in configured.supply.catalog))
    monkeypatch.setattr(configured.catalog, "read_supply_configuration", lambda: changed)
    second = response(api).profile
    assert second.interpreter.binary_sha256 == "f" * 64 != first.interpreter.binary_sha256
    assert {entry.sha256 for entry in second.catalog} == {"9" * 64}
    assert second.catalog_sha256 == changed.digest != first.catalog_sha256
    assert second.status == "configured_not_probed"


@pytest.mark.parametrize("mutation", ["install", "original", "extra", "v5", "float_version", "bool_version", "action"])
def test_invalid_requests_never_read_privileged_configuration(api: Any, configured: Any, mutation: str) -> None:
    payload = request(api).model_dump(mode="json")
    if mutation == "install":
        payload["invocation"]["install_allowed"] = True
    elif mutation == "original":
        payload["invocation"]["original_reference"] = {"secret": "private"}
    elif mutation == "extra":
        payload["url"] = "https://private.invalid/secret"
    elif mutation == "v5":
        payload["version"] = 5
    elif mutation == "float_version":
        payload["version"] = 6.0
    elif mutation == "bool_version":
        payload["version"] = True
    else:
        payload["action"] = "run_project"
    with pytest.raises(ValueError, match="python_project_profile_request_invalid"):
        api.dispatch_profile_request(json.dumps(payload).encode())
    assert not configured.reads


@pytest.mark.parametrize("raw", [b"", b"[]", b"{bad", b"{\"version\":6,\"version\":6}",
                                  b'{"v":NaN}', b" " * 16385])
def test_malformed_duplicate_or_oversized_input_is_finite_and_read_only(
    api: Any, configured: Any, raw: bytes,
) -> None:
    with pytest.raises(ValueError, match="python_project_profile_request_invalid"):
        api.dispatch_profile_request(raw)
    assert not configured.reads


@pytest.mark.parametrize("code", ["input_supply_configuration_missing", "input_supply_configuration_untrusted"])
def test_optional_supply_missing_or_untrusted_has_precise_typed_failure(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch, code: Any,
) -> None:
    def missing() -> None:
        raise configured.catalog.InputSupplyError(code)
    monkeypatch.setattr(configured.catalog, "read_supply_configuration", missing)
    value = response(api)
    assert not value.ok and value.error == code and value.profile is None
    value.require_for(request(api))
    assert configured.reads == ["root"]


@pytest.mark.parametrize("code", ["python_project_runtime_missing", "python_project_runtime_untrusted"])
def test_root_failure_never_guesses_interpreter_or_reads_supply(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch, code: Any,
) -> None:
    def missing() -> None:
        raise configured.backend.PythonProjectBackendError(code)
    monkeypatch.setattr(configured.backend, "read_root_configuration", missing)
    value = response(api)
    assert not value.ok and value.error == code and value.profile is None and not configured.reads


def test_unexpected_config_exception_cannot_echo_paths_or_credentials(
    api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> None:
        raise OSError("/host/credential-secret token=private")
    monkeypatch.setattr(configured.backend, "read_root_configuration", fail)
    value = response(api)
    assert value.error == "python_project_profile_unavailable" and value.profile is None
    assert all(word not in value.model_dump_json() for word in ("/host", "credential", "token="))


@pytest.mark.parametrize("field", ["tenant_id", "run_id", "project_id", "session_id", "execution_id"])
def test_response_cannot_be_rebound_to_another_scope(api: Any, configured: Any, field: str) -> None:
    payload = response(api).model_dump(mode="json")
    payload["scope"][field] = str(UUID(int=99)) if field in {"tenant_id", "run_id"} else "different"
    value = api.PythonProjectProfileResponse.model_validate_json(json.dumps(payload), strict=True)
    with pytest.raises(ValueError, match="python_project_profile_response_invalid"):
        value.require_for(request(api))


@pytest.mark.parametrize("mutation", ["available", "url", "path", "no_profile", "mixed_error", "version"])
def test_response_rejects_false_availability_and_private_fields(api: Any, configured: Any, mutation: str) -> None:
    payload = response(api).model_dump(mode="json")
    if mutation == "available":
        payload["profile"]["status"] = "available"
    elif mutation == "url":
        payload["profile"]["catalog"][0]["url"] = "https://private"
    elif mutation == "path":
        payload["profile"]["interpreter"]["path"] = "/host/python"
    elif mutation == "no_profile":
        payload["profile"] = None
    elif mutation == "mixed_error":
        payload["error"] = "python_project_profile_unavailable"
    else:
        payload["version"] = 6.0
    with pytest.raises(ValidationError):
        api.PythonProjectProfileResponse.model_validate_json(json.dumps(payload), strict=True)


class Writer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False
        self.waited = False
        self.close_started = asyncio.Event()
        self.close_gate: asyncio.Event | None = None

    def write(self, value: bytes) -> None:
        self.data.extend(value)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.close_started.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.waited = True


@pytest.fixture
async def transport(api: Any, configured: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    raw = response(api).model_dump_json().encode()
    reader, writer = asyncio.StreamReader(), Writer()
    entered = asyncio.Event()
    calls: list[str] = []
    async def connect(path: str) -> tuple[Any, Writer]:
        calls.append(path)
        entered.set()
        return reader, writer
    monkeypatch.setattr(api.asyncio, "open_unix_connection", connect, raising=False)
    monkeypatch.setattr(api, "_systemd_supported", lambda: True)
    return SimpleNamespace(reader=reader, writer=writer, calls=calls, entered=entered, raw=raw)


def feed(item: Any, raw: bytes) -> None:
    item.reader.feed_data(len(raw).to_bytes(4, "big") + raw)
    item.reader.feed_eof()


async def test_client_uses_existing_frames_and_returns_strict_scoped_json(api: Any, transport: Any) -> None:
    feed(transport, transport.raw)
    client = api.PythonProjectProfileSandbox(socket_path=Path("/test-profile.sock"))
    value = await client.inspect(invocation())
    assert value.ok and value.profile.status == "configured_not_probed"
    value.require_for(request(api))
    assert transport.calls == ["/test-profile.sock"]
    written = transport.writer.data
    assert int.from_bytes(written[:4], "big") == len(written) - 4
    assert json.loads(written[4:])["action"] == "inspect_python_project_profile"
    assert transport.writer.closed and transport.writer.waited


async def test_client_preserves_typed_unavailable_instead_of_guessing_defaults(api: Any, transport: Any) -> None:
    payload = json.loads(transport.raw)
    payload.update(ok=False, error="input_supply_configuration_missing", profile=None)
    feed(transport, json.dumps(payload).encode())
    value = await api.PythonProjectProfileSandbox().inspect(invocation())
    assert not value.ok and value.error == "input_supply_configuration_missing" and value.profile is None
    assert transport.writer.closed and transport.writer.waited


@pytest.mark.parametrize("mutation", ["scope", "old_version", "duplicate", "too_large", "broken"])
async def test_client_rejects_invalid_or_oversized_response_and_closes_socket(
    api: Any, transport: Any, mutation: str,
) -> None:
    payload = json.loads(transport.raw)
    if mutation == "scope":
        payload["scope"]["execution_id"] = "other"
    elif mutation == "old_version":
        payload["version"] = 5
    if mutation == "too_large":
        transport.reader.feed_data((1024**2 + 1).to_bytes(4, "big"))
        transport.reader.feed_eof()
    else:
        raw = b"{" if mutation == "broken" else json.dumps(payload).encode()
        if mutation == "duplicate":
            raw = raw.replace(b'{"version": 6,', b'{"version": 6,"version":6,', 1)
        feed(transport, raw)
    with pytest.raises(ValueError, match="python_project_profile_response_invalid"):
        await api.PythonProjectProfileSandbox().inspect(invocation())
    assert transport.writer.closed and transport.writer.waited


async def test_mutated_invocation_is_rejected_before_connection(api: Any, transport: Any) -> None:
    item = invocation().model_copy(update={"project_id": "/private/unsafe"})
    with pytest.raises(ValueError, match="python_project_profile_request_invalid"):
        await api.PythonProjectProfileSandbox().inspect(item)
    assert not transport.calls


async def test_nonlinux_client_fails_before_transport(
    api: Any, transport: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "_systemd_supported", lambda: False)
    with pytest.raises(OSError, match="python_project_profile_unsupported"):
        await api.PythonProjectProfileSandbox().inspect(invocation())
    assert not transport.calls


async def test_substituted_transport_response_is_revalidated_and_scoped(
    api: Any, transport: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(transport.raw)
    payload["scope"]["tenant_id"] = str(UUID(int=99))
    bad = api.PythonProjectProfileResponse.model_validate_json(json.dumps(payload), strict=True)
    async def exchange(*args: Any, **kwargs: Any) -> Any:
        return bad
    monkeypatch.setattr(api, "request_python_project_profile_broker", exchange)
    with pytest.raises(ValueError, match="python_project_profile_response_invalid"):
        await api.PythonProjectProfileSandbox().inspect(invocation())
    assert not transport.calls


async def test_read_only_cancel_drains_socket_without_terminate_or_resend(
    api: Any, transport: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = importlib.import_module("agent_hub.skills.sandbox.project")
    async def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("read-only inspection must not create cancellation tombstones")
    monkeypatch.setattr(project, "request_project_broker", forbidden)
    task = asyncio.create_task(api.PythonProjectProfileSandbox().inspect(invocation()))
    try:
        await asyncio.wait_for(transport.entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.writer.closed and transport.writer.waited and len(transport.calls) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_repeated_cancel_waits_for_socket_finally_without_leaking_pending_task(
    api: Any, transport: Any,
) -> None:
    gate = transport.writer.close_gate = asyncio.Event()
    task = asyncio.create_task(api.PythonProjectProfileSandbox().inspect(invocation()))
    try:
        await asyncio.wait_for(transport.entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(transport.writer.close_started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and transport.writer.closed
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert transport.writer.waited and len(transport.calls) == 1
    finally:
        gate.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_read_only_timeout_drains_socket_without_unit_cancellation(
    api: Any, transport: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "_REQUEST_SECONDS", 0.01)
    with pytest.raises(TimeoutError):
        await api.PythonProjectProfileSandbox().inspect(invocation())
    assert transport.writer.closed and transport.writer.waited and len(transport.calls) == 1
