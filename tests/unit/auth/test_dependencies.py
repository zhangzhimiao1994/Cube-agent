import pytest

from agent_hub.auth.dependencies import require_permission
from agent_hub.auth.models import AuthenticatedPrincipal, Authorizer, PermissionDenied, Role


def test_permission_dependency_is_a_thin_principal_check() -> None:
    from uuid import uuid4

    dependency = require_permission(Authorizer(), "run:create")
    viewer = AuthenticatedPrincipal(uuid4(), uuid4(), Role.VIEWER)

    with pytest.raises(PermissionDenied):
        dependency(viewer)
