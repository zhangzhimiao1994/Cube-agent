"""Preserve validated Root dependency failures through real authorization.

Only Root exchanges/platform gates are simulated; runtime, service, gateway and
client DTO validation stay real. This first step does not define a bootstrap API
or prove native Root/cache behavior. A valid index with cold wheels must retain
the existing post-capture input-supply and separate recipe-approval flow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityStatus
from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
from agent_hub.harness.tool_gateway import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest
from agent_hub.skills.sandbox import python_dependency_protocol as dependency
from agent_hub.skills.sandbox.python_dependency_protocol import (
    PythonDependencyClosureError,
    PythonDependencyClosureFailure,
    PythonDependencyClosureRequest,
    PythonDependencyClosureResponse,
)
from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileScope
from tests.unit.capabilities.test_generated_project_service import USER
from tests.unit.capabilities.test_generated_python_dependency_closure import (
    draft as draft,  # noqa: PLC0414 - Register the existing generated Python fixture.
)
from tests.unit.capabilities.test_python_dependency_capture_guards import (
    FakeRootTransport,
    _correct_lock,
)
from tests.unit.capabilities.test_scoped_read import RUN, TENANT
from tests.unit.harness.test_tool_gateway import FakePolicyGateway
from tests.unit.skills.test_python_dependency_protocol import (
    CATALOG,
    SOURCE,
    Wire,
    encode,
    invocation,
    payload,
)

ERRORS: tuple[PythonDependencyClosureError, ...] = (
    'python_dependency_unavailable',
    'python_dependency_metadata_unavailable',
    'python_dependency_metadata_untrusted',
    'python_dependency_catalog_changed',
    'python_dependency_draft_changed',
    'python_dependency_declaration_invalid',
)


class MissingMetadataIndexTransport(FakeRootTransport):
    error: PythonDependencyClosureError = 'python_dependency_metadata_unavailable'

    async def dependencies(
        self, request: PythonDependencyClosureRequest, **kwargs: object,
    ) -> PythonDependencyClosureResponse:
        self.calls.append(request)
        self._read_only(request.invocation)
        return PythonDependencyClosureResponse(
            scope=PythonProjectProfileScope.for_invocation(request.invocation),
            ok=False,
            error=self.error,
        )


def _gateway(draft: Any, tmp_path: Path) -> tuple[
    HarnessToolGateway, FakePolicyGateway, HarnessToolCallRequest,
]:
    service, repo, _ = draft
    runtime = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / 'skills',
        project_workspace_dir=service.root,
        generated_artifact_dir=tmp_path / 'artifacts',
        run_repository=repo,
    )
    policy = FakePolicyGateway(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(runtime, policy_gateway=policy)
    call = HarnessToolCallRequest(
        run_id=RUN, actor='engineer', tool_name='project.verify', arguments={},
        sandbox='workspace_write', approval_required=True,
        idempotency_key='missing-root-metadata-before-capture',
    )

    return gateway, policy, call


def _assert_uncaptured(draft: Any, policy: FakePolicyGateway) -> None:
    service, repo, _ = draft
    assert repo.updates == 0
    assert 'generated_project_preparation' not in repo.row.routing_decision
    assert not (service.root / '.project-contracts' / str(TENANT) / str(RUN)).exists()
    assert not policy.requests, 'Metadata failure must not request an install grant'


@pytest.mark.parametrize('code', ERRORS)
async def test_missing_root_metadata_reason_survives_gateway_before_capture(
    draft: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    code: PythonDependencyClosureError,
) -> None:
    _correct_lock(draft)
    # Existing transport supplies a valid Root profile with one catalog entry.
    transport = MissingMetadataIndexTransport(draft, monkeypatch)
    transport.error = code
    gateway, policy, call = _gateway(draft, tmp_path)

    result = await gateway.invoke(TENANT, call, user_id=USER, role=Role.OPERATOR)

    assert result.status == 'failed'
    _assert_uncaptured(draft, policy)
    assert [request.action for request in transport.calls] == [
        'inspect_python_project_profile', 'inspect_python_dependency_closure',
    ]
    assert result.failure_reason == code, (
        'Preserve the finite Root reason for metadata-only preparation; '
        f'got {result.failure_reason!r} with payload {dict(result.payload)!r}'
    )


@pytest.mark.parametrize('code', ERRORS)
async def test_sdk_scoped_root_failure_uses_typed_exception(
    monkeypatch: pytest.MonkeyPatch, code: PythonDependencyClosureError,
) -> None:
    wire = Wire(monkeypatch)
    wire.feed(encode(payload() | {'ok': False, 'error': code, 'observation': None}))
    with pytest.raises(ValueError) as caught:
        await dependency.PythonDependencyClosureSandbox().inspect(
            invocation(), expected_source_sha256=SOURCE, expected_catalog_sha256=CATALOG,
        )
    assert type(caught.value) is PythonDependencyClosureFailure
    assert caught.value.code == code
    assert str(caught.value) == code
    assert wire.writer.closed and wire.writer.waited


@pytest.mark.parametrize('value', [
    'python_dependency_unknown', '/private/secret token=secret', None, True,
])
def test_typed_failure_rejects_non_protocol_codes(value: Any) -> None:
    with pytest.raises(ValueError, match='^python_dependency_response_invalid$') as caught:
        PythonDependencyClosureFailure(value)
    assert type(caught.value) is ValueError


@pytest.mark.parametrize('exception_type', [ValueError, OSError])
@pytest.mark.parametrize('text', (*ERRORS, '/private/secret token=secret'))
async def test_transport_exception_text_cannot_impersonate_root_diagnostic(
    draft: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    exception_type: type[Exception], text: str,
) -> None:
    _correct_lock(draft)
    MissingMetadataIndexTransport(draft, monkeypatch)

    async def spoof(
        request: PythonDependencyClosureRequest, **kwargs: object,
    ) -> PythonDependencyClosureResponse:
        raise exception_type(text)

    monkeypatch.setattr(dependency, 'request_python_dependency_closure_broker', spoof)
    gateway, policy, call = _gateway(draft, tmp_path)
    result = await gateway.invoke(TENANT, call, user_id=USER, role=Role.OPERATOR)

    _assert_uncaptured(draft, policy)
    assert result.status == 'failed'
    assert result.failure_reason == 'generated_acceptance_unavailable_or_changed'
    assert not result.payload


@pytest.mark.parametrize('field', [
    'tenant_id', 'run_id', 'project_id', 'session_id', 'execution_id',
])
async def test_foreign_scope_error_never_becomes_typed_root_diagnostic(
    draft: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    _correct_lock(draft)
    transport = MissingMetadataIndexTransport(draft, monkeypatch)

    async def foreign(
        request: PythonDependencyClosureRequest, **kwargs: object,
    ) -> PythonDependencyClosureResponse:
        response = await transport.dependencies(request)
        value: object = UUID(int=123) if field in {'tenant_id', 'run_id'} else 'foreign'
        return response.model_copy(update={
            'scope': response.scope.model_copy(update={field: value}),
        })

    monkeypatch.setattr(dependency, 'request_python_dependency_closure_broker', foreign)
    gateway, policy, call = _gateway(draft, tmp_path)
    result = await gateway.invoke(TENANT, call, user_id=USER, role=Role.OPERATOR)

    _assert_uncaptured(draft, policy)
    assert result.status == 'failed'
    assert result.failure_reason == 'generated_acceptance_unavailable_or_changed'
    assert not result.payload


@pytest.mark.parametrize('field', [
    'tenant_id', 'run_id', 'project_id', 'session_id', 'execution_id',
])
async def test_sdk_rejects_foreign_scope_before_classifying_root_failure(
    monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    wire = Wire(monkeypatch)
    body = payload() | {
        'ok': False, 'error': 'python_dependency_metadata_unavailable', 'observation': None,
    }
    body['scope'][field] = str(UUID(int=123)) if field in {'tenant_id', 'run_id'} else 'foreign'
    wire.feed(encode(body))
    with pytest.raises(ValueError, match='^python_dependency_response_invalid$') as caught:
        await dependency.PythonDependencyClosureSandbox().inspect(
            invocation(), expected_source_sha256=SOURCE, expected_catalog_sha256=CATALOG,
        )
    assert type(caught.value) is ValueError
    assert wire.writer.closed and wire.writer.waited
