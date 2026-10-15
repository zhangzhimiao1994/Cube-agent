"""HTTP boundary units; service doubles are not current-SQL or ordinary-login proof."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.requests import ClientDisconnect
from starlette.types import Message, Scope

from agent_hub.api.errors import PublicAPIError, public_error_handler
from agent_hub.auth.models import AuthenticatedPrincipal, InvalidCredentials, PermissionDenied, Role
from agent_hub.plugins.handover_snapshots import InvalidSnapshot, SnapshotUnavailable
from agent_hub.plugins.handover_sources import SourceChanged, SourceLimits
from agent_hub.plugins.handover_storage import (
    ArtifactSource,
    Conflict,
    HandoverReceipt,
    NotFound,
    StorageUnavailable,
    WorkspaceSource,
)
from agent_hub.plugins.handover_verification import PackageVerificationError

HID = "gh-" + "a" * 64
PLAN_HASH = "b" * 64
KEY = "signing-key:current"
SECRET = "SQL params token /private/snapshot must not escape"


class ServiceProbe:
    def __init__(self, receipt: HandoverReceipt) -> None:
        self.receipt = receipt
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.error: Exception | None = None

    def result(self, name: str, args: tuple[object, ...], **kwargs: object) -> HandoverReceipt:
        self.calls.append((name, args, kwargs))
        if self.error is not None:
            raise self.error
        return self.receipt

    async def create_plan(
        self, principal: AuthenticatedPrincipal, run_id: UUID,
        source: ArtifactSource | WorkspaceSource, *, idempotency_key: str, expires_at: datetime,
    ) -> HandoverReceipt:
        return self.result("create", (principal, run_id, source),
                           idempotency_key=idempotency_key, expires_at=expires_at)

    async def get_receipt(
        self, principal: AuthenticatedPrincipal, run_id: UUID, handover_id: str,
    ) -> HandoverReceipt:
        return self.result("get", (principal, run_id, handover_id))

    async def cancel_plan(
        self, principal: AuthenticatedPrincipal, run_id: UUID, handover_id: str,
        expected_version: int, plan_hash: str,
    ) -> HandoverReceipt:
        return self.result("cancel", (principal, run_id, handover_id, expected_version, plan_hash))

    async def confirm(
        self, principal: AuthenticatedPrincipal, run_id: UUID, handover_id: str, *,
        expected_version: int, plan_hash: str, confirm: bool, idempotency_key: str,
        archive_bytes: bytes, key_id: str, request: Request,
    ) -> HandoverReceipt:
        return self.result("confirm", (principal, run_id, handover_id),
                           expected_version=expected_version, plan_hash=plan_hash, confirm=confirm,
                           idempotency_key=idempotency_key, archive_bytes=archive_bytes,
                           key_id=key_id, request=request)


@dataclass
class StubAuth:
    principal: AuthenticatedPrincipal

    def authenticate_token(self, token: str) -> AuthenticatedPrincipal:
        if token != "valid-token":
            raise InvalidCredentials("invalid token")
        return self.principal


@dataclass
class Case:
    principal: AuthenticatedPrincipal
    service: ServiceProbe
    auth: StubAuth
    headers: dict[str, str]
    run_id: UUID

    @property
    def base(self) -> str:
        return f"/api/v1/runs/{self.run_id}/plugin-handovers"

    def app(self) -> FastAPI:
        name = "agent_hub.api.routers.plugin_handovers"
        assert importlib.util.find_spec(name) is not None, "handover router is not implemented"
        module = importlib.import_module(name)
        app = FastAPI()
        app.add_exception_handler(PublicAPIError, public_error_handler)
        app.include_router(module.router)
        app.state.auth_service = self.auth
        app.state.plugin_handover_services = self.service
        return app

    def create_body(self) -> dict[str, Any]:
        return {
            "source": {"kind": "artifact", "artifact_id": str(uuid4()).upper()},
            "expires_at": self.service.receipt.expires_at.isoformat(),
        }


@pytest.fixture
def case() -> Case:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), Role.VIEWER)
    run_id, now = uuid4(), datetime.now(UTC)
    receipt = HandoverReceipt(
        handover_id=HID, run_id=run_id, creator_id=principal.user_id, installer_id=None,
        version=1, status="planned", created_at=now, updated_at=now,
        expires_at=now + timedelta(hours=1), source=ArtifactSource(artifact_id=str(uuid4())),
        source_manifest_sha256="c" * 64, plan_hash=PLAN_HASH, plugin_id="generated-tool",
        plugin_version="1.0.0", permissions=(), risk_summary=("approval_required",),
        signature_payload_sha256=None, archive_sha256=None, key_id=None,
        receipt_id=None, audit_id=None, committed_at=None, result=None,
    )
    return Case(principal, ServiceProbe(receipt), StubAuth(principal),
                {"Authorization": "Bearer valid-token", "Idempotency-Key": "unit-key"}, run_id)


def confirm_query() -> dict[str, str]:
    return {"expected_version": "1", "plan_hash": PLAN_HASH, "confirm": "true", "key_id": KEY}


def test_openapi_declares_bearer_and_route_error_contract(case: Case) -> None:
    schema = case.app().openapi()
    prefix = "/api/v1/runs/{run_id}/plugin-handovers"
    operations = [(prefix, "post"), (prefix + "/{handover_id}", "get"),
                  (prefix + "/{handover_id}/cancel", "post"),
                  (prefix + "/{handover_id}/confirm", "post")]
    for path, method in operations:
        operation = schema["paths"][path][method]
        assert operation["security"] == [{"BearerAuth": []}]
        for status in (401, 403, 404, 405, 409, 413, 422, 500, 503):
            assert str(status) in operation["responses"]
        assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/HandoverReceipt"}
    confirm = schema["paths"][operations[-1][0]]["post"]
    assert "requestBody" not in confirm
    assert {p["name"] for p in confirm["parameters"] if p["in"] == "query"} == {
        "expected_version", "plan_hash", "confirm", "key_id"}


async def call(case: Case, operation: str, *, app: FastAPI | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=case.app() if app is None else app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        if operation == "create":
            return await client.post(case.base, headers=case.headers, json=case.create_body())
        if operation == "get":
            return await client.get(case.base + "/" + HID, headers=case.headers)
        if operation == "cancel":
            return await client.post(case.base + "/" + HID + "/cancel", headers=case.headers,
                                     json={"expected_version": 1, "plan_hash": PLAN_HASH})
        return await client.post(case.base + "/" + HID + "/confirm", headers=case.headers,
                                 params=confirm_query(), content=b"PK-unit-archive")


@pytest.mark.parametrize("operation", ["create", "get", "cancel", "confirm"])
async def test_four_routes_pass_token_identity_without_role_authorization(
    case: Case, operation: str,
) -> None:
    response = await call(case, operation)
    assert response.status_code == 200
    assert response.json() == case.service.receipt.model_dump(mode="json")
    name, args, kwargs = case.service.calls[0]
    assert name == operation and args[:2] == (case.principal, case.run_id)
    if operation == "create":
        assert type(args[2]) is ArtifactSource
        assert args[2].artifact_id == str(UUID(args[2].artifact_id))
        assert kwargs["idempotency_key"] == "unit-key"
        assert kwargs["expires_at"] == case.service.receipt.expires_at
    elif operation == "cancel":
        assert args[2:] == (HID, 1, PLAN_HASH) and not kwargs
    elif operation == "confirm":
        assert kwargs["confirm"] is True and kwargs["expected_version"] == 1
        assert kwargs["archive_bytes"] == b"PK-unit-archive" and kwargs["key_id"] == KEY
        request = kwargs["request"]
        assert isinstance(request, Request)
        assert request.app.state.plugin_handover_services is case.service


async def test_workspace_wire_list_becomes_checked_sorted_tuple(case: Case) -> None:
    body = case.create_body()
    body["source"] = {"kind": "workspace", "project_id": "project", "session_id": "session",
                      "paths": ["plugin.json", "main.py"]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=case.app()),
                                base_url="http://test") as client:
        response = await client.post(case.base, headers=case.headers, json=body)
    assert response.status_code == 200
    assert case.service.calls[0][1][2] == WorkspaceSource(
        project_id="project", session_id="session", paths=("main.py", "plugin.json"))


@pytest.mark.parametrize("operation", ["create", "get", "cancel", "confirm"])
async def test_missing_state_is_503_without_fallback(case: Case, operation: str) -> None:
    app = case.app()
    del app.state.plugin_handover_services
    response = await call(case, operation, app=app)
    assert response.status_code == 503 and not case.service.calls


@pytest.mark.parametrize("authorization", [None, "Basic abc", "Bearer invalid"])
async def test_real_bearer_dependency_rejects_bad_auth(
    case: Case, authorization: str | None,
) -> None:
    case.headers.pop("Authorization")
    if authorization is not None:
        case.headers["Authorization"] = authorization
    response = await call(case, "get")
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
    assert not case.service.calls


@pytest.mark.parametrize("error,status", [
    (PermissionDenied, 403), (NotFound, 404), (Conflict, 409), (SourceChanged, 409),
    (ValueError, 422), (PackageVerificationError, 422), (StorageUnavailable, 503),
    (SnapshotUnavailable, 503), (InvalidSnapshot, 503), (RuntimeError, 500),
])
@pytest.mark.parametrize("operation", ["create", "get", "cancel", "confirm"])
async def test_error_mapping_never_renders_backend_exception(
    case: Case, operation: str, error: type[Exception], status: int,
) -> None:
    def forbidden(self: Exception) -> str:
        raise AssertionError("backend exception rendering is forbidden")

    protected = type("PrivateError", (error,), {"__str__": forbidden, "__repr__": forbidden})
    case.service.error = protected(SECRET)
    response = await call(case, operation)
    assert response.status_code == status
    assert set(response.json()) == {"error"}
    assert set(response.json()["error"]) == {"code", "message"}
    assert SECRET not in response.text and "PrivateError" not in response.text


@pytest.mark.parametrize("mutation", [
    "authority", "source_extra", "artifact_bool", "artifact_id", "kind", "expires_bool",
    "expires_naive", "project_bool", "paths_bool", "paths_string", "duplicate", "escape",
    "path_long", "too_many_paths",
])
async def test_create_strict_wire_schema_and_safe_validation(case: Case, mutation: str) -> None:
    body = case.create_body()
    if mutation == "authority":
        body["tenant_id"] = SECRET
    elif mutation == "source_extra":
        body["source"]["source_manifest_sha256"] = SECRET
    elif mutation.startswith("artifact"):
        body["source"]["artifact_id"] = True if mutation == "artifact_bool" else SECRET
    elif mutation == "kind":
        body["source"]["kind"] = True
    elif mutation.startswith("expires"):
        body["expires_at"] = True if mutation == "expires_bool" else "2026-10-13T12:00:00"
    else:
        body["source"] = {"kind": "workspace", "project_id": "p", "session_id": "s",
                          "paths": ["main.py", "plugin.json"]}
        if mutation == "project_bool":
            body["source"]["project_id"] = True
        else:
            body["source"]["paths"] = {
                "paths_bool": [True], "paths_string": "main.py",
                "duplicate": ["main.py", "main.py"], "escape": ["../private"],
                "path_long": ["x" * 256], "too_many_paths": [f"f{i}.py" for i in range(201)],
            }[mutation]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=case.app()),
                                base_url="http://test") as client:
        response = await client.post(case.base, headers=case.headers, json=body)
    assert response.status_code == 422 and not case.service.calls
    assert SECRET not in response.text and "input" not in response.json()["error"]


@pytest.mark.parametrize("version", [True, False, "1", 1.0, 0, -1, None])
async def test_cancel_version_is_strict_positive_int(case: Case, version: object) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=case.app()),
                                base_url="http://test") as client:
        response = await client.post(case.base + "/" + HID + "/cancel", headers=case.headers,
                                     json={"expected_version": version, "plan_hash": PLAN_HASH})
    assert response.status_code == 422 and not case.service.calls


@pytest.mark.parametrize("field,value", [
    ("expected_version", "true"), ("expected_version", "1.0"), ("expected_version", "0"),
    ("confirm", "false"), ("confirm", "1"), ("plan_hash", "B" * 64),
    ("key_id", "../private"), ("tenant_id", SECRET),
])
async def test_confirm_query_rejects_coercion_and_authority_injection(
    case: Case, field: str, value: str,
) -> None:
    query = {**confirm_query(), field: value}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=case.app()),
                                base_url="http://test") as client:
        response = await client.post(case.base + "/" + HID + "/confirm", headers=case.headers,
                                     params=query, content=b"PK-bytes")
    assert response.status_code == 422 and not case.service.calls and SECRET not in response.text


async def drive(
    case: Case, messages: list[Message | BaseException], *, content_length: str = "1",
) -> tuple[list[Message], int]:
    headers = [(key.lower().encode(), value.encode()) for key, value in case.headers.items()]
    scope: Scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": case.base + "/" + HID + "/confirm",
        "raw_path": (case.base + "/" + HID + "/confirm").encode(), "root_path": "",
        "query_string": urlencode(confirm_query()).encode(),
        "headers": [*headers, (b"content-length", content_length.encode())],
        "client": ("127.0.0.1", 1000), "server": ("test", 80),
    }
    sent: list[Message] = []
    received = 0

    async def receive() -> Message:
        nonlocal received
        received += 1
        assert messages, "router read past the upload boundary"
        item = messages.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def send(message: Message) -> None:
        sent.append(message)

    await case.app()(scope, receive, send)
    return sent, received


@pytest.mark.parametrize("size", [8, SourceLimits().max_archive_bytes])
async def test_actual_chunks_are_read_despite_false_content_length(case: Case, size: int) -> None:
    sent, received = await drive(case, [
        {"type": "http.request", "body": b"P" * (size - 1), "more_body": True},
        {"type": "http.request", "body": b"K", "more_body": False},
    ])
    assert sent[0]["status"] == 200 and received == 2
    assert case.service.calls[0][2]["archive_bytes"] == b"P" * (size - 1) + b"K"


async def test_overflow_stops_before_next_chunk_and_never_confirms(case: Case) -> None:
    sent, received = await drive(case, [
        {"type": "http.request", "body": b"x" * SourceLimits().max_archive_bytes,
         "more_body": True},
        {"type": "http.request", "body": b"overflow", "more_body": True},
        AssertionError("overflow must stop consuming the upload"),
    ])
    assert sent[0]["status"] == 413 and received == 2 and not case.service.calls


async def test_empty_archive_is_422_without_confirmation(case: Case) -> None:
    sent, _ = await drive(case, [{"type": "http.request", "body": b"", "more_body": False}])
    assert sent[0]["status"] == 422 and not case.service.calls


@pytest.mark.parametrize("ending", ["disconnect", "cancel"])
async def test_incomplete_upload_propagates_without_confirmation(case: Case, ending: str) -> None:
    error = ClientDisconnect if ending == "disconnect" else asyncio.CancelledError
    last: Message | BaseException = (
        {"type": "http.disconnect"} if ending == "disconnect" else asyncio.CancelledError()
    )
    with pytest.raises(error):
        await drive(case, [{"type": "http.request", "body": b"PK", "more_body": True}, last])
    assert not case.service.calls
