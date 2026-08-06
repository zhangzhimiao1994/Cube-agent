from __future__ import annotations

from uuid import UUID

import pytest

from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.channels.feishu.cards import (
    FeishuActionError,
    FeishuCardBuilder,
    InMemoryFeishuActionStore,
    consume_approval_action,
    consume_mode_action,
)
from agent_hub.channels.feishu.commands import (
    FeishuCommandKind,
    FeishuCommandParser,
)
from agent_hub.domain.runs import TaskMode

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
USER_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")


def principal(role: Role) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(user_id=USER_ID, tenant_id=TENANT_ID, role=role)


def test_non_admin_cannot_publish_config() -> None:
    result = FeishuCommandParser().parse(
        "/config publish 12",
        principal(Role.OPERATOR),
    )

    assert result.error_code == "permission_denied"


def test_admin_can_publish_config_revision() -> None:
    result = FeishuCommandParser().parse(
        "/config publish 12",
        principal(Role.ADMIN),
    )

    assert result.accepted is True
    assert result.command is not None
    assert result.command.kind is FeishuCommandKind.CONFIG
    assert result.command.name == "publish"
    assert result.command.arguments == ("12",)


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("/auto", TaskMode.AUTO),
        ("/direct", TaskMode.DIRECT),
        ("/dispatch", TaskMode.DISPATCH),
        ("/discuss", TaskMode.DISCUSS),
        ("/hybrid", TaskMode.HYBRID),
    ],
)
def test_mode_commands_are_explicit(text: str, mode: TaskMode) -> None:
    result = FeishuCommandParser().parse(text, principal(Role.OPERATOR))

    assert result.accepted is True
    assert result.command is not None
    assert result.command.kind is FeishuCommandKind.MODE
    assert result.command.mode is mode


def test_unknown_flags_and_unsafe_arguments_are_rejected() -> None:
    parser = FeishuCommandParser()

    assert parser.parse("/status --json", principal(Role.OPERATOR)).error_code == "unknown_flag"
    assert (
        parser.parse("/config publish revision: 1", principal(Role.ADMIN)).error_code
        == "invalid_argument"
    )
    assert parser.parse("/config raw foo", principal(Role.ADMIN)).error_code == "unknown_subcommand"


def test_run_commands_require_specific_permissions() -> None:
    parser = FeishuCommandParser()

    assert parser.parse("/cancel run_1", principal(Role.VIEWER)).error_code == "permission_denied"
    result = parser.parse("/cancel run_1", principal(Role.OPERATOR))

    assert result.accepted is True
    assert result.command is not None
    assert result.command.name == "cancel"


def test_skill_and_mcp_use_structured_subcommands() -> None:
    parser = FeishuCommandParser()

    assert parser.parse("/skill approve pkg_1", principal(Role.ADMIN)).accepted is True
    assert parser.parse("/mcp enable server_1", principal(Role.ADMIN)).accepted is True
    assert parser.parse("/skill approve pkg_1", principal(Role.OPERATOR)).error_code == (
        "permission_denied"
    )


def test_mode_card_action_is_one_time_and_bound_to_actor() -> None:
    store = InMemoryFeishuActionStore()
    card = FeishuCardBuilder(store).mode_choice_card(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        run_id=RUN_ID,
        options=(TaskMode.DIRECT, TaskMode.DISPATCH),
        reason="low_confidence",
    )
    actions = card.payload["actions"]
    assert isinstance(actions, list)
    action = actions[0]
    assert isinstance(action, dict)
    value = action["value"]
    assert isinstance(value, dict)
    action_id = value["action_id"]
    assert isinstance(action_id, str)

    with pytest.raises(FeishuActionError, match="mismatch"):
        consume_mode_action(
            store,
            action_id,
            tenant_id=TENANT_ID,
            user_id=OTHER_USER_ID,
            run_id=RUN_ID,
            mode=TaskMode.DIRECT,
            index=1,
        )

    consume_mode_action(
        store,
        action_id,
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        run_id=RUN_ID,
        mode=TaskMode.DIRECT,
        index=1,
    )
    with pytest.raises(FeishuActionError, match="not found"):
        consume_mode_action(
            store,
            action_id,
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            run_id=RUN_ID,
            mode=TaskMode.DIRECT,
            index=1,
        )


def test_approval_card_action_expires() -> None:
    now = 10.0
    store = InMemoryFeishuActionStore(monotonic=lambda: now)
    card = FeishuCardBuilder(store, action_ttl_seconds=1).approval_card(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        run_id=RUN_ID,
        approval_id="approval_1",
        summary="read external URL",
    )
    actions = card.payload["actions"]
    assert isinstance(actions, list)
    action = actions[0]
    assert isinstance(action, dict)
    value = action["value"]
    assert isinstance(value, dict)
    action_id = value["action_id"]
    assert isinstance(action_id, str)
    now = 12.0

    with pytest.raises(FeishuActionError, match="expired"):
        consume_approval_action(
            store,
            action_id,
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            run_id=RUN_ID,
            approval_id="approval_1",
            decision="approve",
        )
