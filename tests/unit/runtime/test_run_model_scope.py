from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest

from agent_hub.config.schema import PlatformConfig
from agent_hub.domain.runs import TaskMode
from agent_hub.runs.service import _harness_task_requirements
from agent_hub.runtime import defaults
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.role_planner import RoleAssignment, RolePurpose
from tests.unit.runtime.test_configured_runtime import (
    TENANT_ID,
    FakeConfigService,
    FakeSecretService,
    FakeTransport,
    _immediate_capacity,
)


def _document() -> dict[str, Any]:
    deployment = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "api_base": "https://api.deepseek.com/v1",
        "credential_ref": "secret://deepseek",
        "quota_scope_id": "deepseek_account",
        "max_concurrency": 4,
        "target_utilization": 0.8,
        "reserved_slots": 0,
        "capabilities": ["text", "structured_output", "tool_calling"],
    }
    return {
        "models": {
            "deepseek": {"deployments": [deployment], "fallback_model": "sonnet"},
            "sonnet": {
                "deployments": [
                    {**deployment, "provider": "anthropic", "model": "claude-sonnet-4-5"}
                ]
            },
        },
        "agents": [
            {
                "id": "reviewer",
                "role": "Code reviewer",
                "prompt": "Review the implementation.",
                "model": "sonnet",
                "skills": [],
            }
        ],
    }


def test_service_harness_requirements_retain_request_model_scope() -> None:
    requirements = _harness_task_requirements(
        message="Answer briefly.",
        mode=TaskMode.DIRECT,
        routing_decision={"allowed_models": ["deepseek"]},
    )
    assert requirements.allowed_logical_models == frozenset({"deepseek"})


@pytest.mark.parametrize(
    "runtime_type,mode",
    [
        (defaults.ConfigBackedDirectRuntime, TaskMode.DIRECT),
        (defaults.ConfigBackedDispatchRuntime, TaskMode.DISPATCH),
        (defaults.ConfigBackedDiscussionRuntime, TaskMode.DISCUSS),
        (defaults.ConfigBackedHybridRuntime, TaskMode.HYBRID),
    ],
)
async def test_request_model_scope_excludes_other_gateway_and_role_models(
    runtime_type: Any,
    mode: TaskMode,
) -> None:
    document = _document()
    original = deepcopy(document)
    captured: list[str] = []

    async def capacity(tenant_id: Any, deployments: Any) -> Any:
        captured.extend(item.logical_model for item in deployments)
        return await _immediate_capacity(tenant_id, deployments)

    runtime = runtime_type(
        config_service=FakeConfigService(document),
        secret_service=FakeSecretService(),
        capacity_factory=capacity,
        transport=FakeTransport(),
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        mode=mode,
        request="Build and review a software website.",
        routing_decision={"direct_model": "deepseek", "allowed_models": ("deepseek",)},
    )
    child = await runtime._runtime_for(context)

    assert captured and set(captured) == {"deepseek"}
    assert document == original
    assert not isinstance(child, defaults.UnavailableRuntime)
    if mode is not TaskMode.DIRECT:
        assert child._roles
        assert {role["logical_model"] for role in child._roles} == {"deepseek"}


@pytest.mark.parametrize(
    "selection",
    [
        {"allowed_models": ("unknown",)},
        {"allowed_models": ("deepseek", "deepseek")},
        {"allowed_models": "deepseek"},
        {"allowed_models": ("deepseek",), "direct_model": "sonnet"},
        {"allowed_models": ("deepseek",), "harness_decision": {"selected_logical_model": "sonnet"}},
        {"allowed_models": ("deepseek",), "main_agent_model": {"invalid": True}},
    ],
)
async def test_invalid_request_model_scope_fails_before_capacity_or_transport(
    selection: Any,
) -> None:
    transport = FakeTransport()

    async def forbidden_capacity(tenant_id: Any, deployments: Any) -> Any:
        raise AssertionError("invalid scope reached capacity")

    runtime = defaults.ConfigBackedDirectRuntime(
        config_service=FakeConfigService(_document()),  # type: ignore[arg-type]
        secret_service=FakeSecretService(),  # type: ignore[arg-type]
        capacity_factory=forbidden_capacity,
        transport=transport,
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=TENANT_ID,
        mode=TaskMode.DIRECT,
        request="Answer the question.",
        routing_decision=selection,
    )
    events = [event async for event in runtime.run(context)]

    assert any(event.kind == "runtime.failed" for event in events)
    assert transport.calls == []


def test_scoped_config_retains_agent_identity_and_removes_excluded_fallbacks() -> None:
    config = PlatformConfig.model_validate(_document())
    snapshot = config.model_dump()
    scoped = defaults._scope_runtime_model_config(
        config,
        {"allowed_models": ("deepseek",), "direct_model": "deepseek"},
    )

    assert scoped.models["deepseek"].fallback_model is None
    assert scoped.agents[0].model == "deepseek"
    assert scoped.agents[0].id == config.agents[0].id
    assert scoped.agents[0].prompt == config.agents[0].prompt
    assert PlatformConfig.model_validate(scoped.model_dump()) == scoped
    assert config.model_dump() == snapshot
    assert defaults._scope_runtime_model_config(config, {}) is config


def test_scope_does_not_make_incapable_backup_eligible_for_structured_tool_role() -> None:
    document = _document()
    document["models"]["deepseek"]["deployments"][0]["capabilities"] = ["text"]
    config = PlatformConfig.model_validate(document)
    scoped = defaults._scope_runtime_model_config(config, {"allowed_models": ("deepseek",)})
    role = RoleAssignment(
        id="implementer",
        role="Implementer",
        purpose=RolePurpose.EXECUTE,
        mission="Implement the website.",
        must_answer=("What was implemented?",),
        allowed_tools=("workspace.write_text",),
        forbidden_actions=("Do not modify unrelated files.",),
        skills=(),
        output_schema={"summary": "string"},
        model="deepseek",
    )

    with pytest.raises(defaults.HarnessModelSelectionError, match="capability unavailable"):
        defaults._assign_models_to_roles(
            (role,),
            scoped,
            default_model="deepseek",
            task="Build a website.",
        )
