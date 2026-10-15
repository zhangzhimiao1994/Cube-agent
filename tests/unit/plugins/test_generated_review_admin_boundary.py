from __future__ import annotations

import base64
import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_hub.api.errors import PublicAPIError
from agent_hub.api.routers.admin import (
    PersistentAdminResourceService,
    PluginPackageApprovalRequest,
    _checked_plugin_write_payload,
    _plugin_response_from_payload,
    _PluginWritePayload,
)
from agent_hub.db.models import AdminResourceRow
from agent_hub.plugins.handover_storage import Conflict
from tests.unit.plugins.test_handover_admin_writes import (
    AdminContext,
    plugin_payload,
)
from tests.unit.plugins.test_handover_admin_writes import (
    context as shared_context,
)

PROOF = "_generated_review_approval"
REPORT_ID = "gs-" + "3" * 64
REPORT_SHA = "4" * 64
context = shared_context


def test_package_approval_accepts_an_exact_report_reference() -> None:
    body = PluginPackageApprovalRequest.model_validate({
        "reason": "Reviewed", "scan_report_id": REPORT_ID, "scan_report_sha256": REPORT_SHA,
    })
    assert body.scan_report_id == REPORT_ID
    assert body.scan_report_sha256 == REPORT_SHA


@pytest.mark.parametrize("body", [
    {"scan_report_id": REPORT_ID},
    {"scan_report_sha256": REPORT_SHA},
    {"scan_report_id": "untrusted", "scan_report_sha256": REPORT_SHA},
    {"scan_report_id": REPORT_ID, "scan_report_sha256": "not-a-digest"},
    {"scan_report_id": REPORT_ID, "scan_report_sha256": REPORT_SHA, "scan_passed": True},
])
def test_package_approval_rejects_partial_or_caller_attested_reports(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PluginPackageApprovalRequest.model_validate(body)


def test_legacy_approval_and_rejection_body_remain_compatible() -> None:
    assert PluginPackageApprovalRequest(reason="Legacy").reason == "Legacy"


def test_generated_review_private_authority_is_not_a_public_dto_field() -> None:
    payload = plugin_payload()
    payload[PROOF] = "ga-review-" + "5" * 64
    value = _plugin_response_from_payload(payload).model_dump(mode="json")
    assert PROOF not in value
    assert "_generated_handover_id" not in value


def test_lifecycle_write_preserves_the_existing_private_review_pointer() -> None:
    current = plugin_payload()
    current[PROOF] = "ga-review-" + "5" * 64
    public = {key: value for key, value in current.items() if not key.startswith("_generated_")}
    changed = _checked_plugin_write_payload(current, public, state_snapshot=deepcopy(current))
    assert changed[PROOF] == current[PROOF]
    assert changed["_generated_handover_id"] == current["_generated_handover_id"]
    assert current["enabled"] is False


@pytest.mark.parametrize("value", ["", None, "ga-review-" + "6" * 64])
def test_snapshot_write_cannot_replace_a_committed_review_pointer(value: object) -> None:
    current = plugin_payload()
    current[PROOF] = "ga-review-" + "5" * 64
    forged = deepcopy(current)
    forged[PROOF] = value
    with pytest.raises(PublicAPIError) as caught:
        _checked_plugin_write_payload(current, forged, state_snapshot=deepcopy(current))
    assert caught.value.status_code == 409


@pytest.mark.parametrize("value", ["", None, "ga-review-" + "5" * 64])
def test_ordinary_write_cannot_forge_even_an_empty_review_pointer(value: object) -> None:
    forged = plugin_payload(generated=False)
    forged[PROOF] = value
    with pytest.raises(PublicAPIError) as caught:
        _checked_plugin_write_payload({}, forged)
    assert caught.value.status_code == 409


def test_legacy_public_payload_roundtrip_does_not_gain_a_review_pointer() -> None:
    payload = plugin_payload(generated=False)
    assert _checked_plugin_write_payload({}, payload) == payload
    assert PROOF not in _plugin_response_from_payload(payload).model_dump(mode="json")


@pytest.mark.parametrize("referenced", [False, True])
async def test_generated_approval_cannot_bypass_the_review_transaction(
    context: AdminContext, referenced: bool,
) -> None:
    service, db, _ = context
    db.put(plugin_payload())
    body = PluginPackageApprovalRequest.model_validate({
        "reason": "Must use the generated review transaction",
        **({"scan_report_id": REPORT_ID, "scan_report_sha256": REPORT_SHA}
           if referenced else {}),
    })
    with pytest.raises(PublicAPIError) as caught:
        await service.approve_plugin_package("generated-tool", body)
    assert caught.value.status_code == 409
    assert not any(event[0] in {"write", "audit"} for event in db.events)


async def test_legacy_package_approval_does_not_require_generated_review(context: AdminContext) -> None:
    service, db, _ = context
    db.put(plugin_payload(generated=False))
    result = await service.approve_plugin_package(
        "generated-tool", PluginPackageApprovalRequest(reason="Legacy trusted upload"),
    )
    assert result.package_metadata is not None
    assert result.package_metadata.approval_state == "approved"


async def test_generated_rejection_remains_available_without_a_scan(context: AdminContext) -> None:
    service, db, _ = context
    db.put(plugin_payload())
    result = await service.reject_plugin_package(
        "generated-tool", PluginPackageApprovalRequest(reason="Do not activate"),
    )
    assert result.package_metadata is not None
    assert result.package_metadata.approval_state == "rejected"


def test_generated_review_validator_survives_principal_scoping(context: AdminContext) -> None:
    original, _, tenant = context

    async def validate(session: AsyncSession, *, plugin_row: AdminResourceRow) -> bool:
        return True

    service = PersistentAdminResourceService(
        config_service=original._config_service, secret_service=original._secret_service,
        tenant_id=tenant, actor_id=original._actor_id,
        session_factory=original._session_factory,
        generated_plugin_approval_validator=validate,
    )
    scoped = service.for_principal(tenant, uuid4())
    assert scoped._generated_plugin_approval_validator is validate


@pytest.mark.parametrize("action", ["enable", "start", "reload"])
async def test_generated_activation_write_requires_configured_review_validator(
    context: AdminContext, action: str,
) -> None:
    service, db, tenant = context
    current = plugin_payload()
    db.put(current)
    public = _plugin_response_from_payload(current).model_dump(mode="json")
    public.update(enabled=True, status="running", health="healthy")
    with pytest.raises(PublicAPIError) as caught:
        await service._write_plugin_payload_with_audit(
            "generated-tool", _PluginWritePayload(public, state_snapshot=deepcopy(current)),
            action=f"plugin.{action}", details={"id": "generated-tool"},
            tenant_id=tenant, actor_id=service._actor_id,
        )
    assert caught.value.status_code == 409
    assert not any(event[0] in {"write", "audit"} for event in db.events)


async def test_stop_is_still_allowed_when_generated_review_is_unavailable(context: AdminContext) -> None:
    service, db, tenant = context
    current = plugin_payload()
    db.put(current)
    public = _plugin_response_from_payload(current).model_dump(mode="json")
    assert await service._write_plugin_payload_with_audit(
        "generated-tool", _PluginWritePayload(public, state_snapshot=deepcopy(current)),
        action="plugin.stop", details={"id": "generated-tool"},
        tenant_id=tenant, actor_id=service._actor_id,
    )


@pytest.mark.parametrize("single", [False, True])
async def test_bare_generated_approval_is_not_advertised_as_active(
    context: AdminContext, single: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, db, _ = context
    current = plugin_payload(enabled=True)
    current["package_metadata"]["approval_state"] = "approved"
    db.put(current)
    if not single:
        async def resources(kind: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [deepcopy(current)] if kind == "plugin" else []
        monkeypatch.setattr(service, "_list_admin_payloads", resources)
    observed = await service._plugin_response("generated-tool") if single else (await service.list_plugins())[0]
    assert observed is not None
    assert observed.enabled is False
    assert observed.status == "disabled"
    assert observed.package_metadata is not None
    assert observed.package_metadata.approval_state == "pending"
    assert db.plugin() == current


async def test_legacy_read_projection_preserves_its_existing_approval(context: AdminContext) -> None:
    service, db, _ = context
    current = plugin_payload(generated=False, enabled=True)
    current["package_metadata"]["approval_state"] = "approved"
    db.put(current)
    observed = await service._plugin_response("generated-tool")
    assert observed is not None and observed.enabled is True
    assert observed.package_metadata is not None
    assert observed.package_metadata.approval_state == "approved"


async def test_invocation_scope_bool_stub_cannot_replace_the_generated_proof(context: AdminContext) -> None:
    service, db, tenant = context
    current = plugin_payload(enabled=True)
    current.update(status="running", health="healthy")
    current["package_metadata"]["approval_state"] = "approved"
    db.put(current)
    calls: list[object] = []

    async def validate(session: AsyncSession, *, plugin_row: AdminResourceRow) -> bool:
        assert session is cast(AsyncSession, db) and db.active
        assert plugin_row.payload == current
        calls.append(plugin_row.id)
        return True

    service._generated_plugin_approval_validator = validate
    cached = _plugin_response_from_payload(current)
    with pytest.raises(PublicAPIError):
        async with service.plugin_invocation_scope(cached, tenant_id=tenant):
            pytest.fail("A bool stub cannot supply generated execution authority")
    assert len(calls) == 1
    assert not db.active
    assert db.plugin() == current
    assert not any(event[0] in {"write", "audit"} for event in db.events)


@pytest.mark.parametrize("changed", ["configuration", "disabled", "deleted", "proof", "no-validator"])
async def test_invocation_scope_refuses_a_stale_or_unreviewed_generated_cache(
    context: AdminContext, changed: str,
) -> None:
    service, db, tenant = context
    current = plugin_payload(enabled=True)
    current.update(status="running", health="healthy")
    current["package_metadata"]["approval_state"] = "approved"
    cached = _plugin_response_from_payload(current)
    changed_payload = deepcopy(current)
    if changed == "configuration":
        changed_payload["description"] = "Changed after cache load"
    elif changed == "disabled":
        changed_payload["enabled"] = False
    db.put(changed_payload)
    if changed == "deleted":
        db.rows.clear()

    async def validate(session: AsyncSession, *, plugin_row: AdminResourceRow) -> bool:
        if changed == "proof":
            raise Conflict("review revoked")
        return True

    service._generated_plugin_approval_validator = None if changed == "no-validator" else validate
    entered = False
    with pytest.raises(PublicAPIError):
        async with service.plugin_invocation_scope(cached, tenant_id=tenant):
            entered = True
    assert entered is False
    assert not db.active


async def test_invocation_scope_cannot_read_another_tenants_registration(context: AdminContext) -> None:
    service, db, _ = context
    current = plugin_payload(enabled=True)
    db.put(current)
    with pytest.raises(PublicAPIError):
        async with service.plugin_invocation_scope(_plugin_response_from_payload(current), tenant_id=uuid4()):
            pytest.fail("foreign registration admitted")
    assert not db.active


@pytest.mark.parametrize("key_state", ["live", "missing", "revoked", "changed", "expired"])
async def test_invocation_scope_rechecks_signing_trust_for_legacy_packages(
    context: AdminContext, key_state: str,
) -> None:
    service, db, tenant = context
    public = b"k" * 32
    current = plugin_payload(generated=False, enabled=True)
    current.update(status="running", health="healthy")
    current["package_metadata"].update(
        signature={"algorithm": "ed25519", "key_id": "test-key", "value": "A" * 86},
        signature_verification="verified", verified_public_key_sha256=hashlib.sha256(public).hexdigest(),
        approval_state="approved",
    )
    db.put(current)
    if key_state != "missing":
        db.put({
            "key_id": "test-key", "algorithm": "ed25519", "trusted": key_state != "revoked",
            "public_key": base64.urlsafe_b64encode(b"x" * 32 if key_state == "changed" else public).rstrip(b"=").decode(),
            "not_after": (datetime.now(UTC) - timedelta(seconds=1)).isoformat() if key_state == "expired" else None,
        }, kind="plugin_signing_key", resource_id="test-key")
    cached = _plugin_response_from_payload(current)
    if key_state == "live":
        async with service.plugin_invocation_scope(cached, tenant_id=tenant) as authority:
            assert db.active
            assert authority is None
    else:
        with pytest.raises(PublicAPIError):
            async with service.plugin_invocation_scope(cached, tenant_id=tenant):
                pytest.fail("stale signing trust admitted")
