"""The existing real repair/checkpoint chain with checked JSON boundaries."""
from __future__ import annotations

from uuid import UUID, uuid4

from agent_hub.runtime.artifacts import ArtifactReference
from agent_hub.runtime.contracts import Artifact, RuntimeCheckpoint, TaskContext, _freeze_object
from tests.unit.runs.project_auto_repair_typed_fixture import mapping, registry
from tests.unit.runs.test_project_auto_repair_runtime import _read_repair_fixture


async def diagnostic_case() -> tuple[TaskContext, RuntimeCheckpoint, RuntimeCheckpoint, tuple[Artifact, ...]]:
    journey, repo, runtime, service, _, source = await _read_repair_fixture()
    runtime.second_fault = 'ack_read'
    await service.execute(repo.run_id)
    saved = repo.checkpoint
    refs = tuple(ArtifactReference(id=UUID(key), sha256=value)
                 for key, value in registry(saved.state['artifact_registry']).items())
    artifacts = await journey.artifacts.get_many(saved.tenant_id, saved.run_id, refs)
    context = journey.context.model_copy(update={'routing_decision': repo.row.routing_decision})
    return context, source, saved, artifacts


def changed(checkpoint: RuntimeCheckpoint, **updates: object) -> RuntimeCheckpoint:
    state = dict(mapping(checkpoint.to_payload()['state']))
    state.update(updates)
    return RuntimeCheckpoint(id=uuid4(), runtime_type=checkpoint.runtime_type,
        runtime_version=checkpoint.runtime_version, run_id=checkpoint.run_id,
        tenant_id=checkpoint.tenant_id, mode=checkpoint.mode,
        state=_freeze_object(state, name='diagnostic checkpoint fixture'))
