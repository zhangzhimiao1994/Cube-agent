"""Closed trusted overlay contract; no host project execution in unit tests."""

from __future__ import annotations

import importlib
import importlib.util
import json
from dataclasses import FrozenInstanceError
from typing import Any

import pytest


def test_canary_contract_exists() -> None:
    assert importlib.util.find_spec('agent_hub.skills.sandbox.go_project_canary') is not None


@pytest.fixture
def api() -> Any:
    name = 'agent_hub.skills.sandbox.go_project_canary'
    if importlib.util.find_spec(name) is None:
        pytest.skip('Go negative overlay contract missing')
    return importlib.import_module(name)


def parameters(api: Any, **changes: object) -> Any:
    return api.GoOverlayCanary(
        project_execution_id='go-project-1', execution_id='go-canary-1',
        module_path='example.invalid/app', nonce='a' * 64,
        source_sha256='b' * 64, protected_sha256='c' * 64, **changes,
    )


def test_overlay_has_exact_virtual_paths_and_no_existing_source_edits(api: Any) -> None:
    probe = parameters(api)
    assert probe.source_path == 'zz_agent_hub_canary_' + 'a' * 64 + '_test.go'
    assert probe.test_name == 'TestAgentHubCanary' + 'a' * 64
    assert json.loads(probe.overlay_bytes) == {'Replace': {
        '/workspace/' + probe.source_path: '/trusted/go-canary.go',
    }}
    source = probe.source_bytes.decode()
    assert source.startswith('package main\nimport "testing"\n')
    assert 'func ' + probe.test_name + '(t *testing.T)' in source
    assert 't.Fatal(' in source and 'go-canary-1' not in source
    assert probe.command == (
        '/sdk/bin/go', 'test', '-json', '-count=1', '-vet=off', '-mod=readonly',
        '-overlay=/trusted/go-canary-overlay.json', '-run=^' + probe.test_name + '$',
        'example.invalid/app',
    )
    identity = probe.identity
    assert identity.execution_id == 'go-canary-1'
    assert identity.project_execution_id == 'go-project-1'
    assert identity.expected.packages[0].package == 'example.invalid/app'
    assert identity.expected.packages[0].tests == (probe.test_name,)
    assert identity.expected.source_sha256 == 'b' * 64
    assert identity.expected.protected_sha256 == 'c' * 64
    with pytest.raises(FrozenInstanceError):
        probe.nonce = 'd' * 64


@pytest.mark.parametrize('field,value', [
    ('project_execution_id', ''), ('project_execution_id', 'x\nsecret'),
    ('execution_id', 'go-project-1'), ('execution_id', '../escape'),
    ('nonce', 'a' * 63), ('nonce', 'A' * 64), ('nonce', True),
    ('module_path', '-run=None'), ('module_path', '-race'),
    ('module_path', '-exec'), ('module_path', 'app/../secret'),
    ('source_sha256', 'invalid'), ('protected_sha256', 'D' * 64),
])
def test_invalid_identity_and_shell_fragments_are_rejected(api: Any, field: str, value: object) -> None:
    payload = {'project_execution_id': 'go-project-1', 'execution_id': 'go-canary-1',
        'module_path': 'example.invalid/app', 'nonce': 'a' * 64,
        'source_sha256': 'b' * 64, 'protected_sha256': 'c' * 64}
    payload[field] = value
    with pytest.raises(ValueError):
        api.GoOverlayCanary(**payload)


def test_collision_rejected_against_complete_caller_snapshot(api: Any) -> None:
    probe = parameters(api)
    probe.require_absent({'main.go', 'main_test.go'})
    for existing in ({probe.source_path}, {probe.source_path.upper()},
                     {probe.source_path + '/nested.go'}):
        with pytest.raises(ValueError):
            probe.require_absent(existing)


def test_factory_uses_distinct_nonce_and_binds_only_caller_identity(api: Any) -> None:
    options = {'project_execution_id': 'project', 'execution_id': 'canary',
        'module_path': 'example.invalid/app', 'source_sha256': 'b' * 64,
        'protected_sha256': 'c' * 64}
    first = api.create_go_overlay_canary(source_paths=('main.go',), **options)
    second = api.create_go_overlay_canary(source_paths=('main.go',), **options)
    assert first.nonce != second.nonce
    assert first.command[-1] == second.command[-1] == 'example.invalid/app'
