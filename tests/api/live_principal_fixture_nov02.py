"""Typed current-user validation shared only by the API auth contract fixtures."""

from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials


def require_current_principal(
    principal: AuthenticatedPrincipal,
    current: AuthenticatedPrincipal | None,
    *,
    disabled: bool = False,
) -> AuthenticatedPrincipal:
    """Model an enabled live identity independently of the decoded token claims."""
    if current is None or disabled or principal != current:
        raise InvalidCredentials("principal is no longer current")
    return current
