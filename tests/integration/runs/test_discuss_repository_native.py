"""Configured Discuss private storage across real PG pools, not paid-user proof.

RunService, runtime registry, AutoGen and private repository are real. Model,
configuration, credential and capacity transports are fixed doubles. This
does not exercise project commands, approvals or verifier authority recovery.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import cast
from uuid import UUID, uuid4

import pytest

import agent_hub.runtime.defaults as defaults_module
from agent_hub.auth.models import Role
from agent_hub.config.repository import ConfigRevision
from agent_hub.config.service import ConfigService
from agent_hub.db.session import build_database
from agent_hub.domain.runs import RunStatus, TaskMode
from agent_hub.models.types import Deployment, ModelRequest, ModelResponse, TokenUsage
from agent_hub.runs.artifacts import PostgresArtifactRepository
from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.artifacts import ArtifactReference, ArtifactRepositoryError
from agent_hub.runtime.defaults import configured_runtime_registry, default_runtime_registry
from agent_hub.security.secrets import SecretService
from tests.integration.runs.test_action_direct_native_recovery import FixedPublishedConfig
from tests.integration.runs.test_action_service_native_recovery import RecordingQueue
from tests.unit.runtime.test_configured_runtime import FakeSecretService, ImmediateCapacity
from tests.unit.runtime.test_discuss_repository_wiring import (
    discussion_context,
    exercise_discuss_repository_recovery,
)


class DiscussionConfig(FixedPublishedConfig):
    async def get_current(self, tenant_id: UUID) -> ConfigRevision:
        revision = await super().get_current(tenant_id)
        return replace(revision, document={**revision.document, 'agents': [
            {'id': name, 'role': name.title(), 'prompt': 'Compare the alternatives.',
             'model': 'offline', 'skills': []} for name in ('analyst', 'critic')
        ]})


class OfflineDiscussionModel:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(
        self, deployment: Deployment, request: ModelRequest, api_key: str,
    ) -> ModelResponse:
        assert deployment.logical_model == 'offline' and api_key == 'sk-live'
        self.requests.append(request)
        assert len(self.requests) <= 6, 'fixed discussion repeated unexpectedly'
        return ModelResponse(
            text='analyst' if request.max_output_tokens == 512 else '[COMPLETE] Compared the alternatives.',
            usage=TokenUsage(prompt_tokens=2, completion_tokens=1, total_tokens=3),
        )


async def test_configured_discuss_persists_private_artifacts_across_real_pools(
    database_url: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = build_database(database_url)
    tenant, actor = uuid4(), uuid4()
    transport = OfflineDiscussionModel()
    redis = object()

    def capacity(
        redis_client: object, *, deployments: tuple[Deployment, ...], fingerprint_resolver: object,
    ) -> ImmediateCapacity:
        assert redis_client is redis and callable(fingerprint_resolver)
        return ImmediateCapacity(deployments)

    monkeypatch.setattr(defaults_module, 'CapacityPool', capacity)
    try:
        assert database.engine.url.get_backend_name() == 'postgresql'
        artifacts = PostgresArtifactRepository(database.session_factory)
        runs = RunRepository(database.session_factory)
        registry = configured_runtime_registry(
            config_service=cast(ConfigService, DiscussionConfig(tenant)),
            secret_service=cast(SecretService, FakeSecretService()), redis_client=redis,
            transport=transport, artifact_repository=artifacts,
        )
        service = RunService(runs, runtime_registry=registry, router=None,
                             task_queue=RecordingQueue(), runtime_timeout_seconds=30,
                             runtime_token_budget=5000)
        submitted = await service.submit(
            tenant_id=tenant, actor_id=actor, actor_role=Role.OPERATOR,
            mode=TaskMode.DISCUSS, message='Compare two options and finish with a short conclusion.',
            agent_ids=('analyst', 'critic'), direct_model='offline', allow_scale_mode_upgrade=False,
        )
        await service.execute(submitted.id)
        record = await runs.get(tenant, submitted.id, actor_id=actor)
        assert record.status is RunStatus.COMPLETED
        async with database.session_factory() as session:
            checkpoint = await runs.latest_checkpoint(session, tenant_id=tenant, run_id=submitted.id)
        assert checkpoint is not None and checkpoint.mode is TaskMode.DISCUSS
        references = checkpoint.state.get('artifact_registry')
        assert isinstance(references, Mapping) and references
        reference_list: list[ArtifactReference] = []
        for key, value in references.items():
            assert isinstance(key, str) and isinstance(value, str)
            reference_list.append(ArtifactReference(UUID(key), value))
        refs = tuple(reference_list)
        expected = tuple(item.to_payload() for item in await artifacts.get_many(tenant, submitted.id, refs))
        assert expected and transport.requests
        request_count = len(transport.requests)
    finally:
        await database.dispose()

    fresh = build_database(database_url)
    try:
        restored = PostgresArtifactRepository(fresh.session_factory)
        actual = await restored.get_many(tenant, submitted.id, refs)
        assert tuple(item.to_payload() for item in actual) == expected
        assert len(transport.requests) == request_count
        with pytest.raises(ArtifactRepositoryError):
            await restored.get_many(uuid4(), submitted.id, refs)
        with pytest.raises(ArtifactRepositoryError):
            await restored.get_many(tenant, uuid4(), refs)
        bad = ArtifactReference(refs[0].id, ('1' if refs[0].sha256[0] == '0' else '0') + refs[0].sha256[1:])
        with pytest.raises(ArtifactRepositoryError):
            await restored.get_many(tenant, submitted.id, (bad,))
    finally:
        await fresh.dispose()


async def test_configured_discuss_resumes_durable_observations_through_fresh_pg_repository(
    database_url: str,
) -> None:
    """Private PG is real; fixed calculator/model transport is not engineering proof."""
    first = build_database(database_url)
    fresh = build_database(database_url)
    try:
        context = discussion_context()
        runs = RunRepository(first.session_factory)
        service = RunService(runs, runtime_registry=default_runtime_registry(), router=None,
                             task_queue=RecordingQueue())
        assert context.actor_id is not None
        submitted = await service.submit(
            tenant_id=context.tenant_id, actor_id=context.actor_id, actor_role=context.actor_role,
            mode=TaskMode.DISCUSS, message=context.request, allow_scale_mode_upgrade=False,
        )
        await exercise_discuss_repository_recovery(
            PostgresArtifactRepository(first.session_factory),
            PostgresArtifactRepository(fresh.session_factory),
            context=context.model_copy(update={'run_id': submitted.id}),
        )
    finally:
        await first.dispose()
        await fresh.dispose()
