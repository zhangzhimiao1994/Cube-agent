import pytest
from pydantic import ValidationError

from agent_hub.api.routers.admin import (
    InMemoryAdminResourceService,
    SystemSettingsRequest,
    SystemSettingsResponse,
    _persisted_system_settings_payload,
)
from tests.api.test_runs_api import _client, bearer


def test_settings_permission_contract_and_response_only_capabilities():
    settings = SystemSettingsResponse()
    assert settings.default_permission_mode == "default"
    assert settings.default_sandbox_profile == "workspace_write"
    assert settings.supported_permission_modes == ("default", "auto_review", "read_only")
    assert "supported_permission_modes" not in _persisted_system_settings_payload(settings)
    with pytest.raises(ValidationError):
        SystemSettingsRequest(supported_permission_modes=["default"])


@pytest.mark.parametrize("field,value", [
    ("default_permission_mode", "full_access"), ("default_sandbox_profile", "full_access"),
])
def test_settings_reject_unsupported_permissions(field, value):
    with pytest.raises(ValidationError):
        SystemSettingsRequest(**{field: value})


@pytest.mark.parametrize("mode,profile", [("default", "restricted"), ("auto_review", "none"),
                                         ("read_only", "read_only")])
def test_explicit_queue_mode_resolves_configured_profile(mode, profile):
    settings = InMemoryAdminResourceService()
    settings.settings = SystemSettingsResponse(default_sandbox_profile="restricted")
    client, service, _ = _client(settings_service=settings)
    payload = {"message": "inspect", "mode": "direct", "permission_mode": mode}
    if mode == "auto_review":
        payload["sandbox_profile"] = profile
    response = client.post("/api/v1/admin/conversations/conv-permission/queue",
                           headers={**bearer(), "Idempotency-Key": "permission-queue"}, json=payload)
    assert response.status_code == 202, response.text
    options = service.queued_submit_options[-1]
    assert options["permission_mode"] == mode
    assert options["sandbox_profile"] == profile
    if mode == "read_only":
        assert options["requested_permissions"] == ("workspace.read",)


@pytest.mark.parametrize("payload", [
    {"permission_mode": "full_access"},
    {"permission_mode": "read_only", "sandbox_profile": "workspace_write"},
    {"permission_mode": "read_only", "requested_permissions": ["command.run"]},
])
@pytest.mark.parametrize("path", ["/api/v1/runs", "/api/v1/admin/conversations/conv-permission/queue"])
def test_api_rejects_unsupported_or_mismatched_permission_modes(payload, path):
    client, service, _ = _client()
    response = client.post(path, headers={**bearer(), "Idempotency-Key": "bad-mode"},
                           json={"message": "inspect", "mode": "direct", **payload})
    assert response.status_code == 422, response.text
    assert not service.submitted and not service.queued_submit_options


def test_legacy_queue_does_not_receive_new_kwargs_or_settings_defaults():
    settings = InMemoryAdminResourceService()
    settings.settings = SystemSettingsResponse(default_permission_mode="read_only",
                                              default_sandbox_profile="read_only")
    client, service, _ = _client(settings_service=settings)
    response = client.post("/api/v1/admin/conversations/conv-permission/queue",
                           headers={**bearer(), "Idempotency-Key": "legacy-mode"},
                           json={"message": "inspect", "mode": "direct"})
    assert response.status_code == 202
    assert "permission_mode" not in service.queued_submit_options[-1]
    assert service.queued_submit_options[-1]["sandbox_profile"] == "workspace_write"


@pytest.mark.parametrize("values", [
    {"default_permission_mode": "read_only", "default_sandbox_profile": "workspace_write"},
    {"default_permission_mode": "auto_review", "tool_approval_mode": "ask"},
])
def test_settings_reject_conflicting_permission_defaults(values):
    with pytest.raises(ValidationError):
        SystemSettingsRequest(**values)
