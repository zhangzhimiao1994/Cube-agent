from uuid import uuid4

import pytest

from agent_hub.auth.models import (
    PERMISSIONS,
    AuthenticatedPrincipal,
    Authorizer,
    PermissionDenied,
    Role,
)

EXPECTED_PERMISSIONS = {
    Role.SUPER_ADMIN: frozenset({"*"}),
    Role.ADMIN: frozenset(
        {
            "config:*",
            "agent:*",
            "skill:*",
            "mcp:*",
            "memory:*",
            "hermes:*",
            "run:*",
            "audit:read",
        }
    ),
    Role.OPERATOR: frozenset(
        {"run:create", "run:read", "run:pause", "run:resume", "run:cancel", "config:read"}
    ),
    Role.VIEWER: frozenset({"run:read", "config:read", "audit:read"}),
}


def _principal(role: Role) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(user_id=uuid4(), tenant_id=uuid4(), role=role)


def test_role_values_and_permission_sets_are_stable() -> None:
    assert [role.value for role in Role] == ["super_admin", "admin", "operator", "viewer"]
    assert PERMISSIONS == EXPECTED_PERMISSIONS


@pytest.mark.parametrize(
    ("role", "permission", "allowed"),
    [
        (Role.SUPER_ADMIN, "anything:read", True),
        (Role.ADMIN, "config:write", True),
        (Role.ADMIN, "memory:write", True),
        (Role.ADMIN, "hermes:write", True),
        (Role.ADMIN, "audit:read", True),
        (Role.ADMIN, "audit:write", False),
        (Role.OPERATOR, "run:create", True),
        (Role.OPERATOR, "run:delete", False),
        (Role.VIEWER, "run:read", True),
        (Role.VIEWER, "run:create", False),
    ],
)
def test_authorizer_enforces_the_rbac_matrix(role: Role, permission: str, allowed: bool) -> None:
    assert Authorizer().is_allowed(_principal(role), permission) is allowed


@pytest.mark.parametrize(
    "permission",
    [
        "configevil:read",
        "config",
        "config::read",
        "config:*:x",
        ":read",
        "config:",
        " config:read",
        "config:read ",
        "CONFIG:read",
        "config/read",
        "a" * 129 + ":read",
    ],
)
def test_namespace_wildcard_never_matches_confusing_or_unsafe_input(permission: str) -> None:
    assert Authorizer().is_allowed(_principal(Role.ADMIN), permission) is False


def test_require_raises_a_stable_safe_error() -> None:
    principal = _principal(Role.VIEWER)

    with pytest.raises(PermissionDenied, match="permission denied") as captured:
        Authorizer().require(principal, "run:create")

    message = str(captured.value)
    assert "run:create" not in message
    assert str(principal.user_id) not in message


def test_principal_is_immutable_and_has_no_secret_fields() -> None:
    principal = _principal(Role.ADMIN)

    with pytest.raises((AttributeError, TypeError)):
        principal.role = Role.VIEWER  # type: ignore[misc]

    assert not hasattr(principal, "token")
    assert not hasattr(principal, "password")
