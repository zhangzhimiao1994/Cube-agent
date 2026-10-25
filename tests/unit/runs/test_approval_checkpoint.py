"""Only an exact top-level AutoGen waiting receipt can finish approval drain."""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runs.approval_checkpoint import checkpoint_waits_for_approval
from agent_hub.runtime.contracts import JsonValue, RuntimeCheckpoint

APPROVAL = 'approval_verify_once'
VERSION = '2'
RUN, TENANT = uuid4(), uuid4()


def receipt(*, position: int = 0, **changes: Any) -> dict[str, Any]:
    actor, name, arguments_hash = 'implementer', 'project.verify', hashlib.sha256(b'{}').hexdigest()
    value = {'status': 'waiting_approval', 'approval_id': APPROVAL, 'replay_safe': False,
             'actor': actor, 'name': name, 'position': position, 'arguments_hash': arguments_hash,
             'idempotency_key': hashlib.sha256(
                 f'{RUN}:{actor}:{position}:{name}:{arguments_hash}'.encode()).hexdigest()}
    value.update(changes)
    return value


def checkpoint(*, ledger: Any = None, **state_changes: Any) -> RuntimeCheckpoint:
    state = dict(terminal=False, tool_ledger=[receipt()] if ledger is None else ledger,
                 **state_changes)
    return RuntimeCheckpoint(id=uuid4(), runtime_type='autogen', runtime_version=VERSION,
        mode=TaskMode.DISCUSS, run_id=RUN, tenant_id=TENANT, state=cast(dict[str, JsonValue], state))


def rehashed(changed: dict[str, Any]) -> RuntimeCheckpoint:
    payload = checkpoint().to_payload()
    payload['state'] = changed
    payload['state_sha256'] = ''
    return RuntimeCheckpoint.from_payload(payload)


def completed(position: int = 0) -> dict[str, Any]:
    row = receipt(position=position)
    row.pop('approval_id')
    row.update(status='succeeded', artifact_id=str(uuid4()), artifact_sha256='a' * 64)
    return row


def test_receipt_uses_actual_formal_autogen_runtime_version() -> None:
    source = Path(__file__).resolve().parents[3] / 'src/agent_hub/runtime/autogen/adapter.py'
    assignment, = [node for node in ast.parse(source.read_bytes()).body
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
            and target.id == '_RUNTIME_VERSION' for target in node.targets)]
    assert ast.literal_eval(assignment.value) == VERSION


@pytest.mark.parametrize('replay_safe', [False, True])
@pytest.mark.parametrize('previous', [False, True])
def test_exact_last_waiting_receipt_matches(replay_safe: bool, previous: bool) -> None:
    ledger = ([completed()] if previous else []) + [receipt(position=int(previous), replay_safe=replay_safe)]
    saved = checkpoint(ledger=ledger)
    assert checkpoint_waits_for_approval(saved, APPROVAL)
    assert not checkpoint_waits_for_approval(saved, 'approval_other')


@pytest.mark.parametrize('name', ['Project.Verify', '_tool', '-tool', '.tool', '9tool', 'A' * 128])
def test_tool_name_matches_parent_character_contract(name: str) -> None:
    row = receipt(name=name)
    row['idempotency_key'] = hashlib.sha256(
        f"{RUN}:{row['actor']}:{row['position']}:{name}:{row['arguments_hash']}".encode()).hexdigest()
    assert checkpoint_waits_for_approval(checkpoint(ledger=[row]), APPROVAL)


@pytest.mark.parametrize('name', ['', 'A' * 129, 'project/verify', 'project:verify',
                                  'project verify', 'project.verify\n'])
def test_tool_name_outside_parent_character_contract_rejected(name: str) -> None:
    row = receipt(name=name)
    row['idempotency_key'] = hashlib.sha256(
        f"{RUN}:{row['actor']}:{row['position']}:{name}:{row['arguments_hash']}".encode()).hexdigest()
    assert not checkpoint_waits_for_approval(checkpoint(ledger=[row]), APPROVAL)


@pytest.mark.parametrize('approval_id', ['', 'approval_', 'approval-wrong', 'approval_' + 'a' * 101,
                                         APPROVAL + '\n', APPROVAL + ' ', ' approval_once', 1, True, None])
def test_invalid_approval_identity_is_rejected(approval_id: Any) -> None:
    saved = checkpoint(ledger=[receipt(approval_id=approval_id)])
    assert not checkpoint_waits_for_approval(saved, approval_id)


@pytest.mark.parametrize('suffix', ['a', 'a' * 100, 'A_0-z'])
def test_exact_approval_regex_boundaries(suffix: str) -> None:
    approval_id = 'approval_' + suffix
    assert checkpoint_waits_for_approval(checkpoint(ledger=[receipt(approval_id=approval_id)]), approval_id)


@pytest.mark.parametrize('terminal', [True, 0, 1, 'false', None])
def test_terminal_or_non_boolean_state_rejected(terminal: Any) -> None:
    saved = rehashed({'terminal': terminal, 'tool_ledger': [receipt()]})
    assert not checkpoint_waits_for_approval(saved, APPROVAL)


@pytest.mark.parametrize('field', ['terminal', 'tool_ledger'])
def test_missing_state_fields_rejected(field: str) -> None:
    state = {'terminal': False, 'tool_ledger': [receipt()]}
    state.pop(field)
    assert not checkpoint_waits_for_approval(rehashed(state), APPROVAL)


@pytest.mark.parametrize('ledger', [[], {}, {'row': receipt()}, 'waiting_approval', [None], [42]])
def test_wrong_ledger_or_receipt_shape_rejected(ledger: Any) -> None:
    assert not checkpoint_waits_for_approval(checkpoint(ledger=ledger), APPROVAL)


@pytest.mark.parametrize('field', list(receipt()))
def test_each_waiting_identity_field_is_required(field: str) -> None:
    row = receipt()
    row.pop(field)
    assert not checkpoint_waits_for_approval(checkpoint(ledger=[row]), APPROVAL)


@pytest.mark.parametrize('field,value', [
    ('status', 'running'), ('status', 'failed'), ('status', 'succeeded'),
    ('replay_safe', 0), ('replay_safe', 1), ('replay_safe', 'false'), ('replay_safe', None),
    ('actor', ''), ('actor', 'other'), ('actor', 'x\ny'), ('actor', 'actor with space'),
    ('actor', 1), ('actor', 'a' * 129), ('name', ''), ('name', 'workspace.bundle'),
    ('name', 'project.verify\n'), ('name', 1), ('name', 'a' * 129),
    ('position', True), ('position', -1), ('position', 1), ('position', 0.0), ('position', '0'),
    ('arguments_hash', 'A' * 64), ('arguments_hash', 'a' * 63), ('arguments_hash', 'b' * 64),
    ('arguments_hash', None), ('idempotency_key', '0' * 64), ('idempotency_key', 'A' * 64),
    ('idempotency_key', None),
])
def test_rehashed_wrong_identity_does_not_satisfy_drain(field: str, value: Any) -> None:
    assert not checkpoint_waits_for_approval(checkpoint(ledger=[receipt(**{field: value})]), APPROVAL)


@pytest.mark.parametrize('field,value', [('artifact_id', None), ('artifact_id', str(uuid4())),
    ('artifact_sha256', None), ('artifact_sha256', 'a' * 64), ('artifact', {}),
    ('payload', {'approval_id': APPROVAL}), ('approval_scope', 'forged'), ('extra', True)])
def test_waiting_receipt_has_no_artifact_or_extra_fields(field: str, value: Any) -> None:
    assert not checkpoint_waits_for_approval(checkpoint(ledger=[receipt(**{field: value})]), APPROVAL)


@pytest.mark.parametrize('ledger', [
    [receipt(), receipt(position=1)],
    [receipt(), completed(1)],
    [receipt(approval_id='approval_other'), receipt(position=1)],
    [receipt(status='running', approval_id='approval_other'), receipt(position=1)],
])
def test_waiting_row_is_last_and_only_pending_receipt(ledger: list[dict[str, Any]]) -> None:
    assert not checkpoint_waits_for_approval(checkpoint(ledger=ledger), APPROVAL)


@pytest.mark.parametrize('version', ['1', '3', '2.0', '99'])
def test_other_autogen_versions_rejected(version: str) -> None:
    assert not checkpoint_waits_for_approval(checkpoint().model_copy(update={'runtime_version': version}), APPROVAL)


@pytest.mark.parametrize('mode', [TaskMode.DIRECT, TaskMode.DISPATCH, TaskMode.HYBRID])
def test_only_discuss_mode_supported(mode: TaskMode) -> None:
    assert not checkpoint_waits_for_approval(checkpoint().model_copy(update={'mode': mode}), APPROVAL)


def test_stale_state_hash_or_rebound_run_rejected() -> None:
    saved = checkpoint()
    assert not checkpoint_waits_for_approval(saved.model_copy(update={'state_sha256': '0' * 64}), APPROVAL)
    assert not checkpoint_waits_for_approval(saved.model_copy(update={'run_id': uuid4()}), APPROVAL)
    assert not checkpoint_waits_for_approval(None, APPROVAL)


@pytest.mark.parametrize('version', ['2', '3'])
@pytest.mark.parametrize('stage', [0, 1])
def test_hybrid_autogen_remains_unsupported(version: str, stage: int) -> None:
    child = checkpoint()
    outer = RuntimeCheckpoint(id=uuid4(), runtime_type='hybrid', runtime_version=version,
        run_id=RUN, tenant_id=TENANT, mode=TaskMode.HYBRID,
        state={'terminal': False, 'next_stage': stage, 'child_checkpoint': child.to_payload()})
    assert not checkpoint_waits_for_approval(outer, APPROVAL)


def test_nested_fake_waiting_receipt_does_not_satisfy_drain() -> None:
    assert not checkpoint_waits_for_approval(rehashed({'terminal': False, 'metadata': {
        'tool_ledger': [receipt()]}, 'tool_ledger': [completed()]}), APPROVAL)
