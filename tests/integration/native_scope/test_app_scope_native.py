"""Real SQL/login/app selection and nonroot namespaces, not generated delivery proof.

Only an explicitly owned disposable database may be used. Redis notifications are
a no-op double; plugin/MCP builders, API authorization and child processes are real.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import sysconfig
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent_hub.app import create_app
from agent_hub.auth.passwords import PasswordService
from agent_hub.auth.rate_limit import RateLimitDecision
from agent_hub.auth.service import AuthService
from agent_hub.auth.tokens import AccessTokenService
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.config.service import ConfigService
from agent_hub.db.base import Base
from agent_hub.db.models import TenantRow, UserRow
from agent_hub.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("AGENT_HUB_SCOPE_NATIVE_ISOLATED") != "1" or sys.platform != "linux",
        reason="owned native Linux SQL scope opt-in required",
    ),
]
_PROGRAM = str(Path(sys.executable).resolve())
_INSPECT = """import json, os, pathlib
here = pathlib.Path.cwd()
result = {'uid': os.getuid(), 'scope': (here / 'scope').read_text(),
          'secret_absent': 'SCOPE_SYNTHETIC_SECRET' not in os.environ}
for key, path in (('private_denied', pathlib.Path(PRIVATE)),
                  ('sibling_denied', pathlib.Path(SIBLING) / 'scope')):
    try:
        path.read_bytes()
    except OSError:
        result[key] = True
    else:
        result[key] = False
try:
    (here / 'unauthorized').write_text('bad')
except OSError:
    result['readonly'] = True
else:
    result['readonly'] = False
print(json.dumps(result))
"""
_MCP = """import json, pathlib, sys
def respond(message, result):
    raw = json.dumps({'jsonrpc':'2.0', 'id':message['id'], 'result':result}).encode()
    sys.stdout.buffer.write(f'Content-Length: {len(raw)}\\r\\n\\r\\n'.encode() + raw)
    sys.stdout.buffer.flush()
while True:
    line = sys.stdin.buffer.readline()
    if not line:
        break
    length = int(line.split(b':', 1)[1])
    while sys.stdin.buffer.readline() not in (b'\\r\\n', b'\\n', b''):
        pass
    message = json.loads(sys.stdin.buffer.read(length))
    method = message.get('method')
    if 'id' not in message:
        continue
    if method == 'tools/list':
        result = {'tools':[{'name':'inspect','description':'Read scope marker',
                           'inputSchema':{'type':'object'}}]}
    elif method == 'tools/call':
        result = {'content':[{'type':'text','text':json.dumps(
            {'scope':(pathlib.Path.cwd() / 'scope').read_text()})}]}
    else:
        result = {'protocolVersion':'2025-06-18','capabilities':{'tools':{}},
                  'serverInfo':{'name':'scoped','version':'1.0.0'}}
    respond(message, result)
"""


class _Notifications:
    async def xadd(self, *args: Any, **kwargs: Any) -> str:
        return "1-0"

    async def publish(self, *args: Any, **kwargs: Any) -> int:
        return 0


class _Admission:
    async def check(self, endpoint: str, client_ip: str) -> RateLimitDecision:
        assert endpoint == "login" and client_ip
        return RateLimitDecision(True)


async def _ready() -> None:
    return None


@dataclass(repr=False)
class _Context:
    app: Any
    client: httpx.AsyncClient
    tenants: tuple[UUID, UUID]
    users: dict[tuple[UUID, str], UUID]
    headers: dict[tuple[UUID, str], dict[str, str]]
    scopes: dict[tuple[UUID, str], Path]

    async def create_plugin(self, tenant: UUID, source: str) -> None:
        scope = self.scopes.get((tenant, source), self.scopes[(tenant, "alpha")])
        response = await self.client.post(
            "/api/v1/admin/plugins",
            headers=self.headers[(tenant, "admin")],
            json={
                "id": source,
                "name": source,
                "resource_config": {
                    "command": _PROGRAM,
                    "base_args": ["-I", "-u", str(scope / "inspect.py")],
                },
                "capabilities": [
                    {
                        "id": source + ".inspect",
                        "adapter": "local_command",
                        "permission_class": "plugin.use",
                        "sandbox_profile": "local_process",
                    }
                ],
            },
        )
        assert response.status_code == 200, response.status_code


@pytest.fixture
async def context(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[_Context]:
    assert getattr(os, "geteuid", lambda: -1)() == 10001
    database_url = os.environ["AGENT_HUB_TEST_DATABASE_URL"]
    expected = os.environ["AGENT_HUB_SCOPE_NATIVE_DATABASE"]
    marker = os.environ["AGENT_HUB_SCOPE_NATIVE_MARKER"]
    expected_oid = int(os.environ["AGENT_HUB_SCOPE_NATIVE_OID"])
    url = make_url(database_url)
    assert expected.startswith("scope_native_") and len(expected) == 45
    assert url.database == expected and url.username == "agent_hub"
    assert url.host == "127.0.0.1" and url.port == 5432 and not url.query
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            identity = (
                await connection.execute(
                    text(
                        "SELECT oid, pg_get_userbyid(datdba), shobj_description(oid,'pg_database') "
                        "FROM pg_database WHERE datname = current_database()"
                    )
                )
            ).one()
            assert tuple(identity) == (expected_oid, "agent_hub", marker)
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        tenants = uuid4(), uuid4()
        password = secrets.token_urlsafe(32)
        passwords = PasswordService()
        hashed = await passwords.hash_async(password)
        users = {(tenant, role): uuid4() for tenant in tenants for role in ("admin", "operator")}
        async with factory() as session, session.begin():
            session.add_all(
                [
                    TenantRow(id=tenant, slug="scope-" + tenant.hex, name="Scoped fixture")
                    for tenant in tenants
                ]
            )
            await session.flush()
            session.add_all(
                [
                    UserRow(
                        id=user,
                        tenant_id=tenant,
                        username=role + "-fixture",
                        role=role,
                        disabled=False,
                        password_hash=hashed,
                    )
                    for (tenant, role), user in users.items()
                ]
            )
        scopes = {
            (tenant, source): tmp_path / (tenant.hex + "-" + source)
            for tenant in tenants
            for source in ("alpha", "beta", "peer")
        }
        private = tmp_path / "private"
        private.write_text("synthetic-private", encoding="ascii")
        for (tenant, source), scope in scopes.items():
            scope.mkdir()
            (scope / "scope").write_text(str(tenant) + "/" + source, encoding="ascii")
            sibling = scopes[(tenants[1] if tenant == tenants[0] else tenants[0], source)]
            script = (
                "PRIVATE="
                + repr(str(private))
                + "\nSIBLING="
                + repr(str(sibling))
                + "\n"
                + _INSPECT
            )
            (scope / "inspect.py").write_text(script, encoding="ascii")
            (scope / "mcp.py").write_text(_MCP, encoding="ascii")
        runtime = [Path(sysconfig.get_path("stdlib")), Path("/lib64/ld-linux-x86-64.so.2")]
        runtime.extend(
            Path("/lib/x86_64-linux-gnu") / name
            for name in (
                "libpthread.so.0",
                "libdl.so.2",
                "libutil.so.1",
                "libm.so.6",
                "librt.so.1",
                "libc.so.6",
            )
        )
        policy = tmp_path / "policy.json"
        policy.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "bubblewrap_executable": "/usr/bin/bwrap",
                    "runtime_readonly_paths": [str(path) for path in runtime],
                    "native_probe_program": "/usr/bin/true",
                    "bindings": [
                        {
                            "kind": "mcp" if source == "peer" else "plugin",
                            "tenant_id": str(tenant),
                            "source_id": source,
                            "working_directory": str(scope),
                            "approved_programs": [_PROGRAM],
                        }
                        for (tenant, source), scope in scopes.items()
                    ],
                }
            ),
            encoding="ascii",
        )
        if request.node.name == "test_missing_policy_preserves_login_and_refuses_activation":
            policy.unlink()
        settings = Settings.model_validate(
            {
                "bootstrap_tenant_id": tenants[0],
                "scheduler_enabled": False,
                "execution_scope_policy_file": policy,
                "skill_store_dir": tmp_path / "skills",
                "project_workspace_dir": tmp_path / "workspaces",
                "attachment_store_dir": tmp_path / "attachments",
                "generated_artifact_dir": tmp_path / "generated",
                "plugin_package_store_dir": tmp_path / "packages",
                "plugin_package_dependency_cache_dir": tmp_path / "dependencies",
                "capability_environment_root_dir": tmp_path / "environments",
            }
        )
        auth = AuthService(
            factory, tenants[0], passwords, AccessTokenService(secrets.token_bytes(32))
        )
        application = create_app(
            settings=settings,
            session_factory=factory,
            redis_client=cast(Any, _Notifications()),
            auth_service=auth,
            config_service=ConfigService(factory),
            rate_limiter=_Admission(),
            database_probe=_ready,
            redis_probe=_ready,
        )
        async with (
            application.router.lifespan_context(application),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://isolated.test"
            ) as client,
        ):
            headers: dict[tuple[UUID, str], dict[str, str]] = {}
            for tenant, role in users:
                response = await client.post(
                    "/api/v1/auth/login",
                    json={
                        "tenant_id": str(tenant),
                        "username": role + "-fixture",
                        "password": password,
                    },
                )
                assert response.status_code == 200, response.status_code
                headers[(tenant, role)] = {
                    "Authorization": "Bearer " + response.json()["access_token"]
                }
            del password, hashed, response
            yield _Context(application, client, tenants, users, headers, scopes)
            headers.clear()
    finally:
        await engine.dispose()


async def test_real_login_plugin_scopes_and_operator_permissions(context: _Context) -> None:
    c = context
    assert c.app.state.execution_scope_policy_status == "loaded"
    for tenant, source in (
        (c.tenants[0], "alpha"),
        (c.tenants[0], "beta"),
        (c.tenants[1], "alpha"),
    ):
        await c.create_plugin(tenant, source)
        denied = await c.client.post(
            f"/api/v1/admin/plugins/{source}/start", headers=c.headers[(tenant, "operator")]
        )
        assert denied.status_code == 403
        started = await c.client.post(
            f"/api/v1/admin/plugins/{source}/start", headers=c.headers[(tenant, "admin")]
        )
        assert started.status_code == 200, started.status_code
        manifest = await c.client.get(
            "/api/v1/admin/capabilities/manifest", headers=c.headers[(tenant, "operator")]
        )
        assert manifest.status_code == 200
        item = next(
            item for item in manifest.json()["capabilities"] if item["id"] == source + ".inspect"
        )
        assert item["plugin_id"] == source and item["available"] is True
        result = await c.app.state.plugin_service.invoke(
            tenant_id=tenant,
            user_id=c.users[(tenant, "operator")],
            run_id=uuid4(),
            actor="operator-fixture",
            name=source + ".inspect",
            arguments={},
            idempotency_key=uuid4().hex,
        )
        assert result["ok"] is True, result
        inspected = json.loads(result["stdout"])
        assert inspected == {
            "uid": 10001,
            "scope": str(tenant) + "/" + source,
            "secret_absent": True,
            "private_denied": True,
            "sibling_denied": True,
            "readonly": True,
        }
    tenant = c.tenants[0]
    await c.create_plugin(tenant, "unknown")
    refused = await c.client.post(
        "/api/v1/admin/plugins/unknown/start", headers=c.headers[(tenant, "admin")]
    )
    assert refused.status_code == 409
    with pytest.raises(RuntimeCapabilityError):
        await c.app.state.plugin_service.invoke(
            tenant_id=tenant,
            user_id=c.users[(tenant, "operator")],
            run_id=uuid4(),
            actor="operator-fixture",
            name="unknown.inspect",
            arguments={},
            idempotency_key=uuid4().hex,
        )


async def test_real_login_mcp_scopes_and_failed_reconfiguration(context: _Context) -> None:
    c = context
    for tenant in c.tenants:
        body = {
            "id": "peer",
            "name": "Scoped peer",
            "transport": "stdio",
            "command": _PROGRAM,
            "args": ["-I", "-u", str(c.scopes[(tenant, "peer")] / "mcp.py")],
            "allowed_tools": ["inspect"],
            "executable_allowlist": [_PROGRAM],
        }
        response = await c.client.post(
            "/api/v1/admin/mcp", headers=c.headers[(tenant, "admin")], json=body
        )
        assert response.status_code == 200
        assert c.app.state.mcp_service.is_available(tenant, "peer.inspect")
        with pytest.raises(RuntimeCapabilityError):
            await c.app.state.mcp_service.invoke(
                tenant_id=tenant,
                user_id=c.users[(tenant, "operator")],
                run_id=uuid4(),
                actor="operator-fixture",
                name="peer.inspect",
                arguments={},
                idempotency_key=uuid4().hex,
            )
        result = await c.app.state.mcp_service.invoke(
            tenant_id=tenant,
            user_id=c.users[(tenant, "operator")],
            run_id=uuid4(),
            actor="runtime_planning",
            name="peer.inspect",
            arguments={},
            idempotency_key=uuid4().hex,
        )
        content = result["content"]
        assert isinstance(content, list) and isinstance(content[0], dict)
        assert json.loads(content[0]["text"])["scope"] == str(tenant) + "/peer"
        body["command"] = "/usr/bin/false"
        body["executable_allowlist"] = ["/usr/bin/false"]
        changed = await c.client.post(
            "/api/v1/admin/mcp", headers=c.headers[(tenant, "admin")], json=body
        )
        assert changed.status_code == 200
        assert not c.app.state.mcp_service.is_available(tenant, "peer.inspect")
        with pytest.raises(RuntimeCapabilityError):
            await c.app.state.mcp_service.invoke(
                tenant_id=tenant,
                user_id=c.users[(tenant, "operator")],
                run_id=uuid4(),
                actor="operator-fixture",
                name="peer.inspect",
                arguments={},
                idempotency_key=uuid4().hex,
            )


async def test_missing_policy_preserves_login_and_refuses_activation(context: _Context) -> None:
    c = context
    assert c.app.state.execution_scope_policy_status == "unavailable"
    await c.create_plugin(c.tenants[0], "alpha")
    response = await c.client.post(
        "/api/v1/admin/plugins/alpha/start", headers=c.headers[(c.tenants[0], "admin")]
    )
    assert response.status_code == 409
    assert not c.app.state.plugin_service.is_available(c.tenants[0], "alpha.inspect")
