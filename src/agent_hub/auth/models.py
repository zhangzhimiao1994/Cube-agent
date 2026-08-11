"""Stable authentication and authorization domain types."""

import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID


class Role(StrEnum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


PERMISSIONS = MappingProxyType(
    {
        Role.SUPER_ADMIN: frozenset({"*"}),
        Role.ADMIN: frozenset(
            {
                "config:*",
                "agent:*",
                "skill:read",
                "skill:use",
                "skill:write",
                "skill:approve",
                "mcp:read",
                "mcp:use",
                "mcp:write",
                "memory:*",
                "hermes:*",
                "run:*",
                "plugin:read",
                "plugin:use",
                "plugin:write",
                "user:read",
                "user:write",
                "audit:read",
            }
        ),
        Role.OPERATOR: frozenset(
            {
                "run:create",
                "run:read",
                "run:pause",
                "run:resume",
                "run:cancel",
                "config:read",
                "skill:read",
                "skill:use",
                "mcp:read",
                "mcp:use",
                "plugin:read",
                "plugin:use",
            }
        ),
        Role.VIEWER: frozenset(
            {
                "run:read",
                "config:read",
                "skill:read",
                "skill:use",
                "mcp:read",
                "mcp:use",
                "plugin:read",
                "plugin:use",
            }
        ),
    }
)

_PERMISSION_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}:[a-z][a-z0-9_-]{0,63}\Z")


class PermissionDenied(RuntimeError):
    pass


class BootstrapClosed(RuntimeError):
    pass


class InvalidBootstrapCode(PermissionDenied):
    pass


class InvalidCredentials(PermissionDenied):
    pass


class UsernameValidationError(ValueError):
    pass


class AuthenticationPersistenceError(RuntimeError):
    pass


class AuthenticationBusy(RuntimeError):
    pass


class AuthenticationOperationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    user_id: UUID
    tenant_id: UUID
    role: Role


@dataclass(frozen=True, slots=True, repr=False)
class AuthResult:
    principal: AuthenticatedPrincipal
    access_token: str

    def __repr__(self) -> str:
        return f"AuthResult(principal={self.principal!r}, access_token=<redacted>)"

    __str__ = __repr__


class Authorizer:
    """Apply exact and namespace-wildcard permission grants."""

    def is_allowed(self, principal: AuthenticatedPrincipal, permission: str) -> bool:
        if not _is_safe_permission(permission):
            return False
        grants = PERMISSIONS[principal.role]
        if "*" in grants or permission in grants:
            return True
        namespace, _ = permission.split(":", 1)
        return f"{namespace}:*" in grants

    def require(self, principal: AuthenticatedPrincipal, permission: str) -> AuthenticatedPrincipal:
        if not self.is_allowed(principal, permission):
            raise PermissionDenied("permission denied")
        return principal


def _is_safe_permission(permission: str) -> bool:
    return (
        isinstance(permission, str)
        and len(permission) <= 129
        and _PERMISSION_PATTERN.fullmatch(permission) is not None
    )
