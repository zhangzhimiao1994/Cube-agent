"""Inventory discovery must not turn planning into duplicate execution."""

from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.runtime import defaults
from agent_hub.runtime.contracts import JsonValue, TaskContext
from agent_hub.runtime.role_planner import (
    RoleAssignment,
    RolePlanner,
    RolePlanningRequest,
    RolePurpose,
    TaskProfile,
)

CAPABILITY = "fixture.stats_once"
ALIAS = "fixture_stats"
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("11111111-1111-4111-8111-111111111111")


class InventoryGateway:
    def __init__(self, kind: str, *, available: bool = True) -> None:
        self.kind = kind
        self.available = available

    def capability_manifest(self, tenant_id: UUID) -> Mapping[str, JsonValue]:
        assert tenant_id == TENANT_ID
        item: dict[str, JsonValue] = {
            "id": CAPABILITY,
            "kind": self.kind,
            "available": self.available,
            "replay_safe": True,
            "aliases": (ALIAS,),
        }
        return {"schema_version": 1, "capabilities": (item,)}

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        assert tenant_id == TENANT_ID
        return name == "read_context" or (name == CAPABILITY and self.available)

    def is_replay_safe(self, name: str) -> bool:
        return name == "read_context" or (name == CAPABILITY and self.available)

    async def execute(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        raise AssertionError("Discovery must not invoke a capability")


def _context(request: str) -> TaskContext:
    return TaskContext(
        run_id=RUN_ID, tenant_id=TENANT_ID, mode=TaskMode.DISPATCH, request=request,
    )


def _role(purpose: RolePurpose) -> RoleAssignment:
    return RoleAssignment(
        id="candidate", role="Candidate", purpose=purpose,
        mission="Contribute the assigned result.", must_answer=("What is the result?",),
        allowed_tools=("read_context",), forbidden_actions=("Do not expand scope.",),
        skills=(), output_schema={"summary": "string"}, model="main",
    )


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
def test_real_general_dispatch_has_only_project_manager_inventory_owner(kind: str) -> None:
    task = f'Call {CAPABILITY} exactly once with {{"text":"owned fixture"}}. Return the tool result briefly.'
    roles = RolePlanner().plan(RolePlanningRequest(
        task=task, mode=TaskMode.DISPATCH, profile=TaskProfile.GENERAL, default_model="main",
    )).roles
    assert {"planner", "project_manager"} <= {role.id for role in roles}
    plan = defaults._dispatch_plan(roles, _context(task), capability_gateway=InventoryGateway(kind))
    owners = {step.agent for step in plan.steps if CAPABILITY in step.tools}
    # Reject zero owners as well as the observed planner + project_manager duplication.
    assert owners == {"project_manager"}


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
@pytest.mark.parametrize("purpose", list(RolePurpose))
def test_independent_role_helper_keeps_legacy_request_discovery(
    kind: str, purpose: RolePurpose,
) -> None:
    role = _role(purpose)
    tools = defaults._role_allowed_tools(
        role, _context(f"Use {CAPABILITY}."), capability_gateway=InventoryGateway(kind),
    )
    expected = not defaults._is_post_product_role(role)
    assert (CAPABILITY in tools) is expected
    assert "read_context" in tools


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
@pytest.mark.parametrize("purpose", [RolePurpose.PLAN, RolePurpose.VERIFY])
@pytest.mark.parametrize("assignment", ["mission", "tools", "skills"])
def test_own_explicit_inventory_assignment_survives(
    kind: str, purpose: RolePurpose, assignment: str,
) -> None:
    role = _role(purpose)
    role = replace(
        role,
        mission=f"Use {CAPABILITY}." if assignment == "mission" else role.mission,
        allowed_tools=("read_context", CAPABILITY) if assignment == "tools" else role.allowed_tools,
        skills=(ALIAS,) if assignment == "skills" else (),
    )
    tools = defaults._role_allowed_tools(
        role, _context("Review the recorded result."), capability_gateway=InventoryGateway(kind),
        include_request=False,
    )
    assert CAPABILITY in tools
    assert "read_context" in tools


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
@pytest.mark.parametrize("token", [CAPABILITY, ALIAS])
def test_execute_discovers_exact_token_or_alias(kind: str, token: str) -> None:
    tools = defaults._role_allowed_tools(
        _role(RolePurpose.EXECUTE), _context(f"Use {token}."),
        capability_gateway=InventoryGateway(kind),
    )
    assert CAPABILITY in tools


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
@pytest.mark.parametrize("token", [f"{CAPABILITY}.details", f"{CAPABILITY}_extra", f"{ALIAS}_extra"])
def test_execute_rejects_partial_namespace_matches(kind: str, token: str) -> None:
    tools = defaults._role_allowed_tools(
        _role(RolePurpose.EXECUTE), _context(f"Use {token}."),
        capability_gateway=InventoryGateway(kind),
    )
    assert tools == ("read_context",)


def _assert_collection_owners(
    roles: tuple[RoleAssignment, ...], kind: str, expected: set[str],
    *, token: str = CAPABILITY, available: bool = True,
) -> None:
    context = _context(f"Use {token} exactly once.")
    gateway = InventoryGateway(kind, available=available)
    by_role = defaults._role_tools_by_id(roles, context, capability_gateway=gateway)
    dispatch = defaults._dispatch_plan(roles, context, capability_gateway=gateway)
    discussion = defaults._discussion_plan(roles, "main", context, capability_gateway=gateway)
    assert {role_id for role_id, tools in by_role.items() if CAPABILITY in tools} == expected
    assert {step.agent for step in dispatch.steps if CAPABILITY in step.tools} == expected
    assert {participant.id for participant in discussion.participants if CAPABILITY in participant.allowed_tools} == expected
    union = defaults._plan_allowed_tools(roles, context, capability_gateway=gateway)
    assert (CAPABILITY in union) is bool(expected)
    assert (CAPABILITY in dispatch.allowed_tools) is bool(expected)
    assert all("read_context" in tools for tools in by_role.values())


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
@pytest.mark.parametrize("ordering", [("plan", "execute", "execute", "expertise"), ("expertise", "plan", "verify")])
def test_collection_owner_prefers_first_execute_otherwise_last_producer(
    kind: str, ordering: tuple[str, ...],
) -> None:
    roles = tuple(replace(_role(RolePurpose(purpose)), id=f"custom_{index}") for index, purpose in enumerate(ordering))
    expected = {"custom_1"}
    _assert_collection_owners(roles, kind, expected)


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
def test_real_software_role_planner_prefers_implementer(kind: str) -> None:
    task = f"Implement software using {CAPABILITY}."
    roles = RolePlanner().plan(RolePlanningRequest(
        task=task, mode=TaskMode.DISPATCH, profile=TaskProfile.SOFTWARE, default_model="main",
    )).roles
    first_execute = next(role for role in roles if role.purpose is RolePurpose.EXECUTE)
    assert first_execute.id == "implementer"
    _assert_collection_owners(roles, kind, {first_execute.id})


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
@pytest.mark.parametrize("assignment", ["mission", "tools", "skills"])
def test_multiple_explicit_assignments_are_not_deduplicated(kind: str, assignment: str) -> None:
    roles = tuple(replace(
        _role(purpose), id=f"explicit_{index}",
        mission=f"Use {CAPABILITY}." if assignment == "mission" else "Review recorded evidence.",
        allowed_tools=("read_context", CAPABILITY) if assignment == "tools" else ("read_context",),
        skills=(ALIAS,) if assignment == "skills" else (),
    ) for index, purpose in enumerate((RolePurpose.PLAN, RolePurpose.VERIFY)))
    _assert_collection_owners(roles, kind, {role.id for role in roles})


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
def test_post_product_only_collection_has_no_implicit_owner(kind: str) -> None:
    roles = tuple(replace(_role(purpose), id=f"review_{index}") for index, purpose in enumerate((RolePurpose.VERIFY, RolePurpose.CRITIQUE)))
    _assert_collection_owners(roles, kind, set())


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
@pytest.mark.parametrize("token", [ALIAS, f"{CAPABILITY}.details", f"{ALIAS}_extra"])
def test_collection_alias_discovery_respects_token_boundary(kind: str, token: str) -> None:
    roles = (replace(_role(RolePurpose.PLAN), id="custom_planner"), replace(_role(RolePurpose.EXECUTE), id="custom_owner"))
    _assert_collection_owners(roles, kind, {"custom_owner"} if token == ALIAS else set(), token=token)


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
def test_collection_owner_cannot_discover_unavailable_tool(kind: str) -> None:
    roles = (replace(_role(RolePurpose.PLAN), id="custom_planner"), replace(_role(RolePurpose.EXECUTE), id="custom_owner"))
    _assert_collection_owners(roles, kind, set(), available=False)


def test_no_gateway_and_no_context_keep_existing_behavior() -> None:
    roles = (replace(_role(RolePurpose.PLAN), id="custom_planner"), replace(_role(RolePurpose.EXECUTE), id="custom_owner"))
    context = _context(f"Use {CAPABILITY}.")
    assert defaults._role_tools_by_id(roles, context, capability_gateway=None) == {role.id: role.allowed_tools for role in roles}
    assert defaults._plan_allowed_tools(roles, context, capability_gateway=None) == ()
    plan = defaults._dispatch_plan(roles, context, capability_gateway=None)
    assert plan.allowed_tools == ()
    assert all(step.tools == () for step in plan.steps)
    assert all(agent.allowed_tools == () for agent in plan.agents)
    for role in roles:
        assert defaults._role_allowed_tools(role, None, capability_gateway=InventoryGateway("plugin")) == ()
        assert defaults._role_allowed_tools(role, context, capability_gateway=None) == ()
    discussion = defaults._discussion_plan(roles, "main", capability_gateway=InventoryGateway("plugin"))
    assert all(participant.allowed_tools == () for participant in discussion.participants)


@pytest.mark.parametrize("kind", ["mcp", "plugin"])
@pytest.mark.parametrize("purpose", [RolePurpose.PLAN, RolePurpose.EXECUTE, RolePurpose.VERIFY])
def test_unavailable_inventory_is_not_granted_even_when_explicit(
    kind: str, purpose: RolePurpose,
) -> None:
    role = replace(_role(purpose), mission=f"Use {CAPABILITY}.", allowed_tools=("read_context", CAPABILITY))
    tools = defaults._role_allowed_tools(
        role, _context(f"Use {CAPABILITY}."), capability_gateway=InventoryGateway(kind, available=False),
    )
    assert tools == ("read_context",)
