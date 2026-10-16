"""Real builtin manifests must survive the ordinary-user HTTP response boundary."""
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import ValidationError

from agent_hub.api.routers.admin import CapabilityManifestItemResponse, InMemoryAdminResourceService
from agent_hub.app import create_app
from agent_hub.auth.models import AuthenticatedPrincipal, Role
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.settings import Settings
from tests.api.test_runs_api import StubAuthService, bearer


def test_ordinary_operator_reads_actual_builtin_manifest(tmp_path: Path) -> None:
    principal = AuthenticatedPrincipal(UUID(int=1), UUID(int=2), Role.OPERATOR)
    app = create_app(auth_service=StubAuthService(principal), rate_limiter=object())
    app.state.admin_resource_service = InMemoryAdminResourceService()
    app.state.runtime_capability_gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path)
    app.state.settings = Settings.model_construct(plugin_package_store_dir=tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/v1/admin/capabilities/manifest", headers=bearer())
    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()["capabilities"]}
    for name, field, value in (
        ("calculator.evaluate", "expression", "2 + 2"),
        ("http.read", "url", "https://example.com/read"),
        ("workspace.read", "path", "src/main.ts"),
        ("document.generate_docx", "title", "Report"),
        ("presentation.generate_pptx", "title", "Review"),
    ):
        item = items[name]
        assert item["description"]
        assert item["input_contract_version"] == 1
        assert set(item["input_contract_added_fields"]) == {"input_schema", "description"}
        validator = Draft202012Validator(item["input_schema"])
        assert validator.is_valid({field: value})
        assert not validator.is_valid({})
    assert client.get("/api/v1/admin/capabilities/manifest").status_code == 401


@pytest.mark.parametrize("field,value", (
    ("input_contract_version", True),
    ("input_contract_version", 2),
    ("input_contract_added_fields", ["permission_class"]),
))
def test_contract_metadata_does_not_disable_strict_response_validation(field: str, value: object) -> None:
    payload = {"id": "workspace.read", "kind": "builtin", "adapter": "runtime_builtin",
               "permission_class": "file.read", "sandbox_profile": "workspace_read",
               "available": True, "replay_safe": True, field: value}
    with pytest.raises(ValidationError):
        CapabilityManifestItemResponse.model_validate(payload)
