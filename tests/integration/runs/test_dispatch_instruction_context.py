from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from agent_hub.db.session import build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.crew.adapter import CrewAIObjectFactory, CrewDispatchRuntime
from agent_hub.runtime.crew.plan import AgentSpec, DispatchPlan, DispatchStep
from agent_hub.runtime.instruction_context import InstructionContextLoader
from agent_hub.runtime.registry import RuntimeRegistry


class CapturingGateway:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        self.requests.append(request)
        text = (
            '{"verdict":"approve"}'
            if request.logical_model == "reviewer_probe"
            else "Workspace probe complete."
        )
        return GatewayCompletion(
            response=ModelResponse(text=text, usage=TokenUsage(10, 5, 15)),
            deployment_id="probe", logical_model=request.logical_model,
            provider_id="probe", provider_model="probe", cost_usd=Decimal(0),
        )


class ProbeQueue:
    async def enqueue_run(self, run_id: UUID, *, idempotency_key: str) -> None:
        pass


def _reviewed_plan() -> DispatchPlan:
    return DispatchPlan(
        agents=(
            AgentSpec(id="worker", role="Worker", goal="Answer the workspace request.",
                      logical_model="worker_probe", max_output_tokens=2048),
            AgentSpec(id="reviewer", role="Reviewer", goal="Review the worker answer.",
                      logical_model="reviewer_probe", max_output_tokens=2048),
        ),
        steps=(DispatchStep(
            id="answer", agent="worker", reviewer="reviewer",
            task="Reply briefly about the workspace without quoting private guidance.",
            final_synthesizer=True, token_budget=8192, timeout_seconds=60,
        ),),
        max_parallelism=1, total_token_budget=16384, total_timeout_seconds=120,
    )


def _request_text(request: ModelRequest) -> str:
    return "\n".join(str(message.content) for message in request.messages)


async def test_persisted_dispatch_guidance_reaches_real_worker_and_reviewer(
    database_url: str, tmp_path: Path,
) -> None:
    database = build_database(database_url)
    repository = RunRepository(database.session_factory)
    tenant, actor = uuid4(), uuid4()
    root = tmp_path / "project-store"
    store = ProjectWorkspaceStore(root)
    marker = f"private-dispatch-guidance-{uuid4().hex}"
    foreign_marker = f"foreign-session-guidance-{uuid4().hex}"
    store.write_bytes(tenant, "project", "session", "AGENTS.md", marker.encode(), "text/plain")
    store.write_bytes(
        tenant, "project", "other", "AGENTS.md", foreign_marker.encode(), "text/plain",
    )
    gateway = CapturingGateway()
    storage_root = tmp_path / "session" / "crewai"
    runtime = CrewDispatchRuntime(
        gateway, _reviewed_plan(), crew_factory=CrewAIObjectFactory(storage_dir=storage_root),
    )
    service = RunService(
        repository, runtime_registry=RuntimeRegistry((runtime,)),
        router=None, task_queue=ProbeQueue(),
        instruction_context_loader=InstructionContextLoader(repository=repository, project_root=root),
    )
    run_ids: list[UUID] = []
    try:
        for allowed in (True, False):
            submitted = await service.submit(
                tenant_id=tenant, actor_id=actor, message="Reply briefly about this workspace.",
                mode=TaskMode.DISPATCH, project_id="project", workspace_session_id="session",
                sandbox_profile="read_only" if allowed else "none",
                requested_permissions=("workspace.read",) if allowed else (),
            )
            run_ids.append(submitted.id)
            start = len(gateway.requests)
            completed = await service.execute(submitted.id)
            events = await service.events(tenant, submitted.id)
            assert completed.status is RunStatus.COMPLETED, events
            assert any(event["kind"] == "runtime.completed" for event in events)
            requests = gateway.requests[start:]
            assert [request.logical_model for request in requests] == [
                "worker_probe", "reviewer_probe",
            ]
            assert all(not request.tools for request in requests)
            assert requests[1].response_schema is not None
            assert (storage_root / "agent-hub" / str(tenant) / str(submitted.id)).is_dir()

            loaded = [event for event in events if event["kind"] == "context.loaded"]
            injected = [event for event in events if event["kind"] == "context.injected"]
            assert len(loaded) == 1
            load_payload = loaded[0]["payload"]
            assert isinstance(load_payload, dict)
            assert load_payload["tenant_id"] == str(tenant)
            assert load_payload["run_id"] == str(submitted.id)
            load_id = load_payload["load_id"]
            assert isinstance(load_id, str)
            sources = load_payload["sources"]
            assert isinstance(sources, list)
            agent_sources = [source for source in sources if source["path"] == "AGENTS.md"]
            assert len(agent_sources) == 1
            public_json = json.dumps(events, default=str)
            for private_value in (marker, foreign_marker, str(root), str(storage_root)):
                assert private_value not in public_json
            texts = [_request_text(request) for request in requests]
            for text in texts:
                assert foreign_marker not in text
                assert load_id not in text

            if allowed:
                digest = hashlib.sha256(marker.encode()).hexdigest()
                assert agent_sources[0]["status"] == "loaded"
                assert agent_sources[0]["content_sha256"] == digest
                assert agent_sources[0]["project_id"] == "project"
                assert agent_sources[0]["session_id"] == "session"
                guidance_blocks: list[str] = []
                for text in texts:
                    assert text.count("<PROJECT_GUIDANCE_JSON>") == 1
                    assert text.count("</PROJECT_GUIDANCE_JSON>") == 1
                    block = text.split("<PROJECT_GUIDANCE_JSON>", 1)[1].split(
                        "</PROJECT_GUIDANCE_JSON>", 1,
                    )[0]
                    guidance_blocks.append(block)
                    assert json.loads(block) == [{
                        "path": "AGENTS.md", "kind": "project_guidance",
                        "text": marker, "truncated": False,
                    }]
                assert guidance_blocks[0] == guidance_blocks[1]
                assert len(injected) == 2
                async with database.session_factory() as session:
                    checkpoint = await repository.latest_checkpoint(
                        session, tenant_id=tenant, run_id=submitted.id,
                    )
                assert checkpoint is not None
                models = checkpoint.state["models"]
                assert isinstance(models, Mapping)
                for event, request, expected_actor, stage in zip(
                    injected, requests, ("worker", "reviewer"),
                    ("dispatch_step", "dispatch_review"), strict=True,
                ):
                    payload = event["payload"]
                    assert isinstance(payload, dict)
                    assert event["actor"] == expected_actor
                    assert payload["actor"] == expected_actor
                    assert payload["stage"] == stage
                    assert payload["step_id"] == "answer"
                    assert payload["load_id"] == load_id
                    assert payload["tenant_id"] == str(tenant)
                    assert payload["run_id"] == str(submitted.id)
                    assert payload["logical_model"] == request.logical_model
                    assert payload["boundary"] == "model_gateway"
                    assert payload["sources"] == [{
                        "path": "AGENTS.md", "injected_bytes": len(marker.encode()),
                        "injected_sha256": digest, "truncated": False,
                    }]
                    entries = [entry for entry in models.values()
                               if isinstance(entry, Mapping) and entry["actor"] == expected_actor]
                    assert len(entries) == 1
                    assert entries[0]["status"] == "succeeded"
                    ledger_key = payload["ledger_key"]
                    assert isinstance(ledger_key, str)
                    assert models[ledger_key] == entries[0]
                    assert payload["attempt"] == entries[0]["attempt"]
                    assert payload["call_index"] == entries[0]["call_index"]
                    assert payload["ledger_request_sha256"] == entries[0]["request_sha256"]
                    load_seq, inject_seq = loaded[0]["sequence"], event["sequence"]
                    assert isinstance(load_seq, int) and isinstance(inject_seq, int)
                    assert load_seq < inject_seq
            else:
                assert agent_sources[0]["status"] == "unavailable"
                assert injected == []
                for text in texts:
                    assert marker not in text
                    assert "<PROJECT_GUIDANCE_JSON>" not in text

            # A completed persistent run must not reload context or reissue either model call.
            count = len(gateway.requests)
            again = await service.execute(submitted.id)
            assert again.status is RunStatus.COMPLETED
            assert len(gateway.requests) == count
            assert await service.events(tenant, submitted.id) == events
        assert len(gateway.requests) == 4
    finally:
        try:
            for run_id in run_ids:
                async with database.session_factory() as session, session.begin():
                    row = await repository.get_for_update(session, run_id)
                    row.status = RunStatus.CANCELLED.value
                await repository.delete_run(tenant, run_id)
        finally:
            await database.dispose()
