"""Original recipe admission remains a private, non-executing Harness entry."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from agent_hub.auth.models import Role
from agent_hub.capabilities.gateway import CapabilityResult, CapabilityStatus
from agent_hub.harness.tool_gateway import HarnessToolGateway
from tests.unit.capabilities.test_toolchain_preparation import case as case  # noqa: PLC0414
from tests.unit.harness.test_toolchain_supply_gateway import NoRuntime
from tests.unit.harness.test_toolchain_supply_gateway import envelope as sdk_envelope
from tests.unit.runs.test_go_project_admission import GoDB
from tests.unit.runs.test_go_project_admission import db as db  # noqa: PLC0414


class Policy:
    def __init__(self, status: CapabilityStatus) -> None:
        self.status = status
        self.seen: list[Any] = []

    async def invoke(self, *args: Any, **kwargs: Any) -> Any:
        pytest.fail('Go recipe must not use generic invoke')

    async def authorize_project_go_recipe(self, request: Any, **kwargs: Any) -> Any:
        self.seen.append((request, kwargs))
        return CapabilityResult(self.status, request.run_id,
                                approval_id='recipe_pending' if self.status is CapabilityStatus.WAITING_APPROVAL else None)


def envelope(**patch: Any) -> Any:
    return replace(sdk_envelope(), tool_name='project.prepare_go_recipe', **patch)


@pytest.mark.parametrize('status', list(CapabilityStatus))
async def test_private_entry_uses_pure_recipe_request_without_discover_or_execute(db: GoDB, status: Any) -> None:
    policy = Policy(status)
    gateway = HarnessToolGateway(NoRuntime(), policy_gateway=policy)
    authority = db.current_authority()
    request = envelope()
    result = await gateway.authorize_project_go_recipe(db.row.tenant_id, request,
        user_id=db.row.actor_id, role=Role.OPERATOR, inputs=db.inputs, authority=authority)
    assert len(policy.seen) == 1
    admission, options = policy.seen[0]
    assert admission == db.inputs.approval_request(user_id=db.row.actor_id, agent_id='main_agent',
                                                   idempotency_key=request.idempotency_key)
    assert options == {'role': Role.OPERATOR, 'authority': authority}
    assert result.status == ('succeeded' if status is CapabilityStatus.ALLOWED else 'failed')
    assert result.payload == ({'authorized': True} if status is CapabilityStatus.ALLOWED else
                             {'approval_id': 'recipe_pending'} if status is CapabilityStatus.WAITING_APPROVAL else {})


@pytest.mark.parametrize('patch', [{'arguments': {'approved': True}}, {'actor': 'subagent'},
    {'sandbox': 'restricted'}, {'run_id': uuid4()}])
async def test_untrusted_envelope_cannot_reach_policy(db: GoDB, patch: Any) -> None:
    policy = Policy(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(NoRuntime(), policy_gateway=policy)
    result = await gateway.authorize_project_go_recipe(db.row.tenant_id, envelope(**patch),
        user_id=db.row.actor_id, role=Role.OPERATOR, inputs=db.inputs, authority=db.current_authority())
    assert result.status == 'failed' and not policy.seen


async def test_missing_authority_and_public_invoke_fail_closed(db: GoDB) -> None:
    policy = Policy(CapabilityStatus.ALLOWED)
    gateway = HarnessToolGateway(NoRuntime(), policy_gateway=policy)
    assert (await gateway.authorize_project_go_recipe(db.row.tenant_id, envelope(), user_id=db.row.actor_id,
            role=Role.OPERATOR, inputs=db.inputs)).status == 'failed'
    assert (await gateway.invoke(db.row.tenant_id, envelope(), user_id=db.row.actor_id,
                                  role=Role.OPERATOR)).status == 'failed'
    assert not policy.seen
