"""HTTP shape/identity boundaries; stubs are not SQL or delivery proof."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_hub.auth.models import PermissionDenied
from agent_hub.plugins.handover_sources import SourceLimits
from agent_hub.plugins.handover_storage import Conflict, NotFound, StorageUnavailable
from tests.api.test_plugin_handovers import HID, KEY, SECRET, Case
from tests.api.test_plugin_handovers import case as shared_case

case = shared_case
REPORT_ID = "gs-" + "3" * 64
REPORT_SHA = "4" * 64


class ReviewProbe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.error: Exception | None = None

    def result(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> SimpleNamespace:
        self.calls.append((name, args, kwargs))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            report_id=REPORT_ID, report_sha256=REPORT_SHA, created_at=datetime(2026, 10, 15, tzinfo=UTC),
            report=SimpleNamespace(coverage="complete", body={"findings": [], "coverage": "complete"}),
        )

    async def scan_pending_package(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        return self.result("scan", args, kwargs)

    async def get_report(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        return self.result("get", args, kwargs)


async def call(case: Case, probe: ReviewProbe | None, *, method: str = "POST",
               query: str | None = None, content: bytes = b"signed-original-zip",
               headers: dict[str, str] | None = None) -> httpx.Response:
    app = case.app()
    app.state.plugin_review_services = probe
    path = f"{case.base}/{HID}/scan-reports"
    if method == "GET":
        path += "/" + REPORT_ID
    elif query is None:
        path += "?key_id=" + KEY
    if query is not None:
        path += "?" + query
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, headers=case.headers if headers is None else headers,
                                    content=content if method == "POST" else None)


async def test_scan_and_read_pass_authenticated_identity_and_original_bytes(case: Case) -> None:
    probe = ReviewProbe()
    scanned = await call(case, probe)
    assert scanned.status_code == 200
    assert scanned.json() == {
        "schema_version": 1, "report_id": REPORT_ID, "report_sha256": REPORT_SHA,
        "created_at": "2026-10-15T00:00:00Z", "coverage": "complete",
        "body": {"findings": [], "coverage": "complete"},
    }
    name, args, kwargs = probe.calls[0]
    assert name == "scan" and args == (case.principal, case.run_id, HID)
    assert kwargs == {"archive_bytes": b"signed-original-zip", "key_id": KEY,
                      "idempotency_key": "unit-key"}
    loaded = await call(case, probe, method="GET")
    assert loaded.status_code == 200 and loaded.json() == scanned.json()
    assert probe.calls[1] == ("get", (case.principal, case.run_id, HID, REPORT_ID), {})


@pytest.mark.parametrize("query", ["", "key_id=x&key_id=y", "key_id=x&pass=true", "key_id=bad/key"])
async def test_scan_rejects_unknown_or_ambiguous_query(case: Case, query: str) -> None:
    probe = ReviewProbe()
    response = await call(case, probe, query=query)
    assert response.status_code == 422
    assert probe.calls == []


@pytest.mark.parametrize("content,status", [(b"", 422), (b"x" * (SourceLimits().max_archive_bytes + 1), 413)], ids=["empty", "over-budget"])
async def test_scan_has_bounded_archive_before_service(case: Case, content: bytes, status: int) -> None:
    probe = ReviewProbe()
    assert (await call(case, probe, content=content)).status_code == status
    assert probe.calls == []


@pytest.mark.parametrize("error,status", [
    (PermissionDenied(SECRET), 403), (Conflict(SECRET), 409), (NotFound(SECRET), 404),
    (StorageUnavailable(SECRET), 503), (ValueError(SECRET), 422), (RuntimeError(SECRET), 500),
])
async def test_scan_errors_do_not_return_source_or_backend_details(case: Case, error: Exception, status: int) -> None:
    probe = ReviewProbe()
    probe.error = error
    response = await call(case, probe)
    assert response.status_code == status
    assert SECRET not in response.text


async def test_unavailable_review_service_fails_closed(case: Case) -> None:
    assert (await call(case, None)).status_code == 503


async def test_scan_requires_bearer_before_service_use(case: Case) -> None:
    probe = ReviewProbe()
    assert (await call(case, probe, headers={"Idempotency-Key": "unit-key"})).status_code == 401
    assert probe.calls == []
