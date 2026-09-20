from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

from agent_hub.db.session import build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.direct import DirectRuntime
from agent_hub.runtime.instruction_context import InstructionContextLoader
from agent_hub.runtime.registry import RuntimeRegistry


class CapturingGateway:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        return GatewayCompletion(
            response=ModelResponse(text="Probe complete", usage=TokenUsage(10, 5, 15)),
            deployment_id="probe", logical_model=request.logical_model,
            provider_id="probe", provider_model="probe",
        )


class ProbeQueue:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        pass


async def test_persisted_direct_guidance_is_loaded_injected_and_redacted(
    database_url: str, tmp_path: Path,
) -> None:
    database = build_database(database_url)
    repository = RunRepository(database.session_factory)
    tenant, actor = uuid4(), uuid4()
    root = tmp_path / "project-store"
    store = ProjectWorkspaceStore(root)
    marker = f"private-guidance-{uuid4().hex}"
    store.write_bytes(tenant, "project", "session", "AGENTS.md", marker.encode(), "text/plain")
    store.write_bytes(tenant, "project", "other", "AGENTS.md", b"FOREIGN-SESSION", "text/plain")
    gateway = CapturingGateway()
    service = RunService(
        repository, runtime_registry=RuntimeRegistry((DirectRuntime(gateway, logical_model="main"),)),
        router=None, task_queue=ProbeQueue(),
        instruction_context_loader=InstructionContextLoader(repository=repository, project_root=root),
    )
    run_ids: list[UUID] = []
    try:
        for allowed in (True, False):
            submitted = await service.submit(
                tenant_id=tenant, actor_id=actor, message="Reply briefly about this workspace.",
                mode=TaskMode.DIRECT, project_id="project", workspace_session_id="session",
                sandbox_profile="read_only" if allowed else "none",
                requested_permissions=("workspace.read",) if allowed else (),
            )
            run_ids.append(submitted.id)
            completed = await service.execute(submitted.id)
            assert completed.status is RunStatus.COMPLETED
            events = await repository.events(tenant, submitted.id)
            loaded = [event for event in events if event["kind"] == "context.loaded"]
            injected = [event for event in events if event["kind"] == "context.injected"]
            assert len(loaded) == 1
            assert marker not in json.dumps(events, default=str)
            request_text = "\n".join(str(item.content) for item in gateway.requests[-1].messages)
            assert "FOREIGN-SESSION" not in request_text
            if allowed:
                assert marker in request_text
                assert len(injected) == 1
                load_payload = loaded[0]["payload"]
                inject_payload = injected[0]["payload"]
                assert isinstance(load_payload, dict) and isinstance(inject_payload, dict)
                assert load_payload["load_id"] == inject_payload["load_id"]
                sources = inject_payload["sources"]
                assert isinstance(sources, list) and isinstance(sources[0], dict)
                assert sources[0]["injected_sha256"] == hashlib.sha256(marker.encode()).hexdigest()
                load_seq, inject_seq = loaded[0]["sequence"], injected[0]["sequence"]
                assert isinstance(load_seq, int) and isinstance(inject_seq, int)
                assert load_seq < inject_seq
            else:
                assert marker not in request_text
                assert injected == []
            # Re-executing a completed run must not reissue the model request.
            count = len(gateway.requests)
            again = await service.execute(submitted.id)
            assert again.status is RunStatus.COMPLETED
            assert len(gateway.requests) == count
        assert len(gateway.requests) == 2
    finally:
        try:
            for run_id in run_ids:
                async with database.session_factory() as session, session.begin():
                    row = await repository.get_for_update(session, run_id)
                    row.status = RunStatus.CANCELLED.value
                await repository.delete_run(tenant, run_id)
        finally:
            await database.dispose()
