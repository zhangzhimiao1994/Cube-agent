import json
from decimal import Decimal
from uuid import UUID

from agent_hub.domain.runs import TaskMode
from agent_hub.models.gateway import GatewayCompletion, GatewayRejectedOutput
from agent_hub.models.types import ModelResponse, RejectedOutputEvidence, TokenUsage
from agent_hub.runtime.contracts import TaskContext
from agent_hub.runtime.crew.adapter import (
    _project_scale_artifact_zip_completion,
    _project_scale_rejected_zip_completion,
)
from agent_hub.runtime.crew.plan import DispatchStep

RUN_ID = UUID("00000000-0000-4000-8000-000000000021")
TENANT_ID = UUID("00000000-0000-4000-8000-000000000022")


def _project_scale_context() -> TaskContext:
    return TaskContext(
        run_id=RUN_ID,
        tenant_id=TENANT_ID,
        mode=TaskMode.DISPATCH,
        request="Build a real small business project for flow=dispatch.",
        routing_decision={
            "project_id": "project-scale-acceptance",
            "workspace_session_id": "project-scale-small-dispatch",
        },
    )


def _project_scale_step(task: str) -> DispatchStep:
    return DispatchStep(
        id="implementer_step",
        agent="implementer",
        task=task,
        tools=("project.generate_zip",),
        token_budget=10_000,
        cost_budget_usd=Decimal(10),
    )


def _completion(text: str | None) -> GatewayCompletion:
    return GatewayCompletion(
        response=ModelResponse(text=text, usage=TokenUsage(1, 1, 2)),
        deployment_id="primary",
        logical_model="qwen",
        provider_id="qwen",
        provider_model="qwen/max",
        cost_usd=Decimal(0),
    )


def test_real_project_scale_json_bundle_is_converted_to_zip_tool_call() -> None:
    task = (
        "Role mission: implement.\n"
        "User task: Build a real small business project for flow=dispatch. "
        "Return strict JSON workspace_bundle.files (relative paths to full content)."
    )
    text = json.dumps(
        {
            "workspace_bundle": {
                "files": {
                    "package.json": "{\"scripts\":{\"test\":\"node --test\"}}\n",
                    "src/main.js": "export const ok = true;\n",
                }
            }
        },
        ensure_ascii=False,
    )
    completion = _completion(text)

    updated = _project_scale_artifact_zip_completion(
        _project_scale_context(),
        _project_scale_step(task),
        completion,
        completion.response,
    )

    assert len(updated.response.tool_calls) == 1
    call = updated.response.tool_calls[0]
    assert call.name == "project.generate_zip"
    assert call.arguments["project_id"] == "project-scale-acceptance"
    assert call.arguments["workspace_session_id"] == "project-scale-small-dispatch"
    assert call.arguments["files"] == {
        "package.json": "{\"scripts\":{\"test\":\"node --test\"}}\n",
        "src/main.js": "export const ok = true;\n",
    }


def test_real_project_scale_markdown_blocks_are_converted_to_zip_tool_call() -> None:
    task = (
        "Repair this same business project; preserve every original requirement. "
        "Original request: Build a real small business project for flow=dispatch. "
        "Return strict JSON workspace_bundle.files or fenced file blocks headed ### `path/to/file`."
    )
    text = (
        "### `README.md`\n"
        "```md\n"
        "# Real Project\n"
        "```\n\n"
        "### `src/main.js`\n"
        "```js\n"
        "export function health() { return 'ok'; }\n"
        "```\n"
    )
    completion = _completion(text)

    updated = _project_scale_artifact_zip_completion(
        _project_scale_context(),
        _project_scale_step(task),
        completion,
        completion.response,
    )

    assert len(updated.response.tool_calls) == 1
    assert updated.response.tool_calls[0].arguments["files"] == {
        "README.md": "# Real Project\n",
        "src/main.js": "export function health() { return 'ok'; }\n",
    }


def test_real_project_scale_does_not_use_fixture_zip_without_model_files() -> None:
    task = (
        "Role mission: implement.\n"
        "User task: Build a real small business project for flow=dispatch. "
        "Return strict JSON workspace_bundle.files (relative paths to full content)."
    )
    completion = _completion("I will build the project later.")

    updated = _project_scale_artifact_zip_completion(
        _project_scale_context(),
        _project_scale_step(task),
        completion,
        completion.response,
    )

    assert updated is completion


def test_rejected_real_project_scale_bundle_is_converted_to_zip_tool_call() -> None:
    task = (
        "Role mission: implement.\n"
        "User task: Build a real small business project for flow=dispatch. "
        "Return strict JSON workspace_bundle.files (relative paths to full content)."
    )
    text = json.dumps(
        {
            "workspace_bundle": {
                "files": {
                    "README.md": "# Real rejected bundle\n",
                    "tests/main.test.js": "import test from 'node:test';\n",
                }
            }
        },
        ensure_ascii=False,
    )
    rejected = GatewayRejectedOutput(
        evidence=RejectedOutputEvidence(
            final_text=text,
            usage=TokenUsage(10, 5, 15),
            usage_status="known",
            status="completed",
            reason="schema_mismatch",
        ),
        deployment_id="primary",
        logical_model="deepseek",
        provider_id="deepseek",
        provider_model="deepseek/chat",
        cost_usd=Decimal(0),
    )

    updated = _project_scale_rejected_zip_completion(
        _project_scale_context(),
        _project_scale_step(task),
        rejected,
    )

    assert updated is not None
    assert len(updated.response.tool_calls) == 1
    assert updated.response.tool_calls[0].arguments["files"] == {
        "README.md": "# Real rejected bundle\n",
        "tests/main.test.js": "import test from 'node:test';\n",
    }
