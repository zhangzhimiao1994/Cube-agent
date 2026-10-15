from uuid import UUID, uuid4

import pytest

from agent_hub.api.routers.admin import PersistentAdminResourceService


@pytest.mark.parametrize("payload", [None, {}])
async def test_missing_persistent_install_state_is_none(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object] | None,
) -> None:
    tenant_id = uuid4()

    async def read(
        self: PersistentAdminResourceService, kind: str, resource_id: str,
        *, tenant_id: UUID | None = None,
    ) -> dict[str, object] | None:
        assert kind == "capability_install" and resource_id == "fixture"
        return payload

    monkeypatch.setattr(PersistentAdminResourceService, "_get_admin_payload", read)
    service = PersistentAdminResourceService.__new__(PersistentAdminResourceService)
    assert await service.get_capability_install_state("fixture", tenant_id=tenant_id) is None
