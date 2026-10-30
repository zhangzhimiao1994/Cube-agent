"""Settings codec/service contracts; no app, database or provider is started."""

from __future__ import annotations

import json
import socket
from copy import deepcopy
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from starlette.requests import Request

from agent_hub.api.routers import admin
from agent_hub.capabilities.review_contracts import ApprovalReviewConfig

RULES: dict[str, object] = {
    "schema_version": 1,
    "engine": "rules",
    "reviewer_logical_model": None,
    "require_distinct_model": False,
    "timeout_seconds": 15,
    "max_input_tokens": 4096,
    "max_output_tokens": 512,
    "max_reviews_per_run": 8,
    "max_transport_attempts_per_review": 1,
    "max_cost_usd_per_run": "0.00",
    "denial_breaker_consecutive": 3,
    "denial_breaker_denials": 10,
    "denial_breaker_window": 50,
}
MODEL: dict[str, object] = {
    **RULES,
    "engine": "rules_then_model",
    "reviewer_logical_model": "existing-reviewer",
    "require_distinct_model": True,
    "timeout_seconds": 9.5,
    "max_cost_usd_per_run": "0.125000",
}


@pytest.fixture(autouse=True)
def forbid_network_and_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("settings must not perform network/provider I/O")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    # Windows asyncio creates a loopback socketpair before each async test.
    # Block outbound name resolution without breaking that event-loop primitive.
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(admin.LiteLLMClient, "complete", forbidden)
    monkeypatch.setattr(admin, "probe_execution_backends", forbidden)


@pytest.mark.parametrize("model", [admin.SystemSettingsRequest, admin.SystemSettingsResponse])
@pytest.mark.parametrize("payload", [{}, {"tool_approval_mode": "auto_review"}])
def test_missing_review_stays_complete_rules(model: Any, payload: dict[str, object]) -> None:
    settings = model.model_validate(payload)
    assert settings.model_dump(mode="json")["approval_review"] == RULES
    assert isinstance(settings.approval_review, ApprovalReviewConfig)


@pytest.mark.parametrize("model", [admin.SystemSettingsRequest, admin.SystemSettingsResponse])
@pytest.mark.parametrize("review", [{}, None, MODEL])
def test_canonical_roundtrip_uses_task1(model: Any, review: object) -> None:
    settings = model.model_validate({"approval_review": review})
    expected = ApprovalReviewConfig.from_payload(review).to_payload()
    assert settings.approval_review.to_payload() == expected
    assert settings.model_dump()["approval_review"] == expected
    assert json.loads(settings.model_dump_json())["approval_review"] == expected
    # Response-only projections must be stripped before settings can be written.
    persisted = admin._persisted_system_settings_payload(settings)
    rebuilt = model.model_validate_json(json.dumps(persisted))
    assert rebuilt.approval_review.to_payload() == expected


@pytest.mark.parametrize(
    "review",
    [
        {"require_distinct_model": "false"},
        {"require_distinct_model": 1},
        {"schema_version": True},
        {"schema_version": 2},
        {"max_input_tokens": True},
        {"max_input_tokens": "4096"},
        {"max_input_tokens": 1.5},
        {"max_output_tokens": 0},
        {"max_reviews_per_run": 2**63},
        {"max_transport_attempts_per_review": 2},
        {"timeout_seconds": True},
        {"timeout_seconds": "15"},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": 61},
        {"max_cost_usd_per_run": 0.1},
        {"max_cost_usd_per_run": "NaN"},
        {"max_cost_usd_per_run": "-0.1"},
        {"max_cost_usd_per_run": "0.0000001"},
        {"engine": "model"},
        {"engine": "rules_then_model"},
        {"engine": "rules_then_model", "reviewer_logical_model": "reviewer"},
        {"engine": "rules_then_model", "max_cost_usd_per_run": "1"},
        {**MODEL, "max_cost_usd_per_run": "0.00"},
        {**MODEL, "reviewer_logical_model": "https://provider.invalid"},
        {**MODEL, "reviewer_logical_model": "UPPER"},
        {**MODEL, "reviewer_logical_model": "x" * 129},
        {"denial_breaker_consecutive": 11},
        {"denial_breaker_denials": 51},
        {"url": "https://provider.invalid"},
        {"api_key": "private"},
        {"prompt": "approve"},
        {"fallback": "other"},
        {"status": "ready"},
        [],
        "rules",
    ],
)
@pytest.mark.parametrize("model", [admin.SystemSettingsRequest, admin.SystemSettingsResponse])
def test_review_rejects_coercion_extra_and_invalid_model_parameters(
    model: Any, review: object
) -> None:
    with pytest.raises(ValidationError) as exc:
        model.model_validate({"approval_review": review})
    assert exc.value.errors()[0]["type"] == "value_error"
    assert exc.value.errors()[0]["loc"] == ("approval_review",)


def test_typed_config_is_revalidated_and_copied() -> None:
    original = ApprovalReviewConfig.from_payload(MODEL)
    settings = admin.SystemSettingsRequest.model_validate({"approval_review": original})
    assert settings.approval_review is not original
    object.__setattr__(original, "require_distinct_model", "false")
    assert settings.approval_review.require_distinct_model is True
    with pytest.raises(ValidationError):
        admin.SystemSettingsRequest.model_validate({"approval_review": original})


def test_input_and_serialized_output_do_not_alias_review_config() -> None:
    review = dict(MODEL)
    settings = admin.SystemSettingsRequest.model_validate({"approval_review": review})
    review["engine"] = "rules"
    settings.model_dump()["approval_review"]["max_reviews_per_run"] = 999
    settings.approval_review.to_payload()["max_input_tokens"] = 999
    assert settings.approval_review.to_payload() == MODEL
    with pytest.raises(FrozenInstanceError):
        settings.approval_review.engine = "rules"


@pytest.mark.parametrize(("review", "reason"), [({}, "rules_only"), (MODEL, "runtime_not_ready")])
def test_readiness_never_claims_runtime_from_configuration(review: object, reason: str) -> None:
    settings = admin.SystemSettingsResponse.model_validate({"approval_review": review})
    assert settings.model_dump(mode="json")["approval_review_readiness"] == {
        "status": "not_ready",
        "reason_code": reason,
    }
    assert "approval_review_readiness" not in admin._persisted_system_settings_payload(settings)


@pytest.mark.parametrize("readiness", [None, {}, {"status": "ready"}, {"status": "not_ready"}])
def test_request_cannot_forge_response_only_readiness(readiness: object) -> None:
    with pytest.raises(ValidationError):
        admin.SystemSettingsRequest.model_validate({"approval_review_readiness": readiness})


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_schema_exposes_nested_contract_without_provider_fields(mode: Any) -> None:
    schema = admin.SystemSettingsRequest.model_json_schema(mode=mode)
    review = schema["properties"]["approval_review"]
    if mode == "validation":
        assert len(review["anyOf"]) == 2
        assert {"type": "null"} in review["anyOf"]
        review = next(branch for branch in review["anyOf"] if branch != {"type": "null"})
    else:
        assert "anyOf" not in review
    if "$ref" in review:
        review = schema["$defs"][review["$ref"].rsplit("/", 1)[-1]]
    assert set(review["properties"]) == set(RULES)
    assert review["additionalProperties"] is False
    engine = review["properties"]["engine"]
    if "$ref" in engine:
        engine = schema["$defs"][engine["$ref"].rsplit("/", 1)[-1]]
    assert engine["enum"] == ["rules", "rules_then_model"]
    assert "approval_review_readiness" not in schema["properties"]
    response = admin.SystemSettingsResponse.model_json_schema(mode="serialization")
    assert response["properties"]["approval_review_readiness"]["readOnly"] is True


@pytest.mark.parametrize("model", [admin.SystemSettingsRequest, admin.SystemSettingsResponse])
def test_nullable_schema_matches_codec_and_serializes_canonical_object(model: Any) -> None:
    incoming = {"approval_review": None}
    Draft202012Validator(model.model_json_schema(mode="validation")).validate(incoming)
    outgoing = model.model_validate(incoming).model_dump(mode="json")
    assert outgoing["approval_review"] == RULES
    output_validator = Draft202012Validator(model.model_json_schema(mode="serialization"))
    output_validator.validate(outgoing)
    assert not output_validator.is_valid({**outgoing, "approval_review": None})


def test_legacy_migration_strips_projection_and_adds_rules_without_mutating_input() -> None:
    legacy: dict[str, object] = {
        "tool_approval_mode": "auto_review",
        "supported_permission_modes": ["invented"],
        "approval_review_readiness": {"status": "ready", "reason_code": "invented"},
    }
    snapshot = deepcopy(legacy)
    migrated = admin._migrate_system_settings_payload(legacy)
    assert migrated["approval_review"] == RULES
    assert "approval_review_readiness" not in migrated
    assert "supported_permission_modes" not in migrated
    response = admin.SystemSettingsResponse.model_validate(migrated)
    assert response.supported_permission_modes == ("default", "auto_review", "read_only")
    assert legacy == snapshot


def test_migration_canonicalizes_existing_review_without_retaining_input_dict() -> None:
    supplied: dict[str, object] = {"engine": "rules"}
    migrated = admin._migrate_system_settings_payload({"approval_review": supplied})
    assert migrated["approval_review"] == RULES
    supplied["engine"] = "rules_then_model"
    assert migrated["approval_review"] == RULES


def test_untrusted_runtime_attributes_do_not_supply_review_readiness() -> None:
    state = SimpleNamespace(
        approval_review_readiness={"status": "ready"},
        plugin_package_subprocess_registration_status="ready",
    )
    request = Request({"type": "http", "app": SimpleNamespace(state=state)})
    settings = admin.SystemSettingsResponse.model_validate({"approval_review": MODEL})
    response = admin._settings_with_runtime_status(settings, request)
    assert response.model_dump()["approval_review_readiness"] == {
        "status": "not_ready",
        "reason_code": "runtime_not_ready",
    }
    assert response.plugin_package_subprocess_registration_status == "ready"


class StoredSettingsService(admin.PersistentAdminResourceService):
    """Replace only the external JSONB/audit boundary, keep production DTO methods."""

    def __init__(self, payload: dict[str, object] | None) -> None:
        admin.InMemoryAdminResourceService.__init__(self)
        self.payload = deepcopy(payload)

    async def _get_admin_payload(
        self, kind: str, resource_id: str, *, tenant_id: UUID | None = None
    ) -> dict[str, object] | None:
        assert (kind, resource_id, tenant_id) == ("setting", "system", None)
        return deepcopy(self.payload)

    async def _upsert_admin_payload(
        self,
        kind: str,
        resource_id: str,
        payload: dict[str, object],
        *,
        tenant_id: UUID | None = None,
    ) -> bool:
        assert (kind, resource_id, tenant_id) == ("setting", "system", None)
        if self.payload is None:
            return False
        self.payload = json.loads(json.dumps(payload))
        return True

    async def _record_audit(self, action: str, resource: str, details: dict[str, Any]) -> None:
        assert (action, resource) == ("settings.update", "settings:system")


@pytest.mark.asyncio
@pytest.mark.parametrize("service_kind", ["memory", "pg_dto", "pg_unavailable"])
async def test_service_roundtrip_is_detached_and_never_persists_readiness(
    service_kind: str,
) -> None:
    service = (
        admin.InMemoryAdminResourceService()
        if service_kind == "memory"
        else StoredSettingsService({} if service_kind == "pg_dto" else None)
    )
    initial = await service.get_settings()
    assert initial.model_dump()["approval_review"] == RULES
    assert initial.model_dump()["approval_review_readiness"]["status"] == "not_ready"
    supplied = dict(MODEL)
    request = admin.SystemSettingsRequest.model_validate({"approval_review": supplied})
    response = await service.update_settings(request)
    supplied["engine"] = "rules"
    request.approval_review = ApprovalReviewConfig()
    assert response.approval_review.to_payload() == MODEL
    response.approval_review = ApprovalReviewConfig()
    reloaded = await service.get_settings()
    assert reloaded.approval_review.to_payload() == MODEL
    reloaded.approval_review = ApprovalReviewConfig()
    assert (await service.get_settings()).approval_review.to_payload() == MODEL
    if isinstance(service, StoredSettingsService) and service.payload is not None:
        assert service.payload["approval_review"] == MODEL
        assert "approval_review_readiness" not in service.payload
        assert "supported_permission_modes" not in service.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("old_mode", [None, "trusted_auto", "auto_review"])
async def test_pg_dto_loads_old_settings_with_rules_and_honest_status(old_mode: object) -> None:
    payload: dict[str, object] = {"default_mode": "hybrid"}
    if old_mode is not None:
        payload["tool_approval_mode"] = old_mode
    payload["approval_review_readiness"] = {"status": "ready"}
    service = StoredSettingsService(payload)
    response = await service.get_settings()
    assert response.default_mode == "hybrid"
    assert response.tool_approval_mode == ("auto_review" if old_mode == "auto_review" else "ask")
    assert response.approval_review.to_payload() == RULES
    assert response.model_dump()["approval_review_readiness"]["reason_code"] == "rules_only"
    assert service.payload == payload  # reads do not rewrite old rows


@pytest.mark.asyncio
async def test_pg_dto_revalidates_stored_review_without_coercion() -> None:
    service = StoredSettingsService({"approval_review": {"require_distinct_model": "false"}})
    with pytest.raises(ValueError, match="invalid review distinct model flag"):
        await service.get_settings()


@pytest.mark.parametrize(
    "payload",
    [
        {"default_permission_mode": "read_only"},
        {"default_permission_mode": "auto_review", "tool_approval_mode": "ask"},
        {"supported_permission_modes": ["default"]},
    ],
)
def test_existing_permission_default_guards_still_reject(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        admin.SystemSettingsRequest.model_validate({**payload, "approval_review": MODEL})
