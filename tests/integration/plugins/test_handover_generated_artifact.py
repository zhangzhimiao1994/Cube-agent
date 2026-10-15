"""Real login/SQL bridge for published generated ZIPs; no model or source execution.

Reuse the explicit isolated-DB opt-in and ordinary creator/admin login fixtures.
The published tool_result UUID is distinct from the generated file UUID, as in
Crew project.generate_zip and direct content.workspace_delivery producers.
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, update

from agent_hub.auth.models import Role
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.db.models import AdminResourceRow, RunArtifactRow, RunRow, UserRow
from agent_hub.files.generated import ZIP_MIME_TYPE, GeneratedFileStore
from agent_hub.plugins.handover_grants import SourceGrant
from agent_hub.runtime.contracts import Artifact, JsonValue
from tests.integration.plugins import test_handover_confirmation as sql_tests
from tests.integration.plugins import test_handover_public_http as http_tests

pytestmark = sql_tests.pytestmark
sql_context = http_tests.sql_context
context = http_tests.context

Shape = Literal["crew", "direct"]


@dataclass(frozen=True)
class GeneratedCase:
    http: http_tests.Context
    shape: Shape
    published: Artifact
    file_id: UUID


async def _publish(
    c: http_tests.Context, shape: Shape, *, storage_run_id: UUID | None = None
) -> GeneratedCase:
    run_id = c.sql.run_id if storage_run_id is None else storage_run_id
    record = await c.sql.repository.get(c.sql.creator.tenant_id, run_id)
    async with c.sql.factory() as session, session.begin():
        await session.execute(
            update(RunRow)
            .where(RunRow.tenant_id == c.sql.creator.tenant_id, RunRow.id == run_id)
            .values(
                routing_decision={
                    **(record.routing_decision or {}),
                    "sandbox_profile": "workspace_write",
                    "requested_permissions": ["workspace.read", "workspace.write"],
                }
            )
        )
    owned = c.sql.package_root.parent
    # Match the HTTP app's Settings, rather than the SQL service fixture's root.
    generated_root = owned / "generated"
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=owned / "skills",
        generated_artifact_dir=generated_root,
        project_workspace_dir=owned / "workspaces",
        run_repository=c.sql.repository,
        skill_sandboxes={},
    )
    files = {**sql_tests.MEMBERS, "plugin.json": json.dumps(sql_tests._manifest()).encode()}
    arguments: dict[str, JsonValue] = {
        "title": "Generated plugin source",
        "filename": "plugin.zip",
        "presentation": "final_attachment",
    }
    if shape == "crew":
        arguments["files"] = {name: data.decode() for name, data in files.items()}
    result = await gateway.execute(
        tenant_id=c.sql.creator.tenant_id,
        run_id=run_id,
        actor="main_agent",
        name="project.generate_zip" if shape == "crew" else "workspace.bundle",
        arguments=arguments,
        idempotency_key="generated-artifact-bridge",
    )
    inner_id = result["artifact_id"]
    assert isinstance(inner_id, str)
    file_id = UUID(inner_id)
    assert file_id != c.sql.artifact_id
    file, metadata = result["file"], result["metadata"]
    assert isinstance(file, Mapping) and isinstance(metadata, Mapping)
    content: dict[str, JsonValue]
    if shape == "crew":
        content = {
            "agent_id": "builder",
            "tool_name": "project.generate_zip",
            "result": result,
            "arguments_sha256": hashlib.sha256(
                json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    else:
        content = {
            "workspace_delivery": {**result, "artifact_origin": "incremental_workspace_delivery"},
            "summary": "Generated plugin source",
            "artifact_origin": "incremental_workspace_delivery",
        }
    published = Artifact(
        id=c.sql.artifact_id,
        type="tool_result",
        producer="builder" if shape == "crew" else "main",
        content=content,
    )
    async with c.sql.factory() as session, session.begin():
        row = await session.scalar(
            select(RunArtifactRow).where(
                RunArtifactRow.tenant_id == c.sql.creator.tenant_id,
                RunArtifactRow.run_id == c.sql.run_id,
                RunArtifactRow.id == published.id,
            )
        )
        assert row is not None
        row.type = published.type
        row.producer = published.producer
        row.content_sha256 = published.content_sha256
        row.payload = published.to_payload()
    raw = await c.sql.repository.raw_artifacts(c.sql.creator.tenant_id, c.sql.run_id)
    assert raw == (published.to_payload(),)
    assert file["artifact_id"] == result["artifact_id"] == metadata["artifact_id"] == str(file_id)
    assert "storage_key" not in file
    assert metadata["mime_type"] == file["mime_type"] == ZIP_MIME_TYPE
    assert metadata["storage_key"] == (
        f"{c.sql.creator.tenant_id}/{storage_run_id or c.sql.run_id}/{file_id}/plugin.zip"
    )
    key = metadata["storage_key"]
    assert isinstance(key, str)
    data = (
        GeneratedFileStore(generated_root)
        .resolve_for(
            c.sql.creator.tenant_id,
            run_id,
            file_id,
            key,
        )
        .read_bytes()
    )
    assert file["size_bytes"] == len(data) and file["sha256"] == hashlib.sha256(data).hexdigest()
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        assert {name: zipped.read(name) for name in zipped.namelist()} == files
    return GeneratedCase(c, shape, published, file_id)


@pytest.fixture(params=["crew", "direct"])
async def generated_case(
    context: http_tests.Context,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> GeneratedCase:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    shape: Shape = "crew" if request.param == "crew" else "direct"
    return await _publish(context, shape)


async def _select(c: http_tests.Context, artifact_id: UUID) -> httpx.Response:
    return await c.client.post(
        c.prefix,
        headers={**c.creator_headers, "Idempotency-Key": "generated-artifact-selection"},
        json={
            "source": {"kind": "artifact", "artifact_id": str(artifact_id)},
            "expires_at": (c.sql.clock[0] + timedelta(hours=1)).isoformat(),
        },
    )


async def test_generated_publication_admin_confirmation_is_pending(
    generated_case: GeneratedCase,
) -> None:
    case, c = generated_case, generated_case.http
    assert c.sql.creator.role is Role.OPERATOR and c.sql.installer.role is Role.ADMIN
    before = await c.sql.rows()
    plan = await c.plan()
    assert plan["source"] == {"kind": "artifact", "artifact_id": str(case.published.id)}
    async with c.sql.factory() as session:
        row = await session.scalar(
            select(AdminResourceRow).where(
                AdminResourceRow.tenant_id == c.sql.creator.tenant_id,
                AdminResourceRow.resource_id == plan["source_grant_id"],
                AdminResourceRow.kind == "plugin_handover",
            )
        )
        assert row is not None
        grant = SourceGrant.model_validate_json(json.dumps(row.payload))
    assert grant.state == "active" and grant.binding.artifact is not None
    assert grant.binding.artifact.artifact_id == case.published.id
    assert grant.binding.artifact.content_sha256 == case.published.content_sha256
    assert grant.binding.creator_id == c.sql.creator.user_id
    snapshot = c.sql.saved(grant).snapshot
    assert {item.path: item.content for item in snapshot.files} == {
        **sql_tests.MEMBERS,
        "plugin.json": json.dumps(sql_tests._manifest()).encode(),
    }
    response = await c.confirm(plan)
    assert response.status_code == 200
    pending = response.json()
    assert pending["status"] == "pending" and pending["version"] == 2
    assert pending["creator_id"] == str(c.sql.creator.user_id)
    assert pending["installer_id"] == str(c.sql.installer.user_id)
    assert pending["result"] == "blocked_pending_approval"
    rows = await c.sql.rows()
    assert len(rows) == len(before) + 4
    assert all(row in rows for row in before)
    plugin = json.loads(next(row[3] for row in rows if row[1] == "plugin"))
    assert plugin["enabled"] is False and plugin["status"] == "disabled"
    assert plugin["package_metadata"]["approval_state"] == "pending"
    assert plugin["package_metadata"]["activation_state"] == "blocked_pending_approval"
    consumed = SourceGrant.model_validate_json(
        next(row[3] for row in rows if row[2] == grant.grant_id)
    )
    assert consumed.state == "consumed" and consumed.receipt_id == pending["receipt_id"]
    assert consumed.consumed_by == c.sql.installer.user_id
    c.sql.assert_package()


async def test_nested_file_id_is_not_publication(generated_case: GeneratedCase) -> None:
    case, c = generated_case, generated_case.http
    before = await c.sql.rows()
    response = await _select(c, case.file_id)
    assert response.status_code == 403
    assert await c.sql.rows() == before
    assert not c.sql.canonical.exists()


async def test_ordinary_creator_downloads_the_published_source_zip(
    generated_case: GeneratedCase,
) -> None:
    case, c = generated_case, generated_case.http
    response = await c.client.get(
        f"/api/v1/runs/{c.sql.run_id}/artifacts/{case.published.id}/download",
        headers=c.creator_headers,
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == ZIP_MIME_TYPE
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert {name: archive.read(name) for name in archive.namelist()} == {
            **sql_tests.MEMBERS,
            "plugin.json": json.dumps(sql_tests._manifest()).encode(),
        }


async def test_cross_run_storage_key_is_rejected(generated_case: GeneratedCase) -> None:
    case, c = generated_case, generated_case.http
    other_run = uuid4()
    async with c.sql.factory() as session, session.begin():
        session.add(
            RunRow(
                id=other_run,
                tenant_id=c.sql.creator.tenant_id,
                actor_id=c.sql.creator.user_id,
                actor_role="operator",
                request="same creator sibling run; no execution",
                status="completed",
                version=1,
                routing_decision={"project_id": "project", "workspace_session_id": "session"},
            )
        )
    rebound = await _publish(c, case.shape, storage_run_id=other_run)
    before = await c.sql.rows()
    response = await _select(c, rebound.published.id)
    assert response.status_code == 422
    assert await c.sql.rows() == before
    assert not c.sql.canonical.exists()


async def test_ordinary_creator_cannot_confirm_generated_publication(
    generated_case: GeneratedCase,
) -> None:
    c = generated_case.http
    plan = await c.plan()
    before = await c.sql.rows()
    response = await c.confirm(plan, creator=True)
    assert response.status_code == 403
    assert await c.sql.rows() == before
    assert not c.sql.canonical.exists()
    async with c.sql.factory() as session:
        creator_role = await session.scalar(
            select(UserRow.role).where(UserRow.id == c.sql.creator.user_id)
        )
    assert creator_role == "operator"
