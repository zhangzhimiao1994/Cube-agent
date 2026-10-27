"""Internal release contracts; mocks are not native execution evidence."""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from agent_hub.skills.sandbox import python_project_protocol as protocol
from tests.unit.skills.sandbox.test_python_project_broker import api as api  # noqa: PLC0414
from tests.unit.skills.sandbox.test_python_project_broker import (
    configured as configured,  # noqa: PLC0414
)
from tests.unit.skills.sandbox.test_python_project_client import invocation, reference


def release() -> Any:
    return protocol.PythonProjectReleaseEnvironmentRequest(
        invocation=invocation(), expected_environment=reference()
    )


def test_release_is_strict_internal_v4_without_terminal_authority() -> None:
    request = release()
    assert request.action == "release_python_project_environment"
    assert request.version == 4
    for field in ("terminal", "completed", "terminal_run", "host_path"):
        with pytest.raises(ValidationError):
            type(request).model_validate(request.model_dump() | {field: True}, strict=True)
    with pytest.raises(ValidationError):
        type(request)(
            invocation=invocation().model_copy(update={"install_allowed": True}),
            expected_environment=reference(),
        )
    with pytest.raises(ValidationError):
        type(request)(
            invocation=invocation(),
            expected_environment=reference().model_copy(
                update={"scope": reference().scope.model_copy(update={"session_id": "foreign"})}
            ),
        )


def test_release_success_is_exclusive_and_exactly_bound() -> None:
    request = release()
    response = protocol.PythonProjectBrokerResponse(
        action=request.action,
        execution_id=invocation().execution_id,
        ok=True,
        released_environment=reference(),
        release_outcome="released",
    )
    response.require_for(request)
    for extra in (
        {"released_environment": None},
        {"environment_reference": reference()},
        {"error": "python_project_environment_changed"},
    ):
        with pytest.raises(ValidationError):
            type(response).model_validate(response.model_dump() | extra, strict=True)
    with pytest.raises(ValueError):
        response.model_copy(
            update={
                "released_environment": reference().model_copy(
                    update={"security_manifest_sha256": "0" * 64}
                )
            }
        ).require_for(request)


async def test_client_exposes_only_internal_exact_reference_release(monkeypatch: Any) -> None:
    from agent_hub.skills.sandbox import python_project_client as client

    seen = []

    async def exchange(self: Any, request: Any) -> Any:
        seen.append(request)
        return protocol.PythonProjectBrokerResponse(
            action=request.action,
            execution_id=request.invocation.execution_id,
            ok=True,
            released_environment=reference(),
            release_outcome="released",
        )

    monkeypatch.setattr(client.PythonProjectSystemdSandbox, "_exchange", exchange)
    result = await client.PythonProjectSystemdSandbox().release_completed(
        invocation(), expected_environment=reference()
    )
    assert result == reference()
    assert seen == [release()]


@pytest.mark.parametrize("quiet", [True, False])
def test_root_release_checks_both_origin_units_before_cache_without_project_execution(
    monkeypatch: Any,
    quiet: bool,
) -> None:
    from agent_hub.skills.sandbox import python_project_backend as backend
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

    calls = []

    async def admit(broker: Any, job: Any) -> None:
        calls.append(job.unit)
        if not quiet:
            raise ValueError("active unit")

    class Cache:
        def __init__(self, *args: Any) -> None:
            pass

        def release_completed(self, inv: Any, expected: Any, *, deadline: float) -> bool:
            assert inv == invocation() and expected == reference()
            assert deadline > time.monotonic()
            calls.append("release")
            return True

    monkeypatch.setattr(backend, "PythonProjectEnvironmentCache", Cache)
    monkeypatch.setattr(backend, "_admit_release_unit", admit)
    monkeypatch.setattr(backend, "_release_scope_guard", lambda *args: None)
    config: Any = SimpleNamespace(
        environment_root="unused", cache_policy="unused", stage_root=Path("unused")
    )
    if not quiet:
        with pytest.raises(ProcessCleanupIncomplete):
            backend._release(release(), BrokerPolicy(), config, time.monotonic() + 5,
                             stage=Path("unused/current"))
        assert "release" not in calls
    else:
        result = backend._release(release(), BrokerPolicy(), config, time.monotonic() + 5,
                                  stage=Path("unused/current"))
        assert result.released_environment == reference()
        assert calls == [
            "agent-hub-python-broker-previous_run.service",
            "agent-hub-project-previous_run.service",
            "release",
        ]


async def test_consumed_run_id_denied_fresh_release_id_succeeds_then_replay_denied(
    api: Any,
    configured: Any,
    monkeypatch: Any,
) -> None:
    from agent_hub.skills.sandbox.base import SkillResult
    from agent_hub.skills.sandbox.project import _Execution

    broker = configured.broker
    consumed = invocation()
    broker.executions[consumed.execution_id] = _Execution(
        protocol.base_python_invocation(consumed), None
    )
    old = release()
    denied = await api.handle_python_project(broker, old, peer_uid=broker.policy.allowed_uid)
    assert not denied.ok and denied.error == "python_project_request_invalid"
    assert "spawn" not in configured.events

    fresh = type(old)(
        invocation=consumed.model_copy(update={"execution_id": "python_release_fresh"}),
        expected_environment=reference(),
    )

    async def subprocess(*args: Any, **kwargs: Any) -> SkillResult:
        configured.events.append("release-helper")
        kwargs["process_started"](SimpleNamespace(returncode=0))
        return SkillResult(
            exit_code=0,
            stderr="",
            timed_out=False,
            stdout=protocol.PythonProjectBrokerResponse(
                action=fresh.action,
                execution_id=fresh.invocation.execution_id,
                ok=True,
                released_environment=reference(),
                release_outcome="released",
            ).model_dump_json(),
        )

    monkeypatch.setattr(api, "run_subprocess_with_limits", subprocess)
    result = await api.handle_python_project(broker, fresh, peer_uid=broker.policy.allowed_uid)
    assert result.ok and result.released_environment == reference()
    assert configured.events.count("release-helper") == 1
    replay = await api.handle_python_project(broker, fresh, peer_uid=broker.policy.allowed_uid)
    assert not replay.ok and replay.error == "python_project_request_invalid"
    assert configured.events.count("release-helper") == 1


def test_release_dispatch_holds_shared_admission_without_preparing_or_running_project(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    from agent_hub.skills.sandbox import python_project_backend as backend
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from tests.unit.skills.sandbox.test_python_project_backend import configuration_payload

    locked = []

    @contextmanager
    def roots(*args: Any) -> Any:
        yield SimpleNamespace(stage_root_fd=42)

    @contextmanager
    def admission(parent: Any, deadline: Any) -> Any:
        locked.append(True)
        try:
            yield
        finally:
            locked.pop()

    def release_only(request: Any, *args: Any, **kwargs: Any) -> Any:
        assert locked == [True]
        assert kwargs["stage"] == tmp_path
        return protocol.PythonProjectBrokerResponse(
            action=request.action,
            execution_id=request.invocation.execution_id,
            ok=True,
            released_environment=reference(),
            release_outcome="released",
        )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("release must not prepare/run/probe/inspect project or reserve disk")

    monkeypatch.setattr(backend, "_native", lambda: None)
    monkeypatch.setattr(backend, "_roots", roots)
    monkeypatch.setattr(backend, "_admission", admission)
    monkeypatch.setattr(backend, "_release", release_only)
    for name in ("_prepared", "_run", "_probe", "_inspect", "_disk_budget"):
        monkeypatch.setattr(backend, name, forbidden)
    response = backend.execute_python_project_request(
        release(),
        policy=BrokerPolicy(),
        stage=tmp_path,
        configuration=backend.RootConfig.from_payload(configuration_payload()),
    )
    assert response.ok and locked == []


def test_retained_origin_stage_denies_even_when_units_appear_quiet(monkeypatch: Any) -> None:
    from agent_hub.skills.sandbox import python_project_backend as backend
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.project import ProjectExecutionBroker
    from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

    async def quiet(*args: Any) -> None:
        pass

    monkeypatch.setattr(backend, "_admit_release_unit", quiet)
    monkeypatch.setattr(ProjectExecutionBroker, "_retained", lambda self, key: True)
    with pytest.raises(ProcessCleanupIncomplete):
        backend._release(
            release(),
            BrokerPolicy(),
            cast(Any, SimpleNamespace(stage_root="unused")),
            time.monotonic() + 5,
        )


def test_root_absent_cache_is_typed_retry_success_after_lost_reply(monkeypatch: Any) -> None:
    from agent_hub.skills.sandbox import python_project_backend as backend
    from agent_hub.skills.sandbox.broker import BrokerPolicy
    from agent_hub.skills.sandbox.project import ProjectExecutionBroker

    async def quiet(*args: Any) -> None:
        pass

    monkeypatch.setattr(backend, "_admit_release_unit", quiet)
    monkeypatch.setattr(ProjectExecutionBroker, "_retained", lambda self, key: False)
    monkeypatch.setattr(backend, "_release_scope_guard", lambda *args: None)
    monkeypatch.setattr(
        backend,
        "PythonProjectEnvironmentCache",
        lambda *args: SimpleNamespace(
            release_completed=lambda *args, **kwargs: False,
        ),
    )
    config: Any = SimpleNamespace(
        stage_root=Path("unused"), environment_root=Path("unused"), cache_policy=None
    )
    response = backend._release(release(), BrokerPolicy(), config, time.monotonic() + 5,
                                stage=Path("unused/current"))
    assert response.ok and response.released_environment == reference()
    assert response.release_outcome == "absent"
