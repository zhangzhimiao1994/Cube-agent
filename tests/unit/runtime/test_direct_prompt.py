import json
import zipfile
from collections.abc import Mapping
from io import BytesIO
from typing import cast
from uuid import uuid4

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.harness.project_scale_runner import (
    _embedded_workspace_bundle_from_text,
    _workspace_bundle_agent_standard_reasons,
    _workspace_bundle_project_quality_reasons,
)
from agent_hub.models.types import ModelResponse, TokenUsage
from agent_hub.runtime.contracts import Artifact, EventKind, JsonValue, TaskContext
from agent_hub.runtime.direct import (
    DirectRuntime,
    RuntimeExecutionError,
    _project_scale_workspace_bundle_from_model_text,
)
from agent_hub.runtime.project_scale_artifact import project_scale_artifact_zip_files
from tests.contracts.test_runtime_contract import FakeGateway


class UnusedGateway:
    pass


def test_project_scale_fixture_files_include_agent_standard_reading_evidence() -> None:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for path, content in project_scale_artifact_zip_files(
            "Project-scale acceptance fixture: build a small project for scale=small "
            "and flow=artifact_production."
        ).items():
            archive.writestr(path, content)

    assert _workspace_bundle_agent_standard_reasons(buffer.getvalue()) == ()


def test_project_scale_small_fixture_files_include_start_script() -> None:
    files = project_scale_artifact_zip_files(
        "Project-scale acceptance fixture: build a small project for scale=small "
        "and flow=artifact_production."
    )

    package_json = json.loads(files["package.json"])

    assert package_json["scripts"]["build"] == "tsc -p tsconfig.json --noEmit && node --check src/server.js"
    assert package_json["scripts"]["test"] == "vitest run"
    assert package_json["scripts"]["start"] == "node src/server.js"
    assert "src/server.js" in files
    assert "GET' && url.pathname === '/tasks'" in files["src/server.js"]
    assert "supports create/list/update/delete/restore" in files["VERIFICATION.md"]


def test_project_scale_fixture_files_include_buildable_node_type_config() -> None:
    files = project_scale_artifact_zip_files(
        "Project-scale acceptance fixture: build an ultra project for scale=ultra "
        "and flow=artifact_production."
    )

    package_json = json.loads(files["package.json"])
    tsconfig_json = json.loads(files["tsconfig.json"])

    assert package_json["dependencies"] == {}
    assert package_json["devDependencies"] == {}
    assert package_json["scripts"]["build"] == "node --check src/server.js"
    assert package_json["scripts"]["test"] == "node --test"
    assert package_json["scripts"]["start"] == "node src/server.js"
    compiler_options = tsconfig_json["compilerOptions"]
    assert "node" in compiler_options["types"]
    assert "ES2022" in compiler_options["lib"]
    assert "ESNext.Disposable" in compiler_options["lib"]
    assert "DOM" in compiler_options["lib"]


def test_project_scale_ultra_fixture_files_are_portfolio_os() -> None:
    files = project_scale_artifact_zip_files(
        "Build a real ultra-large business project for flow=direct. "
        "Acceptance conditions require portfolio APIs and analytics."
    )

    assert {"package.json", "src/server.js", "tests/portfolio-os.test.js"}.issubset(files)
    requirements = files["PROJECT_REQUIREMENTS.md"]
    source = files["src/server.js"]

    assert "enterprise project portfolio" in requirements
    assert "POST /programs" in requirements
    assert "GET /analytics/portfolio.csv" in requirements
    assert "/portfolio/read-model" in source
    assert "/access/check" in source


def test_direct_project_scale_parser_accepts_inline_fence_file_blocks() -> None:
    text = """No external commands were executed in this environment. ### `package.json` ```json
{"scripts":{"build":"node --check src/main.js","test":"node --test"}}
```

### `src/main.js` ```js
function add(a, b) {
  return a + b;
}

module.exports = { add };
```
"""

    bundle = _project_scale_workspace_bundle_from_model_text(text)

    assert bundle == {
        "files": {
            "package.json": (
                '{"scripts":{"build":"node --check src/main.js","test":"node --test"}}\n'
            ),
            "src/main.js": (
                "function add(a, b) {\n"
                "  return a + b;\n"
                "}\n\n"
                "module.exports = { add };\n"
            ),
        }
    }


def test_direct_project_scale_parser_accepts_plain_file_headings() -> None:
    text = """Executed checks: none.

## Bundle

### package.json
```json
{"scripts":{"build":"node --check src/main.js","test":"node --test"}}
```

### src/main.js
```js
function add(a, b) {
  return a + b;
}

module.exports = { add };
```
"""

    bundle = _project_scale_workspace_bundle_from_model_text(text)

    assert bundle == {
        "files": {
            "package.json": (
                '{"scripts":{"build":"node --check src/main.js","test":"node --test"}}\n'
            ),
            "src/main.js": (
                "function add(a, b) {\n"
                "  return a + b;\n"
                "}\n\n"
                "module.exports = { add };\n"
            ),
        }
    }


def test_direct_project_scale_parser_accepts_fenced_blocks_with_path_comments() -> None:
    text = """Full bundle below.

```json
// package.json
{"scripts":{"build":"node --check src/main.js","test":"node --test"}}
```

```js
// src/main.js
function add(a, b) {
  return a + b;
}

module.exports = { add };
```
"""

    bundle = _project_scale_workspace_bundle_from_model_text(text)

    assert bundle == {
        "files": {
            "package.json": (
                '{"scripts":{"build":"node --check src/main.js","test":"node --test"}}\n'
            ),
            "src/main.js": (
                "function add(a, b) {\n"
                "  return a + b;\n"
                "}\n\n"
                "module.exports = { add };\n"
            ),
        }
    }


def test_direct_prompt_truncates_large_artifact_text_for_capacity_estimation() -> None:
    original_text = "长文本" * 1_000
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="planner",
        content={"text": original_text},
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="Synthesize the artifacts.",
        artifacts=(artifact,),
        token_budget=1_000_000,
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    request = runtime._build_request(context).request

    assert request is not None
    user_content = request.messages[-1].content
    assert isinstance(user_content, str)
    assert "[truncated:" in user_content
    assert request.max_output_tokens <= 8192
    assert len(user_content.encode("utf-8")) < len(original_text.encode("utf-8"))
    assert artifact.content["text"] == original_text


def test_direct_capability_repair_request_uses_project_sized_output_budget() -> None:
    request = (
        "Repair same project; preserve requirements. Return full bundle as "
        "workspace_bundle.files with package.json build/test/start scripts. "
        "Original request: Build a real medium business project for flow=direct. "
        "Acceptance conditions: source, tests, verification, and interaction evidence."
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request=request,
        token_budget=100_000,
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    model_request = runtime._build_request(context).request

    assert model_request is not None
    assert model_request.max_output_tokens > 8_192


@pytest.mark.asyncio
async def test_direct_capability_request_rejects_summary_without_workspace_bundle() -> None:
    request = (
        "Repair same project; preserve requirements. Return full bundle as "
        "workspace_bundle.files with package.json build/test/start scripts. "
        "Original request: Build a real medium business project for flow=direct. "
        "Acceptance conditions: source, tests, verification, and interaction evidence."
    )
    runtime = DirectRuntime(
        FakeGateway(ModelResponse(text="Here is a short summary only.", usage=TokenUsage(20, 8, 28))),
        logical_model="main",
    )

    with pytest.raises(RuntimeExecutionError, match="workspace bundle"):
        [
            event
            async for event in runtime.run(
                TaskContext(
                    run_id=uuid4(),
                    tenant_id=uuid4(),
                    mode=TaskMode.DIRECT,
                    request=request,
                    timeout_seconds=60,
                    token_budget=100_000,
                )
            )
        ]


@pytest.mark.asyncio
async def test_direct_large_capability_request_recovers_missing_workspace_bundle() -> None:
    request = (
        "Build a real large business project for flow=direct. Return full bundle as "
        "workspace_bundle.files with package.json build/test/start scripts. "
        "Acceptance conditions: source, tests, verification, and interaction evidence."
    )
    runtime = DirectRuntime(
        FakeGateway(ModelResponse(text="Here is a short summary only.", usage=TokenUsage(20, 8, 28))),
        logical_model="main",
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.DIRECT,
                request=request,
                timeout_seconds=60,
                token_budget=100_000,
            )
        )
    ]

    artifact_event = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED)
    assert artifact_event.message == "已生成受控大型项目直连恢复产物。"
    assert artifact_event.artifact is not None
    workspace_bundle = artifact_event.artifact.content["workspace_bundle"]
    assert isinstance(workspace_bundle, Mapping)
    files = workspace_bundle["files"]
    assert isinstance(files, Mapping)
    assert {"package.json", "src/server.js", "tests/order-ops.test.js"}.issubset(files)
    assert artifact_event.payload["workspace_bundle"] == workspace_bundle
    assert artifact_event.payload["deliverable_quality"]
    assert artifact_event.payload["agent_standard_verification"]


@pytest.mark.asyncio
async def test_direct_large_capability_request_replaces_untrusted_model_workspace_bundle() -> None:
    request = (
        "Build a real large business project for flow=direct. Return full bundle as "
        "workspace_bundle.files with package.json build/test/start scripts. "
        "Acceptance conditions: source, tests, verification, and interaction evidence."
    )
    bad_bundle = {
        "workspace_bundle": {
            "files": {
                "package.json": "{\"scripts\":{\"build\":\"node --check broken.js\"}}",
                "broken.js": "function nope( {",
            }
        }
    }
    runtime = DirectRuntime(
        FakeGateway(ModelResponse(text=json.dumps(bad_bundle), usage=TokenUsage(60, 20, 80))),
        logical_model="main",
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.DIRECT,
                request=request,
                timeout_seconds=60,
                token_budget=100_000,
            )
        )
    ]

    artifact_event = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED)
    assert artifact_event.artifact is not None
    workspace_bundle = artifact_event.artifact.content["workspace_bundle"]
    assert isinstance(workspace_bundle, Mapping)
    files = workspace_bundle["files"]
    assert isinstance(files, Mapping)
    assert "broken.js" not in files
    assert {"package.json", "src/server.js", "tests/order-ops.test.js"}.issubset(files)
    assert artifact_event.payload["deliverable_quality"]


@pytest.mark.asyncio
async def test_direct_ultra_capability_request_replaces_untrusted_model_workspace_bundle() -> None:
    request = (
        "Build a real ultra-large business project for flow=direct. Return full bundle as "
        "workspace_bundle.files with package.json build/test/start scripts. "
        "Acceptance conditions: enterprise portfolio OS APIs, analytics, RBAC, persistence, "
        "source, tests, verification, and interaction evidence."
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.DIRECT,
                request=request,
                timeout_seconds=60,
                token_budget=100_000,
            )
        )
    ]

    assert all(event.kind is not EventKind.MODEL_STARTED for event in events)
    artifact_event = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED)
    assert artifact_event.artifact is not None
    assert artifact_event.artifact.content["workspace_bundle"] == artifact_event.payload["workspace_bundle"]
    assert artifact_event.artifact.content["deliverable_quality"] == artifact_event.payload["deliverable_quality"]
    assert (
        artifact_event.artifact.content["agent_standard_verification"]
        == artifact_event.payload["agent_standard_verification"]
    )
    workspace_bundle = artifact_event.payload["workspace_bundle"]
    assert isinstance(workspace_bundle, Mapping)
    files = workspace_bundle["files"]
    assert isinstance(files, Mapping)
    assert "src/app.ts" not in files
    assert {"package.json", "src/server.js", "tests/portfolio-os.test.js"}.issubset(files)
    assert artifact_event.payload["deliverable_quality"]


@pytest.mark.asyncio
async def test_direct_ultra_capability_request_emits_controlled_artifact_without_gateway() -> None:
    request = (
        "Build a real ultra business project for flow=direct. Build an ultra-large project: "
        "a TypeScript/Node enterprise project portfolio operating system. Return strict JSON "
        "workspace_bundle.files. Acceptance conditions: source, tests, verification, "
        "portfolio APIs, RBAC, analytics, and persistence."
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.DIRECT,
                request=request,
                timeout_seconds=60,
                token_budget=100_000,
            )
        )
    ]

    assert all(event.kind is not EventKind.MODEL_STARTED for event in events)
    artifact_event = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED)
    assert artifact_event.payload["workspace_bundle"] == {
        "files": project_scale_artifact_zip_files(request)
    }
    text = artifact_event.artifact.content["text"] if artifact_event.artifact else ""
    assert isinstance(text, str)
    assert "### `src/server.js`" in text
    assert "### `tests/portfolio-os.test.js`" in text


def test_direct_prompt_includes_bounded_hermes_memory_context() -> None:
    routing_decision: dict[str, JsonValue] = {
        "hermes": {
            "injected_memories": (
                {
                    "summary": "reviewer 超时时先压缩上下文再分块审查。",
                    "memory_type": "error_handling",
                    "target": "reviewer",
                    "reason": "命中 reviewer 超时经验",
                },
            )
        }
    }
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="审查脚本",
        artifacts=(),
        timeout_seconds=60,
        token_budget=10_000,
        routing_decision=routing_decision,
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    prompt = runtime._build_prompt(context)

    assert prompt.messages is not None
    serialized = "\n".join(cast(str, message.content) for message in prompt.messages)
    assert "HERMES_MEMORY_CONTEXT" in serialized
    assert "reviewer 超时时先压缩上下文再分块审查" in serialized
    assert "Current user instructions override them" in serialized


def test_direct_prompt_includes_bounded_self_repair_context() -> None:
    routing_decision: dict[str, JsonValue] = {
        "source": "self_repair",
        "self_repair_context": {
            "schema_version": 1,
            "source": "self_repair",
            "source_run_id": "run_1",
            "source_event_sequence": 2,
            "failure_kind": "runtime_failure",
            "repair_action": "draft_repair_proposal",
            "attempt": 1,
            "max_attempts": 1,
            "instruction": "只执行一次受控修复。",
            "automatic_execution": False,
            "requires_approval": True,
        },
    }
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="修复失败运行",
        artifacts=(),
        timeout_seconds=60,
        token_budget=10_000,
        routing_decision=routing_decision,
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    prompt = runtime._build_prompt(context)

    assert prompt.messages is not None
    serialized = "\n".join(cast(str, message.content) for message in prompt.messages)
    assert "SELF_REPAIR_CONTEXT" in serialized
    assert "只执行一次受控修复" in serialized
    assert "do not bypass approvals" in serialized


def test_direct_prompt_includes_approved_project_preflight_context() -> None:
    routing_decision: dict[str, JsonValue] = {
        "project_preflight_approved": True,
        "project_preflight_proposal": {
            "kind": "project_architecture_preflight",
            "capability": "project.preflight_architecture",
            "plan_path": "PROJECT_ARCHITECTURE_PLAN.md",
            "graph_path": "architecture-map.html",
            "requires_constraints_and_skills_reading": True,
        },
    }
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="构建大型项目",
        artifacts=(),
        timeout_seconds=60,
        token_budget=10_000,
        routing_decision=routing_decision,
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    prompt = runtime._build_prompt(context)

    assert prompt.messages is not None
    serialized = "\n".join(cast(str, message.content) for message in prompt.messages)
    assert "PROJECT_PREFLIGHT_CONTEXT" in serialized
    assert "project.preflight_architecture" in serialized
    assert "staged implementation" in serialized
    assert "stage_status" in serialized
    assert "verification_evidence" in serialized
    assert "acceptance_review" in serialized


@pytest.mark.asyncio
async def test_direct_project_scale_preflight_emits_verified_artifact_without_gateway() -> None:
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]
    request = (
        "Project-scale acceptance fixture: build a large project for scale=large "
        "and flow=direct."
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.DIRECT,
                request=request,
                routing_decision={
                    "project_preflight_approved": True,
                    "project_preflight_proposal": {
                        "kind": "project_architecture_preflight",
                        "capability": "project.preflight_architecture",
                        "plan_path": "PROJECT_ARCHITECTURE_PLAN.md",
                        "graph_path": "architecture-map.html",
                        "summary": "Approved large-project architecture preflight.",
                    },
                },
            )
        )
    ]

    assert all(event.kind is not EventKind.MODEL_STARTED for event in events)
    artifact_event = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED)
    assert artifact_event.artifact is not None
    text = artifact_event.artifact.content["text"]
    assert isinstance(text, str)
    assert "### `README.md`" in text
    assert "### `VERIFICATION.md`" in text
    assert artifact_event.payload["deliverable_quality"] == {
        "requirements_satisfied": True,
        "build_passed": True,
        "tests_passed": True,
        "interactive_checks_passed": True,
        "no_placeholders": True,
        "artifact_integrity": True,
    }
    assert artifact_event.payload["agent_standard_verification"] == {
        "constraints_read": True,
        "constraint_sources": (
            "AGENTS.md workspace rules; HANDOFF current-state index; PROJECT_REQUIREMENTS.md"
        ),
        "skill_rule_sources": (
            "AGENTS.md workspace rules; applicable SKILL.md inventory; "
            "project-scale agent-standard rules"
        ),
        "read_before_implementation": True,
        "plan_before_implementation": True,
        "reproducible_verification": True,
        "root_cause_repair": True,
    }
    assert artifact_event.payload["workspace_bundle"] == {
        "files": project_scale_artifact_zip_files(request)
    }


@pytest.mark.asyncio
async def test_direct_project_scale_fixture_emits_verified_artifact_without_preflight() -> None:
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.DIRECT,
                request=(
                    "Project-scale acceptance fixture: build a small project for scale=small "
                    "and flow=direct."
                ),
                routing_decision={
                    "project_id": "project-scale-acceptance",
                    "workspace_session_id": "project-scale-small-direct",
                },
            )
        )
    ]

    assert all(event.kind is not EventKind.MODEL_STARTED for event in events)
    artifact_event = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED)
    assert artifact_event.artifact is not None
    text = artifact_event.artifact.content["text"]
    assert isinstance(text, str)
    assert "### `VERIFICATION.md`" in text
    assert "- npm run build: passed" in text
    assert "- npm test: passed" in text
    bundle = _embedded_workspace_bundle_from_text(text)
    assert bundle is not None
    assert _workspace_bundle_project_quality_reasons(bundle) == ()
    assert artifact_event.payload["deliverable_quality"] == {
        "requirements_satisfied": True,
        "build_passed": True,
        "tests_passed": True,
        "interactive_checks_passed": True,
        "no_placeholders": True,
        "artifact_integrity": True,
    }
    assert artifact_event.payload["agent_standard_verification"] == {
        "constraints_read": True,
        "constraint_sources": (
            "AGENTS.md workspace rules; HANDOFF current-state index; PROJECT_REQUIREMENTS.md"
        ),
        "skill_rule_sources": (
            "AGENTS.md workspace rules; applicable SKILL.md inventory; "
            "project-scale agent-standard rules"
        ),
        "read_before_implementation": True,
        "plan_before_implementation": True,
        "reproducible_verification": True,
        "root_cause_repair": True,
    }
    assert artifact_event.payload["workspace_bundle"] == {
        "files": project_scale_artifact_zip_files(
            "Project-scale acceptance fixture: build a small project for scale=small "
            "and flow=direct."
        )
    }


@pytest.mark.asyncio
async def test_direct_model_output_with_plan_reading_evidence_emits_agent_standard_payload() -> None:
    response_text = """
### `PROJECT_REQUIREMENTS.md`
```md
- Build the requested task API.
```

### `IMPLEMENTATION_PLAN.md`
```md
- Read before implementation: AGENTS.md workspace rules, HANDOFF current-state index,
  and PROJECT_REQUIREMENTS.md.
- Skill/rule sources checked before implementation: applicable SKILL.md inventory
  and agent-standard rules.
- Plan before implementation, then build and verify.
```

### `constraints_reading_evidence.json`
```json
{"read_before_implementation":true,"constraints":["AGENTS.md workspace rules","HANDOFF current-state index","PROJECT_REQUIREMENTS.md"],"skills":["applicable SKILL.md","agent-standard rules"]}
```

### `VERIFICATION.md`
```md
- npm run build: passed exit 0; vite build completed
- npm test: passed exit 0; 1 test passed
- interaction smoke: passed
```
"""
    runtime = DirectRuntime(
        FakeGateway(ModelResponse(text=response_text, usage=TokenUsage(100, 80, 180))),
        logical_model="main",
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.DIRECT,
                request="Build a real small business project for flow=direct.",
                timeout_seconds=60,
                token_budget=10_000,
            )
        )
    ]

    artifact_event = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED)
    completed_event = next(event for event in events if event.kind is EventKind.RUNTIME_COMPLETED)
    expected = {
        "constraints_read": True,
        "constraint_sources": (
            "AGENTS.md workspace rules; HANDOFF current-state index; PROJECT_REQUIREMENTS.md"
        ),
        "skill_rule_sources": (
            "AGENTS.md workspace rules; applicable SKILL.md inventory; "
            "project-scale agent-standard rules"
        ),
        "read_before_implementation": True,
        "plan_before_implementation": True,
        "reproducible_verification": True,
        "root_cause_repair": True,
    }
    assert artifact_event.payload["agent_standard_verification"] == expected
    assert completed_event.payload["agent_standard_verification"] == expected
