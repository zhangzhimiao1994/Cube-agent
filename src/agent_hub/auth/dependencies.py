"""Framework-neutral authorization dependency factories."""

from collections.abc import Callable

from agent_hub.auth.models import AuthenticatedPrincipal, Authorizer


def require_permission(
    authorizer: Authorizer, permission: str
) -> Callable[[AuthenticatedPrincipal], AuthenticatedPrincipal]:
    def dependency(principal: AuthenticatedPrincipal) -> AuthenticatedPrincipal:
        return authorizer.require(principal, permission)

    return dependency
