from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime.contracts import Artifact, JsonValue, TaskContext
from agent_hub.runtime.crew.engineering_delivery import engineering_action_progress
from agent_hub.runtime.engineering_actions import EngineeringProgress, required_engineering_action
from tests.unit.runtime.crew.test_engineering_delivery_gate import fixture, sha


def data() -> tuple[TaskContext, dict[str, Mapping[str, JsonValue]], dict[str, Artifact]]:
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DISPATCH, request='Build.')
    states, artifacts, _ = fixture()
    for key in states:
        states[key] = {**states[key], 'round': int(key) + 1}
    result: dict[str, JsonValue] = {'schema_version': 1,
        'scope': {'run_id': str(context.run_id), 'tenant_id': str(context.tenant_id)},
        'manifests': ({'path': 'package.json', 'status': 'observed'},),
        'operations': {'build': 'declared', 'test': 'declared'}, 'issues': ()}
    artifact = Artifact(id=uuid4(), type='tool_result', producer='implementer', source_ids=(str(uuid4()),),
        content={'agent_id': 'implementer', 'tool_name': 'project.inspect',
            'arguments_sha256': sha(b'{}'), 'result': result})
    artifacts['inspect'] = artifact
    states['inspect'] = {'step_id': 'build', 'attempt': 0, 'round': 0, 'tool_index': 0,
        'name': 'project.inspect', 'status': 'succeeded', 'arguments_sha256': sha(b'{}'),
        'artifact_id': str(artifact.id), 'sha256': artifact.content_sha256}
    return context, states, artifacts


def test_progress_uses_only_current_causal_prefix() -> None:
    context, states, artifacts = data()
    def progress(round_index: int) -> EngineeringProgress:
        return engineering_action_progress(context=context, step_id='build', actor='implementer',
            states=states, artifacts=artifacts, attempt=0, round_index=round_index)
    assert required_engineering_action(progress(0)) == 'project.inspect'
    assert required_engineering_action(progress(1)) == 'project.verify'
    assert progress(2).has_writes
    assert progress(3).verification_current and not progress(3).bundle_current
    assert progress(4).bundle_current


def test_recovery_sees_real_previous_attempt_writes_without_rereading_all_files() -> None:
    context, states, artifacts = data()
    del states['1']
    del states['2']
    value = engineering_action_progress(context=context, step_id='build', actor='implementer',
        states=states, artifacts=artifacts, attempt=1, round_index=0, recovering=True)
    assert required_engineering_action(value) == 'project.verify'


def test_changed_artifact_hash_does_not_force_an_action() -> None:
    context, states, artifacts = data()
    states['inspect'] = {**states['inspect'], 'sha256': '0' * 64}
    value = engineering_action_progress(context=context, step_id='build', actor='implementer',
        states=states, artifacts=artifacts, attempt=0, round_index=2)
    assert value.uncertain and required_engineering_action(value) is None


def test_identical_acknowledged_write_does_not_force_failed_source_again() -> None:
    from tests.unit.runtime.crew.test_engineering_delivery_gate import change_result

    context, states, artifacts = data()
    del states['2']
    change_result((states, artifacts, {}), '1', {'status': 'failed'})
    states['1'] = {**states['1'], 'status': 'failed'}

    def append_write(key: str, changed: bool) -> None:
        content = dict(artifacts['0'].content)
        if changed:
            content['result'] = {'workspace_files': ({
                'path': 'index.js', 'sha256': 'e' * 64, 'size_bytes': 12,
            },)}
        artifact = Artifact(id=uuid4(), type='tool_result', producer='implementer',
                            source_ids=artifacts['0'].source_ids, content=content)
        artifacts[key] = artifact
        states[key] = {**states['0'], 'round': int(key) + 1,
                       'artifact_id': str(artifact.id), 'sha256': artifact.content_sha256}

    def action() -> str | None:
        return required_engineering_action(engineering_action_progress(
            context=context, step_id='build', actor='implementer', states=states,
            artifacts=artifacts, attempt=0, round_index=10, completion_proposed=True))

    assert action() is None
    append_write('3', False)
    assert action() is None
    append_write('4', True)
    assert action() == 'project.verify'
