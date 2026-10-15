"""Route composition only; transaction authority is tested in the review service."""
from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request

from agent_hub.api.errors import PublicAPIError
from agent_hub.api.routers import admin
from agent_hub.auth.models import AuthenticatedPrincipal, PermissionDenied, Role
from agent_hub.plugins.handover_storage import Conflict, NotFound, StorageUnavailable
from tests.unit.plugins.test_handover_admin_writes import plugin_payload

REPORT_ID = "gs-" + "3" * 64
REPORT_SHA = "4" * 64


@pytest.fixture
def context(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    plugin = admin._plugin_response_from_payload(plugin_payload(generated=False))
    legacy = SimpleNamespace(approve_plugin_package=AsyncMock(return_value=plugin))
    reviews = SimpleNamespace(approve_generated=AsyncMock(return_value=plugin))
    request = Request({"type": "http", "app": FastAPI()})
    request.app.state.plugin_review_services = reviews
    monkeypatch.setattr(admin, "_current_plugin_for_activation_check", AsyncMock(return_value=plugin))
    monkeypatch.setattr(admin, "_ensure_runtime_registered_package_can_activate", lambda *args: None)
    monkeypatch.setattr(admin, "_reload_plugin_runtime_config", AsyncMock())
    return SimpleNamespace(plugin=plugin, legacy=legacy, reviews=reviews, request=request,
                           principal=AuthenticatedPrincipal(uuid4(), uuid4(), Role.SUPER_ADMIN),
                           body=admin.PluginPackageApprovalRequest(
                               reason="Reviewed source", scan_report_id=REPORT_ID,
                               scan_report_sha256=REPORT_SHA))


async def call(context: SimpleNamespace) -> admin.PluginResourceResponse:
    return await admin.approve_plugin_package(
        "generated-tool", context.body, context.request, context.principal,
        cast(admin.AdminResourceService, context.legacy),
    )


async def test_generated_approval_uses_exact_review_transaction(context: SimpleNamespace) -> None:
    assert await call(context) is context.plugin
    context.reviews.approve_generated.assert_awaited_once_with(
        context.principal, "generated-tool", report_id=REPORT_ID,
        report_sha256=REPORT_SHA, reason="Reviewed source",
    )
    context.legacy.approve_plugin_package.assert_not_called()


async def test_non_generated_registration_keeps_legacy_approval(context: SimpleNamespace) -> None:
    context.reviews.approve_generated.return_value = None
    assert await call(context) is context.plugin
    context.legacy.approve_plugin_package.assert_awaited_once_with(
        "generated-tool", context.body, tenant_id=context.principal.tenant_id,
        actor_id=context.principal.user_id,
    )


@pytest.mark.parametrize("error,status", [
    (Conflict("private-source-detail"), 409), (NotFound("private-source-detail"), 404),
    (PermissionDenied("private-source-detail"), 403),
    (StorageUnavailable("private-source-detail"), 503),
])
async def test_review_rejection_never_falls_back_to_legacy(
    context: SimpleNamespace, error: Exception, status: int,
) -> None:
    context.reviews.approve_generated.side_effect = error
    with pytest.raises(PublicAPIError) as caught:
        await call(context)
    assert caught.value.status_code == status
    assert "private-source-detail" not in str(caught.value)
    context.legacy.approve_plugin_package.assert_not_called()


async def test_missing_review_backend_still_allows_legacy_service(context: SimpleNamespace) -> None:
    context.request.app.state.plugin_review_services = None
    assert await call(context) is context.plugin
    context.legacy.approve_plugin_package.assert_awaited_once()


async def test_ordinary_viewer_cannot_grant_source_approval(context: SimpleNamespace) -> None:
    context.principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.VIEWER)
    with pytest.raises(PublicAPIError) as caught:
        await call(context)
    assert caught.value.status_code == 403
    context.reviews.approve_generated.assert_not_called()
    context.legacy.approve_plugin_package.assert_not_called()
