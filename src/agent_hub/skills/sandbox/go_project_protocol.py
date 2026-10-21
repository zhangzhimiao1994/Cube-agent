"""v8 Go execution/inspection identities, distinct from v7 SDK supply.

These envelopes grant no authority. Root must rebuild GoProjectInputs from the
captured baseline and its catalog, check live approval, and measure the SDK.
Requests contain no model-selected paths, commands, flags, URLs or input maps.
"""

from __future__ import annotations

import json
import math
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import ContractProjectInvocation, ProjectInvocation
from agent_hub.skills.sandbox.toolchain_catalog import ToolchainPlanScope
from agent_hub.skills.sandbox.toolchain_protocol import Digest, ExecutionId, ToolchainReference

GO_PROJECT_REQUEST_MAX_BYTES = 512 * 1024
GO_PROJECT_DIAGNOSTIC_MAX_BYTES = 32 * 1024**2
GoProjectAction = Literal['run_go_project', 'inspect_go_project_environment']
GoProjectError = Literal[
    'go_project_request_invalid',
    'go_project_unsupported',
    'go_project_scope_changed',
    'go_project_source_unavailable',
    'go_project_protected_input_changed',
    'go_project_recipe_unavailable',
    'go_project_recipe_changed',
    'go_project_recipe_approval_required',
    'go_project_toolchain_unavailable',
    'go_project_toolchain_changed',
    'go_project_environment_unavailable',
    'go_project_environment_changed',
    'go_project_capacity',
    'go_project_timeout',
    'go_project_unknown',
    'go_project_execution_unavailable',
    'go_project_cleanup_incomplete',
]


def _need(condition: object, code: str = 'go_project_request_invalid') -> None:
    if not condition:
        raise ValueError(code)


def _checked_scope(scope: ToolchainPlanScope) -> ToolchainPlanScope:
    checked = ToolchainPlanScope.from_json(scope.to_json())
    _need(checked.actor_id == 'main_agent'
          and checked.actor_role in {'operator', 'admin', 'super_admin'}, 'go_project_scope_changed')
    return checked


class GoProjectInvocation(ContractProjectInvocation):
    output_limit_bytes: int = Field(default=262144, ge=1, le=GO_PROJECT_DIAGNOSTIC_MAX_BYTES)
    scope: ToolchainPlanScope
    recipe_sha256: Digest
    environment_input_sha256: Digest
    toolchain: ToolchainReference
    install_allowed: Literal[False] = False

    @field_validator('install_allowed', mode='before')
    @classmethod
    def no_install_authority(cls, value: object) -> object:
        _need(value is False)
        return value

    @model_validator(mode='after')
    def captured_scope(self) -> Self:
        scope = _checked_scope(self.scope)
        _need(
            self.sandbox_profile == 'workspace_write'
            and (scope.tenant_id, scope.run_id, scope.project_id, scope.session_id,
                 scope.source_sha256, scope.contract_sha256)
            == (self.tenant_id, self.run_id, self.project_id, self.session_id,
                self.original_reference.input_source_sha256, self.original_reference.contract_sha256),
            'go_project_scope_changed',
        )
        return self


class _Envelope(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)

    version: Literal[8] = 8

    @field_validator('version', mode='before')
    @classmethod
    def integer_version(cls, value: object) -> object:
        _need(type(value) is int)
        return value


class GoProjectRequest(_Envelope):
    action: GoProjectAction
    invocation: GoProjectInvocation


def go_project_response_limit(
    *, diagnostic_limit_bytes: int = GO_PROJECT_DIAGNOSTIC_MAX_BYTES,
) -> int:
    """Wire cap: bounded metadata plus worst-case six-byte JSON escapes.

    The raw diagnostic budget covers combined UTF-8 stdout/stderr (including
    the GoReport), not the old invocation's per-phase output_limit_bytes.
    Transports can select a smaller budget and use this same cap for framing.
    """
    _need(type(diagnostic_limit_bytes) is int
          and 1 <= diagnostic_limit_bytes <= GO_PROJECT_DIAGNOSTIC_MAX_BYTES)
    return GO_PROJECT_REQUEST_MAX_BYTES + 6 * diagnostic_limit_bytes


def _diagnostic_bytes(result: SkillResult | None) -> int:
    return 0 if result is None else len(result.stdout.encode('utf-8')) + len(result.stderr.encode('utf-8'))


class GoProjectResponse(_Envelope):
    action: GoProjectAction
    execution_id: ExecutionId
    scope: ToolchainPlanScope
    recipe_sha256: Digest
    environment_input_sha256: Digest
    toolchain: ToolchainReference
    ok: bool
    result: SkillResult | None = None
    environment: ToolchainReference | None = None
    error: GoProjectError | None = None

    @model_validator(mode='after')
    def exclusive_outcome(self) -> Self:
        _checked_scope(self.scope)
        _need(_diagnostic_bytes(self.result) <= GO_PROJECT_DIAGNOSTIC_MAX_BYTES)
        if not self.ok:
            _need(self.error is not None and self.result is None and self.environment is None)
        else:
            _need(self.error is None)
            if self.action == 'run_go_project':
                _need(self.result is not None and self.environment is None)
            else:
                _need(self.result is None and self.environment is not None
                      and self.environment == self.toolchain)
        return self

    def require_for(self, request: GoProjectRequest) -> None:
        """Bind every reply, including errors, to the authenticated request."""
        _need(type(request) is GoProjectRequest and type(self) is GoProjectResponse)
        checked_request = parse_go_project_request(request.model_dump_json(warnings='error').encode())
        checked = parse_go_project_response(self.model_dump_json(warnings='error').encode())
        invocation = checked_request.invocation
        _need(
            (checked.action, checked.execution_id, checked.scope, checked.recipe_sha256,
             checked.environment_input_sha256, checked.toolchain)
            == (checked_request.action, invocation.execution_id, invocation.scope,
                invocation.recipe_sha256, invocation.environment_input_sha256, invocation.toolchain),
            'go_project_scope_changed',
        )


def base_go_invocation(invocation: GoProjectInvocation) -> ProjectInvocation:
    """Source-staging projection only, never a Go execution invocation.

    Legacy source helpers need no diagnostic stream. Keep their accepted wire
    budget without changing the separately validated Go runner's allocation.
    All source/scope/storage/time/CPU fields remain exact. Do not use this value
    to build a command or to validate the Go result/receipt.
    """
    _need(type(invocation) is GoProjectInvocation)
    checked = GoProjectInvocation.model_validate_json(
        invocation.model_dump_json(warnings='error'), strict=True,
    )
    return ProjectInvocation.model_validate(
        {name: getattr(checked, name) for name in ProjectInvocation.model_fields}
        | {'output_limit_bytes': min(checked.output_limit_bytes, 1024**2)}, strict=True,
    )


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        _need(name not in result)
        result[name] = value
    return result


def _constant(value: str) -> object:
    raise ValueError('go_project_request_invalid')


def _float(value: str) -> float:
    number = float(value)
    _need(math.isfinite(number))
    return number


def _json(raw: bytes, maximum: int) -> None:
    _need(type(raw) is bytes and 0 < len(raw) <= maximum)
    _need(type(json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant,
                          parse_float=_float)) is dict)


def parse_go_project_request(raw: bytes) -> GoProjectRequest:
    try:
        _json(raw, GO_PROJECT_REQUEST_MAX_BYTES)
        return GoProjectRequest.model_validate_json(raw, strict=True)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError('go_project_request_invalid') from None


def parse_go_project_response(
    raw: bytes, *, diagnostic_limit_bytes: int = GO_PROJECT_DIAGNOSTIC_MAX_BYTES,
) -> GoProjectResponse:
    try:
        _json(raw, go_project_response_limit(diagnostic_limit_bytes=diagnostic_limit_bytes))
        response = GoProjectResponse.model_validate_json(raw, strict=True)
        _need(_diagnostic_bytes(response.result) <= diagnostic_limit_bytes)
        return response
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError('go_project_request_invalid') from None
