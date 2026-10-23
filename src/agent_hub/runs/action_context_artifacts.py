"""Published read observations for Direct action recovery, never execution grants."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.action_checkpoint import action_checkpoint_valid
from agent_hub.runtime.contracts import Artifact, EventKind, RunEvent, RuntimeCheckpoint

_EMPTY_ARGUMENTS = hashlib.sha256(b'{}').hexdigest()


class PublishedEvents(Protocol):
    async def raw_events(self, tenant_id: UUID, run_id: UUID) -> tuple[RunEvent, ...]: ...


async def action_context_artifacts(
    repository: PublishedEvents, *, tenant_id: UUID, run_id: UUID,
    checkpoint: RuntimeCheckpoint | None, routing_decision: Mapping[str, object],
) -> tuple[Artifact, ...]:
    """Use only the claimed checkpoint's committed event prefix and exact scope.

    Conversation recall intentionally excludes this run. These observations are
    recovered separately and still undergo the runtime's full payload/source
    checks; verification and other tool/model output never enter this channel.
    """
    if (checkpoint is None or checkpoint.mode is not TaskMode.DIRECT
            or checkpoint.runtime_type != 'direct_action' or checkpoint.runtime_version != '2'):
        return ()
    if (not action_checkpoint_valid(checkpoint)
            or (checkpoint.tenant_id, checkpoint.run_id) != (tenant_id, run_id)):
        raise ValueError('action observation checkpoint is unavailable')
    scope = {'tenant_id':str(tenant_id), 'run_id':str(run_id),
             'project_id':routing_decision.get('project_id'),
             'workspace_session_id':routing_decision.get('workspace_session_id')}
    observations: dict[UUID, Artifact] = {}
    publications: dict[UUID, Artifact] = {}
    sequence = 0
    for event in await repository.raw_events(tenant_id, run_id):
        if event.run_id != run_id or event.sequence <= sequence:
            raise ValueError('action observation event scope changed')
        sequence = event.sequence
        if event.checkpoint is not None and event.checkpoint.id == checkpoint.id:
            if event.kind is not EventKind.CHECKPOINT_SAVED or event.checkpoint.to_payload() != checkpoint.to_payload():
                raise ValueError('action observation checkpoint changed')
            return tuple(observations.values())
        artifact = event.artifact
        if event.kind is EventKind.ARTIFACT_CREATED and artifact is not None:
            previous = publications.get(artifact.id)
            if previous is not None and previous.to_payload() != artifact.to_payload():
                raise ValueError('action observation publication identity conflicts')
            publications[artifact.id] = artifact
        if (event.kind is not EventKind.ARTIFACT_CREATED or artifact is None
                or artifact.type != 'tool_result' or artifact.producer != 'main_agent'
                or artifact.content.get('tool_name') != 'project.inspect'):
            continue
        result = artifact.content.get('result')
        if (event.actor != 'main_agent' or artifact.content_sha256 != artifact.recompute_content_sha256()
                or artifact.content.get('agent_id') != 'main_agent'
                or artifact.content.get('arguments_sha256') != _EMPTY_ARGUMENTS
                or not isinstance(result, Mapping) or result.get('scope') != scope):
            raise ValueError('action observation integrity or scope changed')
        observations[artifact.id] = artifact
    raise ValueError('action observation checkpoint publication is unavailable')
