"""v8 Go execution/inspection identities, distinct from v7 SDK supply.

These envelopes grant no authority. Root must rebuild GoProjectInputs from the
captured baseline and its catalog, check live approval, and measure the SDK.
Requests contain no model-selected paths, commands, flags, URLs or input maps.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any, Literal, Self, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.project import (
    ContractProjectInvocation,
    OriginalProjectReference,
    ProjectInvocation,
)
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


class _ReceiptData(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class GoProjectReceiptBinary(_ReceiptData):
    filename: Literal['project']
    sha256: Digest
    size: int = Field(ge=1, le=2**63 - 1)
    format: Literal['ELF64']
    target: Literal['linux_amd64']


class GoProjectReceiptCheck(_ReceiptData):
    phase: Literal['inspect', 'build', 'test', 'canary', 'business']
    status: Literal['passed']


class GoProjectReceiptObservation(_ReceiptData):
    case_id: str
    criterion_id: str
    case_sha256: Digest
    expected_exit: int = Field(ge=0, le=255)
    exit_code: int = Field(ge=0, le=255)
    timed_out: Literal[False]
    stdout_sha256: Digest
    stderr_sha256: Digest
    stdout_observed_bytes: int = Field(ge=0, le=2**63 - 1)
    stderr_observed_bytes: int = Field(ge=0, le=2**63 - 1)
    stdout_truncated: Literal[False]
    stderr_truncated: Literal[False]
    stdout_utf8_complete: Literal[True]
    stderr_utf8_complete: Literal[True]
    stdout_matches: Literal[True]
    stderr_matches: Literal[True]
    passed: Literal[True]
    reason: None

    @field_validator(
        'timed_out', 'stdout_truncated', 'stderr_truncated', 'stdout_utf8_complete',
        'stderr_utf8_complete', 'stdout_matches', 'stderr_matches', 'passed', mode='before',
    )
    @classmethod
    def boolean_flags(cls, value: object) -> object:
        _need(type(value) is bool)
        return value

    @model_validator(mode='after')
    def passed_exit(self) -> Self:
        _need(self.exit_code == self.expected_exit)
        return self


class GoProjectReceipt(_ReceiptData):
    """Inert passed-receipt data, never Root provenance or approval.

    The backend must independently reconstruct Inputs and measure source and
    binary before calling capabilities.go_project_receipts validation.
    """

    schema_version: Literal[1]
    kind: Literal['go_project_execution_receipt']
    status: Literal['passed']
    execution_id: ExecutionId
    scope: ToolchainPlanScope
    source_kind: Literal['original', 'candidate']
    source_sha256: Digest
    recipe_sha256: Digest
    environment_input_sha256: Digest
    original_reference: OriginalProjectReference
    original_check_contract: Digest
    toolchain: ToolchainReference
    acceptance_sha256: Digest
    business_suite_sha256: Digest
    binary: GoProjectReceiptBinary
    checks: tuple[GoProjectReceiptCheck, ...] = Field(min_length=5, max_length=5)
    named_tests: tuple[str, ...] = Field(min_length=1)
    business_evidence: tuple[GoProjectReceiptObservation, ...] = Field(min_length=1)
    verified_at: str = Field(pattern=(
        r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
        r'(?:\.[0-9]+)?(?:Z|\+00:00)$'
    ))

    @field_validator('schema_version', mode='before')
    @classmethod
    def integer_schema(cls, value: object) -> object:
        _need(type(value) is int)
        return value

    @field_validator('scope', mode='before')
    @classmethod
    def serialized_scope(cls, value: object) -> object:
        # Backend receipts carry JSON UUID strings; keep all other values strict.
        if isinstance(value, dict):
            value = {key: str(item) if isinstance(item, UUID) else item
                     for key, item in value.items()}
            return ToolchainPlanScope.model_validate_json(json.dumps(value), strict=True)
        return value

    @field_validator('checks', 'named_tests', 'business_evidence', mode='before')
    @classmethod
    def immutable_arrays(cls, value: object) -> object:
        _need(type(value) in (list, tuple))
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode='after')
    def passed_shape(self) -> Self:
        _checked_scope(self.scope)
        _need(self.original_reference.model_fields_set == OriginalProjectReference.model_fields.keys()
              and self.toolchain.model_fields_set == ToolchainReference.model_fields.keys())
        _need((self.scope.source_sha256, self.scope.contract_sha256)
              == (self.original_reference.input_source_sha256,
                  self.original_reference.contract_sha256))
        if self.source_kind == 'original':
            _need(self.source_sha256 == self.scope.source_sha256)
        _need(tuple(check.phase for check in self.checks)
              == ('inspect', 'build', 'test', 'canary', 'business'))
        _need(len(set(self.named_tests)) == len(self.named_tests))
        _need(len({row.case_id for row in self.business_evidence}) == len(self.business_evidence)
              and any(row.expected_exit == 0 for row in self.business_evidence)
              and any(row.expected_exit != 0 for row in self.business_evidence))
        datetime.fromisoformat(self.verified_at)
        return self


def go_project_response_limit(
    *, diagnostic_limit_bytes: int = GO_PROJECT_DIAGNOSTIC_MAX_BYTES,
) -> int:
    """Wire cap: bounded metadata plus worst-case six-byte JSON escapes.

    The shared budget covers UTF-8 stdout/stderr (including the GoReport) plus
    the receipt's complete ASCII JSON bytes. Six times this combined budget
    bounds both escaped diagnostics and UTF-8/ASCII receipt representations.
    It is independent of the invocation's per-phase output_limit_bytes.
    Transports can select a smaller budget and use this same cap for framing.
    """
    _need(type(diagnostic_limit_bytes) is int
          and 1 <= diagnostic_limit_bytes <= GO_PROJECT_DIAGNOSTIC_MAX_BYTES)
    return GO_PROJECT_REQUEST_MAX_BYTES + 6 * diagnostic_limit_bytes


def _diagnostic_bytes(result: SkillResult | None) -> int:
    return 0 if result is None else len(result.stdout.encode('utf-8')) + len(result.stderr.encode('utf-8'))


def _receipt_bytes(receipt: GoProjectReceipt | None) -> int:
    if receipt is None:
        return 0
    # ASCII JSON also bounds UTF-8 JSON, including supplementary characters.
    return len(json.dumps(receipt.model_dump(mode='json'), ensure_ascii=True,
                          separators=(',', ':'), allow_nan=False).encode('ascii'))


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
    receipt: GoProjectReceipt | None = None

    @model_serializer(mode='wrap')
    def compatible_wire(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        result = cast(dict[str, Any], handler(self))
        if self.receipt is None:
            result.pop('receipt', None)
        return result

    @model_validator(mode='after')
    def exclusive_outcome(self) -> Self:
        _checked_scope(self.scope)
        _need(_diagnostic_bytes(self.result) + _receipt_bytes(self.receipt)
              <= GO_PROJECT_DIAGNOSTIC_MAX_BYTES)
        if self.receipt is not None:
            _need(self.ok and self.action == 'run_go_project' and self.result is not None)
            assert self.result is not None
            _need(self.result.exit_code == 0 and not self.result.timed_out)
            _need((self.receipt.execution_id, self.receipt.scope, self.receipt.recipe_sha256,
                   self.receipt.environment_input_sha256, self.receipt.toolchain)
                  == (self.execution_id, self.scope, self.recipe_sha256,
                      self.environment_input_sha256, self.toolchain), 'go_project_scope_changed')
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
        if checked.receipt is not None:
            _need(checked.receipt.source_kind == invocation.source_kind
                  and checked.receipt.original_reference == invocation.original_reference,
                  'go_project_scope_changed')
            # candidate's actual source hash is known only at the Root boundary.
            if invocation.source_kind == 'original':
                _need(checked.receipt.source_sha256 == invocation.scope.source_sha256,
                      'go_project_scope_changed')


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
        _need(_diagnostic_bytes(response.result) + _receipt_bytes(response.receipt)
              <= diagnostic_limit_bytes)
        return response
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError('go_project_request_invalid') from None
