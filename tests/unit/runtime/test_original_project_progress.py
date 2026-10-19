from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from agent_hub.capabilities.project_verification import ProjectVerificationError, _validate_report
from tests.unit.runtime.test_direct_project_verification import context


def selected(**changes: Any) -> Any:
    source, contract = 'a' * 64, 'b' * 64
    try:
        _validate_report(json.dumps({'schema_version': 1, 'status': 'failed', 'source_sha256': source,
            'runtime': {'node': 'v22.12.0', 'npm': '10.9.0'}, 'checks': [
                {'phase': phase, 'status': 'failed' if phase == 'test' else 'passed',
                 'exit_code': 1 if phase == 'test' else 0, 'timed_out': False, 'stdout': '', 'stderr': ''}
                for phase in ('install', 'build', 'test')]}), expected_source=source)
    except ProjectVerificationError as error:
        assert error.project_failure is not None and error.diagnostic is not None
        observation = {'schema_version': 1, 'status': 'failed',
            'verification': error.project_failure.to_payload(), 'execution_diagnostic': error.diagnostic.to_payload(),
            'original_check_contract': {'schema_version': 1, 'contract_sha256': contract,
                                        'protected_sha256': contract}}
    else:
        raise AssertionError('fixture must fail its original check')
    state = {'schema_version': 1, 'phase': 'observed', 'reference': {'schema_version': 1,
        'contract_sha256': 'c' * 64, 'input_source_sha256': source, 'file_count': 3},
        'observation': observation, 'observation_sha256': hashlib.sha256(json.dumps(observation,
            default=dict, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')).hexdigest()}
    return context(routing_decision={'project_input_contract_version': 1,
        'project_input': {'schema_version': 1, 'kind': 'managed_workspace', 'editable_paths': ['src']},
        'project_input_preparation': state, **changes})


def test_observed_original_failure_is_a_hint_to_repair_not_repeat_unchanged_verify() -> None:
    from agent_hub.runtime.original_project_progress import original_failure_observed

    assert original_failure_observed(selected()) is True
    assert original_failure_observed(context()) is False


def test_both_orchestrators_use_failed_baseline_without_granting_delivery_credit() -> None:
    from agent_hub.runtime.autogen.engineering_delivery import engineering_progress
    from agent_hub.runtime.crew.engineering_delivery import engineering_action_progress

    ctx = selected()
    progresses = (
        engineering_progress(ctx, tool_entries=(), artifacts=(), allowed_tools={}),
        engineering_action_progress(context=ctx, step_id='implementation', actor='engineer',
            states={}, artifacts={}, attempt=0, round_index=0),
    )
    for progress in progresses:
        assert progress.verification_seen is True
        assert progress.verification_failed_current is True
        assert progress.verification_current is False
        assert progress.bundle_current is False
        assert progress.has_writes is False


def test_new_policy_never_upgrades_a_legacy_receipt_into_original_check_proof() -> None:
    from agent_hub.runtime.original_project_progress import original_contract_binding_matches

    ctx = selected()
    source = 'd' * 64
    assert original_contract_binding_matches(ctx, {}, source) is False
    assert original_contract_binding_matches(context(), {}, source) is True
    state = ctx.routing_decision['project_input_preparation']
    binding = {'schema_version': 1, 'contract_sha256': 'b' * 64, 'protected_sha256': 'b' * 64,
        'baseline_envelope_sha256': 'c' * 64, 'baseline_source_sha256': 'a' * 64,
        'baseline_observation_sha256': state['observation_sha256'], 'candidate_source_sha256': source}
    assert original_contract_binding_matches(ctx, {'original_check_contract': binding}, source) is True
    assert original_contract_binding_matches(ctx, {'original_check_contract': {**binding,
        'schema_version': True}}, source) is False
    assert original_contract_binding_matches(ctx, {'original_check_contract': {**binding,
        'candidate_source_sha256': 'e' * 64}}, source) is False


@pytest.mark.parametrize('mode', ['crew', 'autogen'])
def test_delivery_replay_requires_original_contract_at_both_verify_and_bundle(mode: str) -> None:
    from collections.abc import Mapping

    ctx = selected()
    if mode == 'crew':
        from agent_hub.runtime.crew.engineering_delivery import (
            engineering_delivery_ready as crew_ready,
        )
        from tests.unit.runtime.crew.test_engineering_delivery_gate import change_result
        from tests.unit.runtime.crew.test_engineering_delivery_gate import fixture as crew_fixture

        data = crew_fixture()

        def check() -> bool:
            return crew_ready(step_id='build', actor='implementer', states=data[0],
                                              artifacts=data[1], context=ctx)

        def result(position: int) -> Any:
            return data[1][str(position)].content['result']

        def update(position: int, value: Any) -> None:
            change_result(data, str(position), value)
    else:
        from agent_hub.runtime.autogen.engineering_delivery import (
            engineering_delivery_ready as autogen_ready,
        )
        from tests.unit.runtime.autogen.test_engineering_delivery import RUN, TOOLS, replace_result
        from tests.unit.runtime.autogen.test_engineering_delivery import fixture as autogen_fixture

        rows, artifacts = autogen_fixture()

        def check() -> bool:
            return autogen_ready(run_id=RUN, tool_entries=rows, artifacts=artifacts,
                                              allowed_tools=TOOLS, context=ctx)

        def result(position: int) -> Any:
            return artifacts[position].content['result']

        def update(position: int, value: Any) -> None:
            replace_result(rows, artifacts, position, value)
    assert check() is False
    verified = result(1)
    assert isinstance(verified, Mapping)
    source = verified['verification']['source_sha256']
    binding = {'schema_version': 1, 'contract_sha256': 'b' * 64, 'protected_sha256': 'b' * 64,
        'baseline_envelope_sha256': 'c' * 64, 'baseline_source_sha256': 'a' * 64,
        'baseline_observation_sha256': ctx.routing_decision['project_input_preparation']['observation_sha256'],
        'candidate_source_sha256': source}
    update(1, {**verified, 'original_check_contract': binding})
    assert check() is False
    bundled = result(2)
    update(2, {**bundled, 'original_check_contract': binding})
    assert check() is True


@pytest.mark.parametrize('change', ['bool', 'unknown', 'digest', 'source', 'success'])
def test_uncertain_or_different_observation_never_suppresses_original_verification(change: str) -> None:
    from agent_hub.runtime.original_project_progress import original_failure_observed

    ctx = selected()
    route = json.loads(json.dumps(ctx.to_payload()['routing_decision']))
    state = route['project_input_preparation']
    if change == 'bool':
        route['project_input_contract_version'] = True
    elif change == 'unknown':
        state['phase'] = 'executing'
    elif change == 'digest':
        state['observation_sha256'] = '0' * 64
    elif change == 'source':
        state['reference']['input_source_sha256'] = '0' * 64
    else:
        state['observation']['status'] = 'passed'
    assert original_failure_observed(ctx.model_copy(update={'routing_decision': route})) is False
