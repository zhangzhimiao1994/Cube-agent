import hashlib
import json
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agent_hub.api.routers import admin
from agent_hub.app import _deployment_from_model_resource, _MainAgentModeRouter
from agent_hub.config.schema import PlatformConfig
from agent_hub.config.service import _validated_document
from agent_hub.models.types import ModelCapability
from agent_hub.runtime.defaults import _deployments
from tests.api.test_admin_resources import (
    ACTOR_ID,
    TENANT_ID,
    FakeConfigService,
    FakeModelTransport,
    FakeSecretService,
    client,
    headers,
    model_payload,
)


def request_values(**overrides: Any) -> dict[str, Any]:
    return {
        **model_payload(),
        "api_base": "https://provider.example/v1",
        "fallback": None,
        "capabilities": ["text", "structured_output"],
        **overrides,
    }


def main_values(**overrides: Any) -> dict[str, Any]:
    values = request_values(**overrides)
    return {
        key: values[key]
        for key in (
            "provider",
            "api_base",
            "upstream_model",
            "credential_ref",
            "capabilities",
            "max_concurrency",
            "structured_output_api",
        )
        if key in values
    }


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
def test_model_api_round_trip_preserves_protocol_on_omitted_edit(protocol: str) -> None:
    api = client()
    values = request_values(structured_output_api=protocol)
    created = api.post("/api/v1/admin/models", headers=headers(), json=values)
    assert created.status_code == 200, created.text
    body = created.json()
    omitted = request_values(max_concurrency=2)
    updated = api.put(f"/api/v1/admin/models/{body['id']}", headers=headers(), json=omitted)
    assert updated.status_code == 200, updated.text
    assert updated.json()["structured_output_api"] == protocol
    assert updated.json()["credential_ref"] == values["credential_ref"]
    assert updated.json()["api_base"] == values["api_base"]
    listed = api.get("/api/v1/admin/models", headers=headers()).json()
    assert (
        next(item for item in listed if item["id"] == body["id"])["structured_output_api"]
        == protocol
    )
    reset = api.put(
        f"/api/v1/admin/models/{body['id']}",
        headers=headers(),
        json={**omitted, "structured_output_api": "chat_completions"},
    )
    assert reset.status_code == 200
    assert reset.json()["structured_output_api"] == "chat_completions"


@pytest.mark.parametrize("invalid", ["auto", "json_object", "RESPONSES", None])
def test_admin_rejects_invalid_structured_output_api(invalid: object) -> None:
    with pytest.raises(ValidationError, match="structured_output_api"):
        admin.ModelDeploymentRequest.model_validate(request_values(structured_output_api=invalid))
    with pytest.raises(ValidationError, match="structured_output_api"):
        admin.MainAgentModelConfig.model_validate(main_values(structured_output_api=invalid))


async def test_published_protocol_survives_edits_probe_runtime_and_fingerprint() -> None:
    configs, transport = FakeConfigService(), FakeModelTransport()
    service = admin.PersistentAdminResourceService(
        config_service=configs,  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )
    created = await service.create_model(
        admin.ModelDeploymentRequest.model_validate(request_values())
    )
    assert configs.current is not None
    original = deepcopy(_validated_document(configs.current.document, include_defaults=True))
    updated = await service.update_model(
        created.id,
        admin.ModelDeploymentRequest.model_validate(
            request_values(structured_output_api="responses"),
        ),
    )
    assert updated.structured_output_api == "responses"
    assert configs.current is not None
    native = _validated_document(configs.current.document, include_defaults=True)
    expected = deepcopy(original)
    expected["models"]["planner"]["deployments"][0]["structured_output_api"] = "responses"  # type: ignore[index]
    assert native == expected

    def fingerprint(doc: dict[str, object]) -> str:
        return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()

    assert fingerprint(original) != fingerprint(native)
    published = await service.update_model(
        updated.id,
        admin.ModelDeploymentRequest.model_validate(
            request_values(),
        ),
    )
    assert configs.current is not None
    assert _validated_document(configs.current.document, include_defaults=True) == native
    assert published.structured_output_api == "responses"
    assert (await service.list_models())[0].structured_output_api == "responses"
    assert (
        _deployments(PlatformConfig.model_validate(configs.current.document))[
            0
        ].structured_output_api
        == "responses"
    )
    assert _deployment_from_model_resource(published).structured_output_api == "responses"
    assert transport.calls[-1][0].structured_output_api == "responses"
    assert transport.calls[-1][0].secret_ref == request_values()["credential_ref"]
    assert transport.calls[-1][0].api_base == request_values()["api_base"]
    assert transport.calls[-1][1].response_schema is not None
    assert ModelCapability.STRUCTURED_OUTPUT in transport.calls[-1][1].required_capabilities
    assert transport.calls[-1][1].max_output_tokens == 256
    reset = await service.update_model(
        published.id,
        admin.ModelDeploymentRequest.model_validate(
            request_values(structured_output_api="chat_completions"),
        ),
    )
    assert reset.structured_output_api == "chat_completions"
    assert _validated_document(configs.current.document, include_defaults=True) == original
    assert transport.calls[-1][1].response_schema is None


@pytest.mark.parametrize("persistent", [False, True])
async def test_main_agent_preserves_structured_protocol_when_edit_omits_it(
    persistent: bool,
) -> None:
    transport = FakeModelTransport()
    if persistent:
        service = admin.PersistentAdminResourceService(
            config_service=FakeConfigService(),  # type: ignore[arg-type]
            secret_service=FakeSecretService(),  # type: ignore[arg-type]
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
            model_transport=transport,
        )
        storage: dict[str, Any] = {}

        async def persist(_kind: str, _key: str, payload: dict[str, Any]) -> bool:
            storage.update(payload)
            return True

        service._get_admin_payload = AsyncMock(side_effect=lambda *_: storage or None)  # type: ignore[method-assign]
        service._upsert_admin_payload = AsyncMock(side_effect=persist)  # type: ignore[method-assign]
        service._record_audit = AsyncMock()  # type: ignore[method-assign]
    else:
        service = admin.InMemoryAdminResourceService()  # type: ignore[assignment]
    await service.update_main_agent_config(
        admin.MainAgentConfigRequest.model_validate(
            {
                "model": main_values(structured_output_api="responses"),
            }
        )
    )
    changed = await service.update_main_agent_config(
        admin.MainAgentConfigRequest.model_validate(
            {
                "model": main_values(),
                "control_mode": "planner",
            }
        )
    )
    assert changed.model is not None
    assert changed.model.structured_output_api == "responses"
    loaded = await service.get_main_agent_config()
    assert loaded.model is not None and loaded.model.structured_output_api == "responses"
    deployment = admin._main_agent_model_deployment(changed.model)
    assert deployment.structured_output_api == "responses"
    assert deployment.api_base == main_values()["api_base"]
    assert deployment.secret_ref == main_values()["credential_ref"]
    if persistent:
        assert storage["model"]["structured_output_api"] == "responses"
        assert transport.calls[-1][0].structured_output_api == "responses"
    reset = await service.update_main_agent_config(
        admin.MainAgentConfigRequest.model_validate(
            {
                "model": main_values(structured_output_api="chat_completions"),
            }
        )
    )
    assert reset.model is not None and reset.model.structured_output_api == "chat_completions"
    if persistent:
        assert "structured_output_api" not in storage["model"]
        reloaded = await service.get_main_agent_config()
        assert reloaded.model is not None
        assert reloaded.model.structured_output_api == "chat_completions"


async def test_main_agent_registered_deployment_construction_keeps_protocol() -> None:
    service = admin.InMemoryAdminResourceService()
    registered = await service.create_model(
        admin.ModelDeploymentRequest.model_validate(
            request_values(structured_output_api="responses"),
        )
    )
    model = admin.MainAgentModelConfig.model_validate(
        main_values(structured_output_api="responses")
    )
    router = _MainAgentModeRouter(
        get_config=service.get_main_agent_config,
        list_models=service.list_models,
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        redis_client=object(),
        transport=FakeModelTransport(),
    )
    deployment = await router._deployment_from_config(model)
    assert deployment.quota_scope_id == registered.quota_scope
    assert deployment.structured_output_api == "responses"


async def test_responses_setting_does_not_change_text_only_availability_probe() -> None:
    transport = FakeModelTransport()
    service = admin.PersistentAdminResourceService(
        config_service=FakeConfigService(),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        model_transport=transport,
    )
    await service.create_model(
        admin.ModelDeploymentRequest.model_validate(
            request_values(
                structured_output_api="responses",
                capabilities=["text"],
                provider="local",
                upstream_model="local-text",
            )
        )
    )
    deployment, request, _ = transport.calls[0]
    assert deployment.structured_output_api == "responses"
    assert request.response_schema is None
    assert request.required_capabilities == frozenset({ModelCapability.TEXT})
    assert request.max_output_tokens == 32
