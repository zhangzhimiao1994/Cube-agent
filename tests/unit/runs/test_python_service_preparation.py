"""The real service preparation boundary, with fake persistence/native transport."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from agent_hub.runs.repository import RunRepository
from agent_hub.runs.service import RunService
from agent_hub.runtime.contracts import Artifact, TaskContext
from agent_hub.runtime.registry import RuntimeRegistry
from tests.unit.runs.test_python_original_preparation import configured
from tests.unit.runs.test_terminal_hooks import RuntimeCompletes


async def case(tmp_path: Path) -> tuple[Any, ...]:
    preparer, repository, context, executor, authorizer, _, _ = await configured(tmp_path)
    authorizer.allowed = True
    service = RunService(
        cast(RunRepository, repository), runtime_registry=RuntimeRegistry((RuntimeCompletes(),)),
        router=None, task_queue=cast(Any, object()), worker_id='test-worker',
        project_input_preparer=preparer,
    )
    return service, preparer, repository, context, executor


async def test_normal_service_accepts_real_python_preparer_and_preserves_identity(tmp_path: Path) -> None:
    service, _, _, context, executor = await case(tmp_path)
    prepared = await service._prepare_original_project_context(context)
    assert prepared.routing_decision['project_input_preparation']['schema_version'] == 2
    assert prepared.routing_decision['python_project_history']
    assert prepared.execution_token == context.execution_token
    assert len(executor.calls) == 1


@pytest.mark.parametrize('mutation', ['extra_route', 'history', 'missing_history', 'artifact', 'metadata', 'schema', 'persisted'])
async def test_normal_service_rejects_python_context_substitution(tmp_path: Path, mutation: str) -> None:
    service, preparer, repository, context, _ = await case(tmp_path)
    async def corrupt(original: TaskContext) -> TaskContext:
        prepared = await preparer(original)
        route = dict(prepared.routing_decision)
        if mutation == 'extra_route':
            route['unexpected_python_privilege'] = True
        elif mutation == 'history':
            route['python_project_history'] = {}
        elif mutation == 'missing_history':
            del route['python_project_history']
        elif mutation == 'schema':
            route['project_input_preparation'] = {**route['project_input_preparation'], 'schema_version': True}
        elif mutation == 'persisted':
            repository.row.routing_decision['project_input_preparation']['observation_sha256'] = '0' * 64
        elif mutation == 'metadata':
            artifacts = list(prepared.artifacts)
            old = artifacts[-1]
            artifacts[-1] = Artifact(id=old.id, type=old.type, producer=old.producer,
                content={**old.content, 'expected_recipe_sha256': '0' * 64}, source_ids=old.source_ids)
            prepared = prepared.model_copy(update={'artifacts': tuple(artifacts)})
        else:
            artifacts = list(prepared.artifacts)
            artifacts[-1] = artifacts[-1].model_copy(update={'producer': 'model'})
            prepared = prepared.model_copy(update={'artifacts': tuple(artifacts)})
        return cast(TaskContext, prepared.model_copy(update={'routing_decision': route}))
    service._project_input_preparer = corrupt
    with pytest.raises((RuntimeError, ValueError)):
        await service._prepare_original_project_context(context)


@pytest.mark.parametrize('inject_history', [False, True])
async def test_node_preparation_contract_does_not_gain_python_route_permission(
    tmp_path: Path, inject_history: bool,
) -> None:
    from tests.unit.runs.test_original_project_preparation import setup
    preparer, repository, context, _, _, _, _ = await setup(tmp_path)
    async def prepare(original: TaskContext) -> TaskContext:
        prepared = await preparer(original)
        if inject_history:
            prepared = prepared.model_copy(update={'routing_decision': {
                **prepared.routing_decision, 'python_project_history': {},
            }})
        return cast(TaskContext, prepared)
    service = RunService(cast(RunRepository, repository),
        runtime_registry=RuntimeRegistry((RuntimeCompletes(),)), router=None,
        task_queue=cast(Any, object()), worker_id='test-worker', project_input_preparer=prepare)
    if inject_history:
        with pytest.raises(RuntimeError, match='original project preparation'):
            await service._prepare_original_project_context(context)
    else:
        prepared = await service._prepare_original_project_context(context)
        state = prepared.routing_decision['project_input_preparation']
        assert isinstance(state, Mapping) and state['schema_version'] == 1
        assert 'python_project_history' not in prepared.routing_decision
