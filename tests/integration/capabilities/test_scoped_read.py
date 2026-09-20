from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_hub.capabilities.defaults import build_runtime_capability_stack
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.db.session import build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.workspace import workspace_selection


@pytest.mark.parametrize("tool", ("workspace_read", "workspace.read", "read_context"))
async def test_persisted_run_controls_file_scope_and_permission_revocation(
    database_url: str, tmp_path: Path, tool: str
) -> None:
    database = build_database(database_url)
    repository = RunRepository(database.session_factory)
    tenant, other_tenant = uuid4(), uuid4()
    attachment, unattached = f"att_{uuid4().hex}", f"att_{uuid4().hex}"
    attachments = tmp_path / "attachments"
    for owner in (tenant, other_tenant):
        directory = attachments / str(owner)
        directory.mkdir(parents=True)
        (directory / f"{attachment}.bin").write_text("authorized" if owner == tenant else "foreign")
        (directory / f"{unattached}.bin").write_text("unattached")
    project_root = tmp_path / "projects"
    store = ProjectWorkspaceStore(project_root)
    store.write_bytes(tenant, "project", "session", "AGENTS.md", b"project rules", "text/plain")
    store.write_bytes(tenant, "project", "other", "AGENTS.md", b"other rules", "text/plain")
    routing = workspace_selection(
        project_id="project", session_id="session", sandbox_profile="read_only"
    ).routing_payload()
    routing["attachment_ids"] = [attachment]
    run_id: UUID | None = None
    try:
        record = await repository.create_run(
            tenant_id=tenant, actor_id=uuid4(), request="Read project rules",
            mode=TaskMode.DISPATCH, status=RunStatus.RUNNING, idempotency_key=None,
            routing_decision=routing, enqueue=False,
        )
        run_id = record.id
        gateway = build_runtime_capability_stack(
            tenant_id=tenant, run_repository=repository, skill_store_dir=tmp_path / "skills",
            workspace_root=attachments, project_workspace_dir=project_root,
        ).runtime_gateway

        async def read(path: str, *, owner: UUID = tenant, run: UUID = record.id) -> str:
            result = await gateway.execute(
                tenant_id=owner, run_id=run, actor="worker", name=tool,
                arguments={"path": path, "workspace_session_id": "other"},
                idempotency_key=f"scope-{uuid4().hex}",
            )
            text = result["text"]
            assert isinstance(text, str)
            return text

        for path in (
            f"{other_tenant}/{attachment}.bin", f"{tenant}/{unattached}.bin",
            "../other/AGENTS.md", "projects/project/sessions/other/AGENTS.md",
        ):
            with pytest.raises(RuntimeCapabilityError):
                await read(path)
        with pytest.raises(RuntimeCapabilityError):
            await read("AGENTS.md", owner=other_tenant)
        with pytest.raises(RuntimeCapabilityError):
            await read("AGENTS.md", run=uuid4())
        assert await read("AGENTS.md") == "project rules"
        assert await read(f"{tenant}/{attachment}.bin") == "authorized"

        # The same gateway must observe a newly committed permission revocation.
        async with database.session_factory() as session, session.begin():
            row = await repository.get_for_update(session, record.id)
            row.routing_decision = {**routing, "requested_permissions": []}
        for path in ("AGENTS.md", f"{tenant}/{attachment}.bin"):
            with pytest.raises(RuntimeCapabilityError):
                await read(path)
    finally:
        try:
            if run_id is not None:
                async with database.session_factory() as session, session.begin():
                    row = await repository.get_for_update(session, run_id)
                    row.status = RunStatus.CANCELLED.value
                await repository.delete_run(tenant, run_id)
        finally:
            await database.dispose()
