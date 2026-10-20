"""In-memory broker frames and cooperative tasks only; no sockets or project execution."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_hub.capabilities.python_project_inputs import PythonInterpreterIdentity
from agent_hub.capabilities.python_project_recipe import (
    PythonLockedRequirement,
    PythonProjectDeclaration,
)
from agent_hub.skills.sandbox import python_project_protocol as protocol
from agent_hub.skills.sandbox.broker import BrokerResponse
from agent_hub.skills.sandbox.project import (
    OriginalProjectReference,
    ProjectBrokerRequest,
    ProjectInvocation,
)
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete


def test_python_v4_client_is_available() -> None:
    assert importlib.util.find_spec("agent_hub.skills.sandbox.python_project_client") is not None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = importlib.import_module("agent_hub.skills.sandbox.python_project_client")
    monkeypatch.setattr(module, "_systemd_supported", lambda: True)
    return module


def invocation() -> protocol.PythonProjectInvocation:
    return protocol.PythonProjectInvocation(
        execution_id="python_check",
        tenant_id=UUID(int=1),
        run_id=UUID(int=2),
        project_id="demo",
        session_id="one",
        sandbox_profile="workspace_write",
        timeout_seconds=1,
        original_reference=OriginalProjectReference(
            contract_sha256="a" * 64, input_source_sha256="b" * 64, file_count=6
        ),
        source_kind="candidate",
        recipe_sha256="c" * 64,
        environment_input_sha256="d" * 64,
    )


def reference() -> protocol.PythonEnvironmentReference:
    inv = invocation()
    return protocol.PythonEnvironmentReference(
        scope=protocol.PythonProjectScope(
            tenant_id=inv.tenant_id,
            run_id=inv.run_id,
            project_id=inv.project_id,
            session_id=inv.session_id,
            original_reference=inv.original_reference,
        ),
        recipe_sha256=inv.recipe_sha256,
        environment_input_sha256=inv.environment_input_sha256,
        environment_sha256="e" * 64,
        security_manifest_sha256="f" * 64,
        origin_execution_id="previous_run",
    )


def request(action: str) -> Any:
    inv = invocation()
    if action == "run":
        return protocol.PythonProjectBrokerRequest(invocation=inv)
    if action == "inspect_environment":
        return protocol.PythonProjectInspectEnvironmentRequest(
            invocation=inv, expected_environment=reference()
        )
    probe = protocol.PythonProjectProbeInvocation.model_validate(
        {name: getattr(inv, name) for name in protocol.PythonProjectProbeInvocation.model_fields},
        strict=True,
    )
    return protocol.PythonProjectProbeRequest(invocation=probe)


def observation() -> dict[str, Any]:
    declaration = PythonProjectDeclaration(
        "demo",
        "1.0",
        "demo",
        ">=3.12,<3.13",
        "a" * 64,
        "b" * 64,
        tuple(
            PythonLockedRequirement(name, "1.0", ("a" * 64,)) for name in ("setuptools", "wheel")
        ),
        (),
        ("setuptools", "wheel"),
    )
    interpreter = PythonInterpreterIdentity(
        "cpython", "3.12.12", "cp312", "linux_x86_64", "c" * 64, "d" * 64
    )
    return {
        "scope": reference().scope.model_dump(mode="json"),
        "declaration": declaration.to_payload(),
        "interpreter": interpreter.to_payload(),
        "wheels": [
            {
                "filename": f"{name}-1.0-py3-none-any.whl",
                "name": name,
                "version": "1.0",
                "sha256": "a" * 64,
            }
            for name in ("setuptools", "wheel")
        ],
    }


def response(action: str) -> dict[str, Any]:
    req = request(action)
    payload: dict[str, Any] = {
        "version": 4,
        "action": req.action,
        "execution_id": req.invocation.execution_id,
        "ok": True,
    }
    if action == "run":
        payload.update(
            result={
                "exit_code": 0,
                "stdout": '{"schema_version":999,"passed":true}',
                "stderr": "",
                "timed_out": False,
            },
            environment_reference=reference().model_dump(mode="json"),
        )
    elif action == "inspect_environment":
        payload["environment_reference"] = reference().model_dump(mode="json")
    else:
        payload["observation"] = observation()
    return payload


class FrameWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False
        self.waited = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


def connect_frames(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> FrameWriter:
    writer = FrameWriter()
    reader = asyncio.StreamReader()
    reader.feed_data(len(payload).to_bytes(4) + payload)
    reader.feed_eof()

    async def connect(path: str) -> Any:
        assert path == "/fake/broker.sock"
        return reader, writer

    monkeypatch.setattr(asyncio, "open_unix_connection", connect, raising=False)
    return writer


@pytest.mark.parametrize("action", ["run", "probe", "inspect_environment"])
async def test_framed_v4_response_is_selected_and_bound_by_actual_request(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    req = request(action)
    writer = connect_frames(monkeypatch, json.dumps(response(action)).encode())
    reply = await client.request_python_project_broker(req, socket_path=Path("/fake/broker.sock"))
    assert reply.action == req.action and reply.execution_id == req.invocation.execution_id
    assert json.loads(writer.data[4:]) == json.loads(req.model_dump_json())
    assert int.from_bytes(writer.data[:4]) == len(writer.data) - 4
    assert writer.closed and writer.waited
    if action == "run":
        assert reply.result.stdout == '{"schema_version":999,"passed":true}'


@pytest.mark.parametrize(
    "change",
    [
        "malformed",
        "legacy",
        "version",
        "weak-version",
        "action",
        "execution",
        "scope",
        "recipe",
        "inputs",
        "environment",
        "security",
        "origin",
        "self-declared",
    ],
)
async def test_transport_rejects_invalid_or_context_swapped_inspection_reply(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    payload = response("inspect_environment")
    if change == "legacy":
        payload = {
            "ok": True,
            "result": {"exit_code": 0, "stdout": "{}", "stderr": "", "timed_out": False},
        }
    elif change in {"version", "weak-version"}:
        payload["version"] = 3 if change == "version" else True
    elif change == "action":
        payload = response("run")
    elif change == "execution":
        payload["execution_id"] = "other_check"
    elif change == "scope":
        payload["environment_reference"]["scope"]["tenant_id"] = str(UUID(int=99))
    elif change == "self-declared":
        payload = {"ok": True, "stdout": json.dumps(payload)}
    elif change in {"recipe", "inputs", "environment", "security", "origin"}:
        field = {
            "recipe": "recipe_sha256",
            "inputs": "environment_input_sha256",
            "environment": "environment_sha256",
            "security": "security_manifest_sha256",
            "origin": "origin_execution_id",
        }[change]
        payload["environment_reference"][field] = (
            "different_origin" if change == "origin" else "a" * 64
        )
    raw = b"not JSON" if change == "malformed" else json.dumps(payload).encode()
    writer = connect_frames(monkeypatch, raw)
    with pytest.raises(ValueError, match="^python_project_response_invalid$"):
        await client.request_python_project_broker(
            request("inspect_environment"), socket_path=Path("/fake/broker.sock")
        )
    assert writer.closed and writer.waited


@pytest.mark.parametrize("action", ["run", "probe", "inspect_environment"])
async def test_explicit_client_methods_keep_result_observation_and_reference_separate(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    writer = connect_frames(monkeypatch, json.dumps(response(action)).encode())
    sandbox = client.PythonProjectSystemdSandbox(socket_path=Path("/fake/broker.sock"))
    req = request(action)
    if action == "run":
        result = await sandbox.run(req.invocation)
        assert result.result.exit_code == 0 and result.environment_reference == reference()
    elif action == "probe":
        result = await sandbox.probe(req.invocation)
        assert result.scope == reference().scope and result.interpreter["abi"] == "cp312"
    else:
        result = await sandbox.inspect_environment(req.invocation, expected_environment=reference())
        assert result == reference()
    assert writer.closed and writer.waited


@pytest.mark.parametrize(
    "code", ["python_project_environment_changed", "python_project_cleanup_incomplete"]
)
async def test_negative_root_reply_is_never_success(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    payload = response("run")
    payload = {key: payload[key] for key in ("version", "action", "execution_id")}
    payload.update(ok=False, error=code)
    connect_frames(monkeypatch, json.dumps(payload).encode())
    error_type = ProcessCleanupIncomplete if code.endswith("cleanup_incomplete") else OSError
    with pytest.raises(error_type, match=f"^{code}$"):
        await client.PythonProjectSystemdSandbox(socket_path=Path("/fake/broker.sock")).run(
            invocation()
        )


@pytest.mark.parametrize("invalid", ["node", "constructed-python", "constructed-inspect"])
async def test_unvalidated_or_wrong_request_type_is_rejected_before_any_transport(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid request opened transport")

    monkeypatch.setattr(asyncio, "open_unix_connection", forbidden, raising=False)
    bad: Any = request("run")
    if invalid == "node":
        bad = ProjectBrokerRequest(
            invocation=ProjectInvocation.model_validate(
                {name: getattr(invocation(), name) for name in ProjectInvocation.model_fields},
                strict=True,
            )
        )
    elif invalid == "constructed-python":
        bad = bad.model_copy(update={"version": 3})
    else:
        bad = request("inspect_environment").model_copy(
            update={
                "expected_environment": reference().model_copy(update={"recipe_sha256": "b" * 64}),
            }
        )
    with pytest.raises(ValueError, match="^python_project_request_invalid$"):
        await client.request_python_project_broker(bad)


@pytest.mark.parametrize("action", ["run", "probe", "inspect_environment"])
@pytest.mark.parametrize("trigger", ["cancel", "timeout"])
async def test_interruption_terminates_original_id_and_drains_pending_even_on_repeated_cancel(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    trigger: str,
) -> None:
    started, terminating, release_terminate = asyncio.Event(), asyncio.Event(), asyncio.Event()
    draining, release_drain, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests: list[Any] = []
    pending: list[asyncio.Task[Any]] = []

    async def exchange(req: Any, **kwargs: Any) -> Any:
        current = asyncio.current_task()
        assert current is not None
        pending.append(current)
        started.set()
        try:
            await asyncio.Future()
        finally:
            draining.set()
            await release_drain.wait()
            drained.set()

    async def terminate(req: Any, **kwargs: Any) -> Any:
        requests.append(req)
        terminating.set()
        await release_terminate.wait()
        return BrokerResponse(ok=True)

    monkeypatch.setattr(client, "request_python_project_broker", exchange)
    monkeypatch.setattr(client, "request_project_broker", terminate)
    monkeypatch.setattr(client, "_REQUEST_GRACE_SECONDS", 0)
    sandbox = client.PythonProjectSystemdSandbox()
    req = request(action)
    kwargs = {"expected_environment": reference()} if action == "inspect_environment" else {}
    task = asyncio.create_task(getattr(sandbox, action)(req.invocation, **kwargs))
    try:
        await asyncio.wait_for(started.wait(), 1)
        if trigger == "cancel":
            task.cancel()
        await asyncio.wait_for(terminating.wait(), 2)
        if trigger == "cancel":
            task.cancel()
        assert not task.done()
        release_terminate.set()
        await asyncio.wait_for(draining.wait(), 1)
        assert not task.done() and not drained.is_set()
        if trigger == "cancel":
            task.cancel()
        release_drain.set()
        with pytest.raises(asyncio.CancelledError if trigger == "cancel" else TimeoutError):
            await task
        assert drained.is_set() and all(item.done() for item in pending)
        assert len(requests) == 1
        assert requests[0].version == 2 and requests[0].action == "terminate_project"
        assert requests[0].execution_id == req.invocation.execution_id
    finally:
        release_terminate.set()
        release_drain.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *pending, return_exceptions=True)


async def test_failed_termination_still_drains_request_and_reports_cleanup_failure(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, drained = asyncio.Event(), asyncio.Event()

    async def exchange(*args: Any, **kwargs: Any) -> Any:
        started.set()
        try:
            await asyncio.Future()
        finally:
            drained.set()

    async def terminate(*args: Any, **kwargs: Any) -> Any:
        return BrokerResponse(ok=False, error="project_termination_cleanup_incomplete")

    monkeypatch.setattr(client, "request_python_project_broker", exchange)
    monkeypatch.setattr(client, "request_project_broker", terminate)
    task = asyncio.create_task(client.PythonProjectSystemdSandbox().run(invocation()))
    await started.wait()
    task.cancel()
    with pytest.raises(ProcessCleanupIncomplete, match="^python_project_cleanup_incomplete$"):
        await task
    assert drained.is_set()


@pytest.mark.parametrize(
    "malformation", ["zero", "oversized", "truncated-header", "truncated-body"]
)
async def test_invalid_frame_is_bounded_and_writer_is_closed(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    malformation: str,
) -> None:
    writer = FrameWriter()
    reader = asyncio.StreamReader()
    if malformation == "zero":
        raw = b"\0\0\0\0"
    elif malformation == "oversized":
        raw = (22_000_001).to_bytes(4)
    elif malformation == "truncated-header":
        raw = b"\0\0"
    else:
        raw = (100).to_bytes(4) + b"{}"
    reader.feed_data(raw)
    reader.feed_eof()

    async def connect(*args: Any, **kwargs: Any) -> Any:
        return reader, writer

    monkeypatch.setattr(asyncio, "open_unix_connection", connect, raising=False)
    with pytest.raises(ValueError, match="^python_project_response_invalid$"):
        await client.request_python_project_broker(request("run"))
    assert writer.closed and writer.waited


async def test_late_success_after_cancellation_cannot_replace_cancellation(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, terminating = asyncio.Event(), asyncio.Event()
    release_reply, reply_ready, release_terminate = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def exchange(*args: Any, **kwargs: Any) -> Any:
        started.set()
        await release_reply.wait()
        reply_ready.set()
        return protocol.PythonProjectBrokerResponse.model_validate_json(
            json.dumps(response("run")), strict=True
        )

    async def terminate(*args: Any, **kwargs: Any) -> Any:
        terminating.set()
        await release_terminate.wait()
        return BrokerResponse(ok=True)

    monkeypatch.setattr(client, "request_python_project_broker", exchange)
    monkeypatch.setattr(client, "request_project_broker", terminate)
    task = asyncio.create_task(client.PythonProjectSystemdSandbox().run(invocation()))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.wait_for(terminating.wait(), 1)
        release_reply.set()
        await asyncio.wait_for(reply_ready.wait(), 1)
        assert not task.done()
        release_terminate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_reply.set()
        release_terminate.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("action", ["run", "probe", "inspect_environment"])
async def test_unsupported_platform_refuses_before_transport(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("unsupported platform attempted broker IO")

    monkeypatch.setattr(client, "_systemd_supported", lambda: False)
    monkeypatch.setattr(client, "request_python_project_broker", forbidden)
    req = request(action)
    kwargs = {"expected_environment": reference()} if action == "inspect_environment" else {}
    with pytest.raises(OSError, match="^python_project_systemd_linux_unsupported$"):
        await getattr(client.PythonProjectSystemdSandbox(), action)(req.invocation, **kwargs)


async def test_probe_scope_is_bound_even_when_observation_is_valid(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = response("probe")
    payload["observation"]["scope"]["session_id"] = "other_session"
    connect_frames(monkeypatch, json.dumps(payload).encode())
    with pytest.raises(ValueError, match="^python_project_response_invalid$"):
        await client.request_python_project_broker(
            request("probe"), socket_path=Path("/fake/broker.sock")
        )


@pytest.mark.parametrize(
    "reply",
    [
        {"ok": True, "error": "unexpected"},
        {"ok": True, "result": {"exit_code": 0, "stdout": "{}", "stderr": "", "timed_out": False}},
        {"ok": True, "action": "run_python_project", "execution_id": "other"},
    ],
)
async def test_termination_ack_cannot_carry_execution_or_unrelated_fields(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    reply: dict[str, Any],
) -> None:
    writer = connect_frames(monkeypatch, json.dumps(reply).encode())
    with pytest.raises(ProcessCleanupIncomplete, match="^python_project_cleanup_incomplete$"):
        await client.PythonProjectSystemdSandbox(socket_path=Path("/fake/broker.sock")).terminate(
            "python_check"
        )
    assert writer.closed and writer.waited


async def test_real_frame_functions_drain_both_writers_before_cancellation_returns(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent, closing, release_close = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class OperationWriter(FrameWriter):
        async def drain(self) -> None:
            sent.set()

        async def wait_closed(self) -> None:
            closing.set()
            await release_close.wait()
            self.waited = True

    operation_writer = OperationWriter()
    terminate_writer = FrameWriter()
    operation_reader, terminate_reader = asyncio.StreamReader(), asyncio.StreamReader()
    ack = b'{"ok":true}'
    terminate_reader.feed_data(len(ack).to_bytes(4) + ack)
    terminate_reader.feed_eof()
    connections = 0

    async def connect(path: str) -> Any:
        nonlocal connections
        assert path == "/fake/broker.sock"
        connections += 1
        assert connections <= 2
        if connections == 1:
            return operation_reader, operation_writer
        return terminate_reader, terminate_writer

    monkeypatch.setattr(asyncio, "open_unix_connection", connect, raising=False)
    task = asyncio.create_task(
        client.PythonProjectSystemdSandbox(
            socket_path=Path("/fake/broker.sock"),
        ).run(invocation())
    )
    try:
        await asyncio.wait_for(sent.wait(), 1)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 1)
        assert terminate_writer.closed and terminate_writer.waited
        assert operation_writer.closed and not operation_writer.waited and not task.done()
        task.cancel()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert operation_writer.waited and connections == 2
        outgoing = [json.loads(writer.data[4:]) for writer in (operation_writer, terminate_writer)]
        assert [item["action"] for item in outgoing] == ["run_python_project", "terminate_project"]
        assert outgoing[1]["execution_id"] == outgoing[0]["invocation"]["execution_id"]
    finally:
        release_close.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
