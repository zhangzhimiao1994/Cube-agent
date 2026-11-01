from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from typing import Never
from uuid import UUID, uuid4

import pytest

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.db.base import Base
from agent_hub.runs import authorization as authorization_api
from tests.unit.runs.authenticated_inputs_typed_fixture import (
    AuthorizationModule,
    ForbiddenSessions,
    RecordingSession,
    assign_attribute,
    capture_untrusted_principal,
    evidence_changes,
    present,
)


def authorization() -> AuthorizationModule:
    # A missing feature must be an explicit assertion failure during RED.
    import importlib.util

    assert importlib.util.find_spec("agent_hub.runs.authorization") is not None
    return authorization_api


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(uuid4(), uuid4(), Role.OPERATOR)


def test_private_immutable_exact_utf8_input_and_canonical_selection() -> None:
    api = authorization()
    user = principal()
    attachments = ["a"]
    scope: dict[str, object] = {
        "project_id": "private-project",
        "attachments": attachments,
        "mode": "direct",
    }
    evidence = api._capture_authenticated_input(user, "  原文\n", scope)
    attachments.append("changed")
    assert evidence.message == "  原文\n"
    assert evidence.message_sha256 == sha256("  原文\n".encode()).hexdigest()
    assert json.loads(evidence.input_scope)["attachments"] == ["a"]
    assert evidence.scope_sha256 == sha256(evidence.input_scope.encode("utf8")).hexdigest()
    assert evidence.tenant_id == user.tenant_id
    assert evidence.actor_id == user.user_id
    assert evidence.actor_role is user.role
    assert type(evidence.turn_id) is UUID
    assert api._capture_authenticated_input(user, evidence.message, {}).turn_id != evidence.turn_id
    assert "原文" not in repr(evidence)
    assert "private-project" not in repr(evidence)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        assign_attribute(evidence, "message", "changed")
    equivalent = api._capture_authenticated_input(
        user, "  原文\n", {"mode": "direct", "attachments": ["a"], "project_id": "private-project"}
    )
    assert evidence.input_scope == equivalent.input_scope


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "actor_id",
        "actor_role",
        "message",
        "message_sha256",
        "scope_sha256",
        "input_scope",
        "_seal",
    ],
)
def test_binding_rejects_tampering_without_disclosing_body(field: str) -> None:
    api = authorization()
    user = principal()
    evidence = api._capture_authenticated_input(user, "secret-body", {"project_id": "secret-scope"})
    bad: dict[str, object] = {
        "tenant_id": uuid4(),
        "actor_id": uuid4(),
        "actor_role": Role.ADMIN,
        "message": "different",
        "message_sha256": "0" * 64,
        "scope_sha256": "0" * 64,
        "input_scope": "{}",
        "_seal": None,
    }
    forged = replace(evidence, **evidence_changes(field, bad[field]))
    with pytest.raises(ValueError) as error:
        api._validate_authenticated_input(
            forged, user.tenant_id, user.user_id, user.role, "secret-body"
        )
    assert "secret-body" not in str(error.value)
    assert "secret-scope" not in str(error.value)


def test_only_a_live_principal_can_mint_and_non_json_scope_is_rejected() -> None:
    api = authorization()
    with pytest.raises(ValueError):
        capture_untrusted_principal({"role": "admin"}, "body", {})
    with pytest.raises(ValueError):
        api._capture_authenticated_input(principal(), "body", {"x": float("nan")})


def test_persisted_loader_validation_does_not_promote_derived_run_request() -> None:
    api = authorization()
    from agent_hub.db.models import RunAuthenticatedInputRow, RunInputAuthorizationRow, RunRow

    user = principal()
    evidence = api._capture_authenticated_input(user, "original", {"mode": "direct"})
    run = RunRow(
        id=uuid4(),
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_role=user.role.value,
        request="internal repair text",
    )
    turn = RunAuthenticatedInputRow(
        turn_id=evidence.turn_id,
        run_id=run.id,
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_role=user.role.value,
        message=evidence.message,
        message_sha256=evidence.message_sha256,
        input_scope=evidence.input_scope,
        scope_sha256=evidence.scope_sha256,
    )
    link = RunInputAuthorizationRow(
        run_id=run.id, tenant_id=user.tenant_id, turn_id=evidence.turn_id
    )
    loaded = present(api._validated_persisted_input(run, link, turn))
    assert loaded.message == "original"
    for obj, field, bad in [
        (turn, "message", "corrupt"),
        (turn, "scope_sha256", "0" * 64),
        (turn, "actor_id", uuid4()),
        (link, "tenant_id", uuid4()),
        (link, "turn_id", uuid4()),
        (run, "actor_role", "admin"),
    ]:
        old = getattr(obj, field)
        setattr(obj, field, bad)
        assert api._validated_persisted_input(run, link, turn) is None
        setattr(obj, field, old)
    assert api._validated_persisted_input(run, None, turn) is None
    assert api._validated_persisted_input(run, link, None) is None


async def test_edit_persistence_keeps_raw_input_separate_from_normalized_run_request() -> None:
    from agent_hub.db.models import RunAuthenticatedInputRow, RunRow

    api = authorization()
    user = principal()
    evidence = api._capture_authenticated_input(
        user, "  edited 文本\n", {"operation": "queue_edit"}
    )
    run = RunRow(
        id=uuid4(),
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_role=user.role.value,
        request="edited 文本",
    )
    added: list[Base] = []
    session = RecordingSession(added)
    await api._persist_authenticated_input(
        session, run, evidence, submitted_message="  edited 文本\n"
    )
    assert isinstance(added[0], RunAuthenticatedInputRow)
    assert added[0].message == "  edited 文本\n"
    assert added[0].message_sha256 == sha256("  edited 文本\n".encode()).hexdigest()
    assert run.request == "edited 文本"


async def test_repository_rejects_mismatched_input_before_any_database_call() -> None:
    from agent_hub.domain.runs import RunStatus, TaskMode
    from agent_hub.runs.repository import RunRepository

    api = authorization()
    user = principal()

    def forbidden_database() -> Never:
        raise AssertionError("invalid evidence reached the database")

    repository = RunRepository(ForbiddenSessions(forbidden_database))
    with pytest.raises(ValueError):
        await repository.create_run(
            tenant_id=uuid4(),
            actor_id=user.user_id,
            actor_role=user.role,
            request="original",
            mode=TaskMode.DIRECT,
            status=RunStatus.QUEUED,
            enqueue=True,
            idempotency_key=None,
            authenticated_input=api._capture_authenticated_input(user, "original", {}),
        )
