"""Real login/HTTP/SQL scan-to-approval, never model generation or code execution."""
from __future__ import annotations

from typing import Any

import pytest

from tests.integration.plugins import test_handover_confirmation as sql_tests
from tests.integration.plugins import test_handover_public_http as public_tests

pytestmark = public_tests.pytestmark
sql_context = public_tests.sql_context
context = public_tests.context


async def pending(c: public_tests.Context) -> str:
    plan = await c.plan()
    confirmed = await c.confirm(plan)
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "pending"
    return f"{c.prefix}/{plan['handover_id']}/scan-reports"


async def scan(c: public_tests.Context, path: str) -> dict[str, Any]:
    response = await c.client.post(
        path, params={"key_id": sql_tests.KEY_ID}, content=c.sql.archive,
        headers={**c.installer_headers, "Idempotency-Key": "actual-review",
                 "Content-Type": "application/zip"},
    )
    assert response.status_code == 200
    report: dict[str, Any] = response.json()
    assert report["coverage"] == "complete"
    return report


def approval(report: dict[str, Any]) -> dict[str, str]:
    return {"reason": "Reviewed static source signals", "scan_report_id": report["report_id"],
            "scan_report_sha256": report["report_sha256"]}


async def test_real_login_http_scan_get_and_bound_approval(context: public_tests.Context) -> None:
    c = context
    path = await pending(c)
    report = await scan(c, path)
    assert await scan(c, path) == report
    loaded = await c.client.get(f"{path}/{report['report_id']}", headers=c.installer_headers)
    assert loaded.status_code == 200 and loaded.json() == report
    response = await c.client.post(
        f"/api/v1/admin/plugins/{sql_tests.PLUGIN_ID}/package/approve",
        headers=c.installer_headers, json=approval(report),
    )
    assert response.status_code == 200
    plugin = response.json()
    assert plugin["enabled"] is False and plugin["status"] == "disabled"
    assert plugin["package_metadata"]["approval_state"] == "approved"
    assert "_generated_review_approval" not in plugin and "_generated_handover_id" not in plugin
    listed = await c.client.get("/api/v1/admin/plugins", headers=c.installer_headers)
    assert listed.status_code == 200
    assert listed.json()[0]["package_metadata"]["approval_state"] == "approved"


@pytest.mark.parametrize("stage", ["scan", "get", "approve"])
async def test_real_creator_cannot_grant_source_execution_review(
    context: public_tests.Context, stage: str,
) -> None:
    c = context
    path = await pending(c)
    report = await scan(c, path)
    if stage == "scan":
        response = await c.client.post(
            path, params={"key_id": sql_tests.KEY_ID}, content=c.sql.archive,
            headers={**c.creator_headers, "Idempotency-Key": "creator-cannot-approve"},
        )
    elif stage == "get":
        response = await c.client.get(f"{path}/{report['report_id']}", headers=c.creator_headers)
    else:
        response = await c.client.post(
            f"/api/v1/admin/plugins/{sql_tests.PLUGIN_ID}/package/approve",
            headers=c.creator_headers, json=approval(report),
        )
    assert response.status_code == 403


@pytest.mark.parametrize("bad", ["missing", "wrong-hash", "unknown-report"])
async def test_real_http_rejects_unbound_generated_approval(
    context: public_tests.Context, bad: str,
) -> None:
    c = context
    path = await pending(c)
    report = await scan(c, path)
    body = approval(report)
    if bad == "missing":
        body = {"reason": "No report authority"}
    elif bad == "wrong-hash":
        body["scan_report_sha256"] = "0" * 64
    else:
        body["scan_report_id"] = "gs-" + "0" * 64
    response = await c.client.post(
        f"/api/v1/admin/plugins/{sql_tests.PLUGIN_ID}/package/approve",
        headers=c.installer_headers, json=body,
    )
    assert response.status_code in {404, 409}
    listed = await c.client.get("/api/v1/admin/plugins", headers=c.installer_headers)
    assert listed.status_code == 200
    assert listed.json()[0]["enabled"] is False
    assert listed.json()[0]["package_metadata"]["approval_state"] == "pending"
