"""Trusted negative-test overlay data, never execution or business authority.

The outer runner authenticates the SDK and complete source, rejects TestMain,
checks absence before mounting, and seals both files. A random name reduces
accidental collision; it is not a secret or proof against malicious project init
code forging test2json framing. Native outcome and independent business checks
remain separate required gates.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Collection
from dataclasses import dataclass

from agent_hub.capabilities.go_project_recipe import _module_path
from agent_hub.skills.sandbox.go_project_evidence import (
    GoCanaryIdentity,
    GoPackageTests,
    GoTestExpectation,
)


def _need(value: object) -> None:
    if not value:
        raise ValueError('go_canary_contract_invalid')


@dataclass(frozen=True, slots=True)
class GoOverlayCanary:
    project_execution_id: str
    execution_id: str
    module_path: str
    nonce: str
    source_sha256: str
    protected_sha256: str

    def __post_init__(self) -> None:
        for execution in (self.project_execution_id, self.execution_id):
            _need(type(execution) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',
                                                        execution) is not None)
        _need(self.project_execution_id != self.execution_id)
        _module_path(self.module_path)
        _need(not self.module_path.startswith('-'))
        for value in (self.nonce, self.source_sha256, self.protected_sha256):
            _need(type(value) is str and re.fullmatch(r'[a-f0-9]{64}', value) is not None)

    @property
    def source_path(self) -> str:
        return 'zz_agent_hub_canary_' + self.nonce + '_test.go'

    @property
    def test_name(self) -> str:
        return 'TestAgentHubCanary' + self.nonce

    @property
    def source_bytes(self) -> bytes:
        return ('package main\nimport "testing"\nfunc ' + self.test_name
                + '(t *testing.T) { t.Fatal("agent_hub_expected_negative_canary") }\n').encode()

    @property
    def overlay_bytes(self) -> bytes:
        return json.dumps({'Replace': {'/workspace/' + self.source_path: '/trusted/go-canary.go'}},
                          sort_keys=True, separators=(',', ':')).encode()

    @property
    def command(self) -> tuple[str, ...]:
        return ('/sdk/bin/go', 'test', '-json', '-count=1', '-vet=off', '-mod=readonly',
                '-overlay=/trusted/go-canary-overlay.json', '-run=^' + self.test_name + '$',
                self.module_path)

    @property
    def identity(self) -> GoCanaryIdentity:
        return GoCanaryIdentity(self.project_execution_id, self.execution_id,
            GoTestExpectation((GoPackageTests(self.module_path, (self.test_name,)),),
                              self.source_sha256, self.protected_sha256))

    def require_absent(self, source_paths: Collection[str]) -> None:
        _need(not isinstance(source_paths, (str, bytes)))
        needle = self.source_path.casefold()
        for path in source_paths:
            _need(type(path) is str and path.casefold().split('/', 1)[0] != needle)


def create_go_overlay_canary(*, project_execution_id: str, execution_id: str,
    module_path: str, source_sha256: str, protected_sha256: str,
    source_paths: Collection[str],
) -> GoOverlayCanary:
    probe = GoOverlayCanary(project_execution_id, execution_id, module_path,
                            secrets.token_hex(32), source_sha256, protected_sha256)
    probe.require_absent(source_paths)
    return probe
