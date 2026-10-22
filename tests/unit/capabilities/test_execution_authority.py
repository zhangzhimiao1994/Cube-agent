"""Internal execution binding contracts; no service integration or database access."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.runs.project_verification_receipts import VerificationAuthority
from agent_hub.runs.repository import RunRecord
from agent_hub.runtime.contracts import TaskContext


def api() -> Any:
    name = "agent_hub.capabilities.execution_authority"
    assert importlib.util.find_spec(name) is not None, "internal execution binding is missing"
    return importlib.import_module(name)


@pytest.fixture
def context() -> TaskContext:
    return TaskContext(
        tenant_id=uuid4(), run_id=uuid4(), actor_id=uuid4(), actor_role=Role.OPERATOR,
        execution_token=uuid4(), mode=TaskMode.DIRECT, request="Verify the current project",
        routing_decision={
            "project_id": "project-a", "workspace_session_id": "session-a",
            "sandbox_profile": "workspace_write", "execution_backend": "systemd",
            "requested_permissions": ("workspace.read", "workspace.write", "command.run"),
            "runtime_timeout_seconds": 300, "generated_project_contract_version": 1,
            "generated_project_preparation": {"schema_version": 1, "phase": "capturing"},
        },
    )


def record(context: TaskContext) -> RunRecord:
    return RunRecord(
        id=context.run_id, tenant_id=context.tenant_id, actor_id=context.actor_id,
        actor_role=context.actor_role, request=context.request, mode=context.mode,
        status=RunStatus.RUNNING, version=4, created_at=datetime.now(UTC),
        routing_decision=cast(dict[str, object], deepcopy(context.to_payload()["routing_decision"])),
        worker_id="different-db-worker", worker_lease_token=uuid4(),
        worker_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


def identity(context: TaskContext) -> dict[str, Any]:
    return {"tenant_id": context.tenant_id, "run_id": context.run_id,
            "user_id": context.actor_id, "role": context.actor_role}


def get(context: TaskContext, **kwargs: Any) -> VerificationAuthority:
    return cast(VerificationAuthority, api().get_verification_authority(**{**identity(context), **kwargs}))


def test_missing_binding_never_falls_back_to_current_record(context: TaskContext) -> None:
    with pytest.raises(api().ExecutionAuthorityError):
        get(context, current_record=record(context))


@pytest.mark.parametrize("initial,current_mode", [
    (None, "default"), ("default", "auto_review"),
    ("auto_review", "default"), ("default", None),
])
def test_permission_mode_change_cannot_refresh_bound_worker_policy(
    context: TaskContext, initial: str | None, current_mode: str | None,
) -> None:
    route = dict(context.routing_decision)
    if initial is not None:
        route["permission_mode"] = initial
    bound = context.model_copy(update={"routing_decision": route})
    current = record(bound)
    assert current.routing_decision is not None
    if current_mode is None:
        current.routing_decision.pop("permission_mode", None)
    else:
        current.routing_decision["permission_mode"] = current_mode
    with (
        api().bind_execution_authority(bound, worker_id="trusted-worker"),
        pytest.raises(api().ExecutionAuthorityError),
    ):
        get(bound, current_record=current)


@pytest.mark.parametrize("mode", [None, "default", "auto_review"])
def test_unchanged_verifying_permission_mode_preserves_worker_binding(
    context: TaskContext, mode: str | None,
) -> None:
    route = dict(context.routing_decision)
    if mode is not None:
        route["permission_mode"] = mode
    bound = context.model_copy(update={"routing_decision": route})
    with api().bind_execution_authority(bound, worker_id="trusted-worker"):
        assert get(bound, current_record=record(bound)) == get(bound)


def test_binding_builds_authority_from_worker_context_and_resets(context: TaskContext) -> None:
    with api().bind_execution_authority(context, worker_id="trusted-worker", actor="engineer"):
        authority = get(context)
        assert authority == VerificationAuthority.from_context(
            context, worker_id="trusted-worker", actor="engineer",
        )
        assert authority.execution_token == context.execution_token
        assert authority.actor == "engineer"
    with pytest.raises(api().ExecutionAuthorityError):
        get(context)


@pytest.mark.parametrize("field", ["tenant_id", "run_id", "user_id", "role"])
def test_getter_rejects_mismatched_identity(context: TaskContext, field: str) -> None:
    with (
        api().bind_execution_authority(context, worker_id="trusted-worker"),
        pytest.raises(api().ExecutionAuthorityError),
    ):
        get(context, **{field: Role.ADMIN if field == "role" else uuid4()})


@pytest.mark.parametrize("field", ["tenant_id", "run_id", "user_id", "role"])
def test_getter_does_not_coerce_string_identity(context: TaskContext, field: str) -> None:
    values = identity(context)
    with (
        api().bind_execution_authority(context, worker_id="trusted-worker"),
        pytest.raises(api().ExecutionAuthorityError),
    ):
        get(context, **{field: str(values[field])})


def test_nested_bindings_restore_outer_execution_token(context: TaskContext) -> None:
    inner = context.model_copy(update={"execution_token": uuid4()})
    with api().bind_execution_authority(context, worker_id="outer-worker"):
        outer = get(context)
        with api().bind_execution_authority(inner, worker_id="inner-worker", actor="engineer"):
            nested = get(inner)
            assert nested.execution_token == inner.execution_token
            assert nested.execution_token != outer.execution_token
            assert nested.worker_id == "inner-worker" and nested.actor == "engineer"
        assert get(context) == outer
    with pytest.raises(api().ExecutionAuthorityError):
        get(context)


def test_exception_resets_inner_and_outer_bindings(context: TaskContext) -> None:
    with (
        pytest.raises(LookupError),
        api().bind_execution_authority(context, worker_id="outer-worker"),
    ):
        with pytest.raises(ValueError), api().bind_execution_authority(
            context.model_copy(update={"execution_token": uuid4()}), worker_id="inner-worker",
        ):
            raise ValueError("inner failure")
        assert get(context).worker_id == "outer-worker"
        raise LookupError("outer failure")
    with pytest.raises(api().ExecutionAuthorityError):
        get(context)


async def test_async_child_inherits_but_cannot_overwrite_parent_binding(context: TaskContext) -> None:
    async def child() -> None:
        assert get(context).worker_id == "parent-worker"
        inner = context.model_copy(update={"execution_token": uuid4()})
        with api().bind_execution_authority(inner, worker_id="child-worker"):
            await asyncio.sleep(0)
            assert get(inner).execution_token == inner.execution_token
        assert get(context).execution_token == context.execution_token

    with api().bind_execution_authority(context, worker_id="parent-worker"):
        await asyncio.create_task(child())
        assert get(context).worker_id == "parent-worker"
    with pytest.raises(api().ExecutionAuthorityError):
        get(context)


async def test_concurrent_tasks_keep_independent_execution_tokens(context: TaskContext) -> None:
    second = context.model_copy(update={"execution_token": uuid4()})

    async def worker(current: TaskContext, worker_id: str) -> VerificationAuthority:
        with api().bind_execution_authority(current, worker_id=worker_id):
            await asyncio.sleep(0)
            return get(current)

    first_authority, second_authority = await asyncio.gather(
        worker(context, "first-worker"), worker(second, "second-worker"),
    )
    assert first_authority.execution_token == context.execution_token
    assert second_authority.execution_token == second.execution_token
    assert first_authority.execution_token != second_authority.execution_token
    with pytest.raises(api().ExecutionAuthorityError):
        get(context)


async def test_escaped_child_cannot_use_binding_after_parent_scope_exits(context: TaskContext) -> None:
    release = asyncio.Event()

    async def child() -> None:
        await release.wait()
        with pytest.raises(api().ExecutionAuthorityError):
            get(context)

    with api().bind_execution_authority(context, worker_id="parent-worker"):
        task = asyncio.create_task(child())
    release.set()
    await task


async def test_cancellation_resets_binding(context: TaskContext) -> None:
    with (
        pytest.raises(asyncio.CancelledError),
        api().bind_execution_authority(context, worker_id="trusted-worker"),
    ):
        raise asyncio.CancelledError
    with pytest.raises(api().ExecutionAuthorityError):
        get(context)


@pytest.mark.parametrize("field,value", [
    ("execution_token", None), ("actor_id", None), ("actor_role", None),
    ("actor_role", Role.VIEWER),
])
def test_invalid_inner_context_does_not_replace_outer_binding(
    context: TaskContext, field: str, value: Any,
) -> None:
    with api().bind_execution_authority(context, worker_id="trusted-worker"):
        original = get(context)
        with pytest.raises(api().ExecutionAuthorityError), api().bind_execution_authority(
            context.model_copy(update={field: value}), worker_id="invalid-worker",
        ):
            pytest.fail("invalid binding entered")
        assert get(context) == original


def test_binding_snapshots_mutable_input_context(context: TaskContext) -> None:
    with api().bind_execution_authority(context, worker_id="trusted-worker"):
        original = get(context)
        cast(dict[str, Any], context.routing_decision)["project_id"] = "project-mutated"
        object.__setattr__(context, "execution_token", uuid4())
        assert get(context) == original


def test_current_preparation_refresh_keeps_bound_execution_identity(context: TaskContext) -> None:
    current = record(context)
    assert current.routing_decision is not None
    current.routing_decision["generated_project_preparation"] = {
        "schema_version": 1, "phase": "python_ready",
        "python_recipe": {"schema_version": 1, "recipe_sha256": "a" * 64,
                          "environment_input_sha256": "b" * 64},
    }
    current.routing_decision["approval_id"] = "approval_resolved"
    current.routing_decision["worker_id"] = "route-worker-must-not-be-used"
    current.routing_decision["execution_token"] = str(uuid4())
    with api().bind_execution_authority(context, worker_id="bound-worker", actor="engineer"):
        original = get(context)
        refreshed = get(context, current_record=current)
        assert refreshed.scope_sha256 != original.scope_sha256
        assert refreshed.execution_token == context.execution_token != current.worker_lease_token
        assert refreshed.worker_id == "bound-worker" and refreshed.actor == "engineer"
        assert refreshed.request_sha256 == hashlib.sha256(context.request.encode()).hexdigest()
        assert refreshed.mode is context.mode
        assert get(context) == original


@pytest.mark.parametrize("field,value", [
    ("tenant_id", uuid4()), ("id", uuid4()), ("actor_id", uuid4()),
    ("actor_role", Role.ADMIN), ("actor_role", "operator"),
    ("request", "a different request"), ("mode", TaskMode.DISPATCH),
    ("status", RunStatus.WAITING_APPROVAL),
])
def test_current_record_cannot_change_bound_stable_identity(
    context: TaskContext, field: str, value: Any,
) -> None:
    current = replace(record(context), **{field: value})
    with (
        api().bind_execution_authority(context, worker_id="bound-worker"),
        pytest.raises(api().ExecutionAuthorityError),
    ):
        get(context, current_record=current)


@pytest.mark.parametrize("key,value", [
    ("project_id", "other-project"), ("workspace_session_id", "other-session"),
    ("sandbox_profile", "restricted"), ("execution_backend", "docker"),
    ("requested_permissions", ["workspace.read", "command.run", "network.read"]),
    ("runtime_timeout_seconds", 600), ("generated_project_contract_version", True),
    ("project_input", {"schema_version": 1, "kind": "uploaded"}),
])
def test_current_route_cannot_change_stable_project_or_policy(
    context: TaskContext, key: str, value: Any,
) -> None:
    current = record(context)
    assert current.routing_decision is not None
    current.routing_decision[key] = value
    with (
        api().bind_execution_authority(context, worker_id="bound-worker"),
        pytest.raises(api().ExecutionAuthorityError),
    ):
        get(context, current_record=current)


def test_raw_tool_arguments_cannot_act_as_context_or_current_record(context: TaskContext) -> None:
    with (
        pytest.raises(api().ExecutionAuthorityError),
        api().bind_execution_authority(context.to_payload(), worker_id="forged-worker"),
    ):
        pytest.fail("raw payload cannot bind")
    with (
        api().bind_execution_authority(context, worker_id="bound-worker"),
        pytest.raises(api().ExecutionAuthorityError),
    ):
        get(context, current_record=context.to_payload())
