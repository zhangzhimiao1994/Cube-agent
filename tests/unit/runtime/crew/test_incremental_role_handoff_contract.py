from __future__ import annotations

import json
from decimal import Decimal

import pytest

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelResponse, TokenUsage
from agent_hub.runtime.crew import adapter
from agent_hub.runtime.crew.plan import AgentSpec, DispatchStep

TASK = "Project workspace delivery contract: implement and verify the assigned workspace."


def completion(text: str) -> GatewayCompletion:
    return GatewayCompletion(
        response=ModelResponse(text=text, usage=TokenUsage(10, 5, 15)),
        deployment_id="primary",
        logical_model="deepseek",
        provider_id="deepseek",
        provider_model="deepseek/model",
        cost_usd=Decimal("0.01"),
    )


def agent() -> AgentSpec:
    return AgentSpec(
        id="implementer",
        role="Implementer",
        goal="Build the assigned workspace.",
        logical_model="deepseek",
        output_schema={
            "status": "string",
            "summary": "string",
            "evidence": "string[]",
            "risks": "string[]",
            "artifacts": "string[]",
            "verification": "string[]",
        },
    )


def step(tools: tuple[str, ...], task: str = TASK) -> DispatchStep:
    return DispatchStep(id="implementer_step", agent="implementer", task=task, tools=tools)


def test_incremental_tool_completion_uses_same_handoff_contract_as_zip() -> None:
    selected = step(("workspace.write_text", "workspace.bundle"))
    result = completion("Wrote source and tests, verified them, and bundled the workspace.")
    assert adapter._is_incremental_workspace_contract_step(selected)
    assert not adapter._is_project_scale_tool_contract_step(selected)
    assert not adapter._should_check_framework_raw(selected, result)
    projected = adapter._project_scale_structured_role_completion(selected, agent(), result)
    payload = json.loads(projected.response.text or "")
    assert payload["status"] == "done"
    assert payload["summary"] == result.response.text
    assert payload["evidence"]
    assert projected.response.usage == result.response.usage
    assert projected.cost_usd == result.cost_usd
    assert projected.logical_model == result.logical_model
    assert projected.provider_model == result.provider_model
    assert projected.response.provider_metadata == result.response.provider_metadata


@pytest.mark.parametrize(
    "tools,task",
    [
        (("workspace.write_text",), TASK),
        (("workspace.bundle",), TASK),
        (("workspace.bundle", "workspace.write_text"), "Ordinary structured role task."),
    ],
)
def test_ordinary_and_partial_tool_scopes_keep_structured_framework_check(
    tools: tuple[str, ...],
    task: str,
) -> None:
    selected = step(tools, task)
    result = completion("Not a structured role output.")
    assert adapter._should_check_framework_raw(selected, result)
    assert adapter._project_scale_structured_role_completion(selected, agent(), result) is result


def test_valid_incremental_structured_output_is_not_rewritten() -> None:
    selected = step(("workspace.write_text", "workspace.bundle"))
    result = completion(
        json.dumps(
            {
                "status": "done",
                "summary": "Original provider facts.",
                "evidence": [],
                "risks": [],
                "artifacts": [],
                "verification": [],
            }
        )
    )
    assert adapter._project_scale_structured_role_completion(selected, agent(), result) is result


def test_incremental_plain_completion_is_projected_before_structured_validation() -> None:
    selected = step(("workspace.write_text", "workspace.bundle"))
    result = completion("Completed the assigned workspace writes and bundle.")
    projected = adapter._project_scale_structured_role_completion(selected, agent(), result)
    assert projected is not result
    assert json.loads(projected.response.text or "")["summary"] == result.response.text
