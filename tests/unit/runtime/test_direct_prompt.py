import asyncio
import errno
import json
import zipfile
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.domain.runs import TaskMode
from agent_hub.harness.project_scale_runner import (
    _embedded_workspace_bundle_from_text,
    _workspace_bundle_agent_standard_reasons,
    _workspace_bundle_project_quality_reasons,
)
from agent_hub.models.litellm_client import ModelTransportError
from agent_hub.models.types import ModelRequest, ModelResponse, TokenUsage
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.direct import (
    DirectRuntime,
    RuntimeExecutionError,
    _normalized_workspace_bundle,
    _project_scale_workspace_bundle_from_model_text,
    _workspace_batch_from_model_text,
    _workspace_bundle_has_website_preview,
    _workspace_delivery_initial_seconds,
    _workspace_delivery_token_limit,
)
from agent_hub.runtime.project_scale_artifact import project_scale_artifact_zip_files
from tests.contracts.test_runtime_contract import FakeGateway
from tests.unit.capabilities.test_scoped_read import FakeRunRepository, stored_run


class UnusedGateway:
    pass


def test_website_preview_accepts_common_public_entrypoint() -> None:
    assert _workspace_bundle_has_website_preview(
        {"files": {"public/preview.html": "<!doctype html><main>ready</main>"}}
    )


class RecordingCapabilityGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, JsonValue], str]] = []

    async def execute(
        self,
        *,
        tenant_id: object,
        run_id: object,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        del tenant_id, run_id, actor
        self.calls.append((name, arguments, idempotency_key))
        if name == "workspace.bundle":
            return {
                "summary": "Generated workspace ZIP artifact project.zip.",
                "artifact_id": str(uuid4()),
                "bundle_download_url": "/api/workspaces/project/session/bundle",
            }
        if name == "workspace.prune":
            return {"summary": "Pruned workspace.", "removed_paths": ()}
        return {"summary": f"Wrote {arguments['path']}."}

    def is_replay_safe(self, name: str) -> bool:
        return name in {"workspace.write_text", "workspace.prune", "workspace.bundle"}


def _wrapped_workspace_io_error(error: OSError) -> RuntimeCapabilityError:
    try:
        raise error
    except OSError as cause:
        try:
            raise RuntimeCapabilityError(str(cause)) from None
        except RuntimeCapabilityError as wrapped:
            return wrapped


class HostileWorkspaceError(RuntimeCapabilityError):
    @property
    def args(self) -> tuple[object, ...]:  # type: ignore[override]
        raise ValueError("private-content confidential secret")


@pytest.mark.parametrize(
    ("error", "expected_code"),
    (
        (RuntimeCapabilityError("content must not be empty"), "empty_content"),
        (RuntimeCapabilityError("workspace path must not contain hidden files"), "hidden_path"),
        (RuntimeCapabilityError("workspace write is not authorized"), "write_denied"),
        (RuntimeCapabilityError("workspace scope could not be resolved"), "scope_unavailable"),
        (RuntimeCapabilityError("workspace file is too large"), "file_too_large"),
        (RuntimeCapabilityError("private-content Bearer confidential"), "capability_failed"),
        (RuntimeCapabilityError("workspace path must not contain hidden files", "secret"),
         "capability_failed"),
        (PermissionError("private-path private-content"), "storage_permission"),
        (_wrapped_workspace_io_error(PermissionError("private-path")), "storage_permission"),
        (_wrapped_workspace_io_error(OSError(errno.ENOSPC, "private-path")), "storage_full"),
        (RuntimeCapabilityError("workspace path must be relative"), "invalid_path"),
        (_wrapped_workspace_io_error(TimeoutError("private-content")), "timeout"),
        (HostileWorkspaceError("private-content"), "capability_failed"),
        (TimeoutError("private-content"), "timeout"),
    ),
)
async def test_direct_workspace_failure_logs_only_fixed_diagnostic_code(
    error: Exception, expected_code: str, caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingCapabilities(RecordingCapabilityGateway):
        async def execute(self, **kwargs: object) -> Mapping[str, JsonValue]:
            raise error

    runtime = DirectRuntime(
        UnusedGateway(),  # type: ignore[arg-type]
        logical_model="main", capability_gateway=FailingCapabilities(),
    )
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DIRECT,
                          request="Build project")
    with pytest.raises(RuntimeExecutionError, match="^incremental workspace delivery failed$") as raised:
        await runtime._execute_workspace_capability(
            context, name="workspace.write_text",
            arguments={"path": "private-path", "content": "private-content"},
            idempotency_key="diagnostic", deadline=asyncio.get_running_loop().time() + 10,
        )

    assert f"failure_code={expected_code}" in caplog.text
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert str(context.run_id) in caplog.text
    for private in ("private-path", "private-content", "confidential", "secret"):
        assert private not in caplog.text


@pytest.mark.parametrize(
    ("path", "content", "expected_code"),
    (("src/empty.ts", "", "empty_content"),
     (".prettierrc", "{}", "hidden_path"),
     (" src/main.ts", "export {}", "invalid_path")),
)
async def test_direct_real_workspace_boundary_preserves_partial_files_and_safe_diagnostic(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    path: str, content: str, expected_code: str,
) -> None:
    context = TaskContext(run_id=uuid4(), tenant_id=uuid4(), mode=TaskMode.DIRECT,
                          request="Build project")
    workspace_root = tmp_path / "workspaces"
    capabilities = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills", project_workspace_dir=workspace_root,
        run_repository=FakeRunRepository(stored_run(
            tenant=context.tenant_id, run=context.run_id,
            project_id="diagnostic-project", session="diagnostic-session",
        )),
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main",  # type: ignore[arg-type]
                            capability_gateway=capabilities)
    prior_files = {".gitignore": "dist\n", "package.json": "{}",
                   "tsconfig.json": "{}", "vitest.config.ts": "export {}"}
    with pytest.raises(RuntimeExecutionError, match="^incremental workspace delivery failed$") as raised:
        await runtime._deliver_workspace_incrementally(
            context, {"files": {**prior_files, path: content}},
        )

    session_root = (workspace_root / str(context.tenant_id) / "projects"
                    / "diagnostic-project" / "sessions" / "diagnostic-session")
    assert {item.relative_to(session_root).as_posix(): item.read_text()
            for item in session_root.rglob("*") if item.is_file()} == prior_files
    assert f"failure_code={expected_code}" in caplog.text
    assert raised.value.__context__ is None
    assert path not in caplog.text


class SequencedDirectGateway:
    def __init__(self, outcomes: tuple[ModelResponse | BaseException, ...]) -> None:
        self._outcomes = list(outcomes)
        self.requests: list[object] = []

    async def complete_with_context(self, request: object) -> object:
        from agent_hub.models.gateway import GatewayCompletion

        self.requests.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return GatewayCompletion(
            response=outcome,
            deployment_id="primary",
            logical_model="main",
            provider_id="deepseek",
            provider_model="deepseek/deepseek-chat",
            attempted_logical_models=("main",),
        )


@pytest.mark.asyncio
async def test_direct_project_preflight_terminal_checkpoint_resumes_at_sequence_three() -> None:
    run_id = uuid4()
    tenant_id = uuid4()
    checkpoint = RuntimeCheckpoint(
        id=uuid4(),
        runtime_type="direct",
        runtime_version="1",
        run_id=run_id,
        tenant_id=tenant_id,
        mode=TaskMode.DIRECT,
        state={
            "completed": True,
            "artifact_id": str(uuid4()),
            "artifact_sha256": "a" * 64,
            "next_sequence": 3,
        },
    )
    context = TaskContext(
        run_id=run_id,
        tenant_id=tenant_id,
        mode=TaskMode.DIRECT,
        request="Resume completed project preflight.",
        checkpoint=checkpoint,
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    await runtime.restore_checkpoint(checkpoint)
    events = [event async for event in runtime.run(context)]

    assert len(events) == 1
    assert events[0].kind is EventKind.RUNTIME_COMPLETED
    assert events[0].sequence == 3


def test_project_scale_fixture_files_include_agent_standard_reading_evidence() -> None:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for path, content in project_scale_artifact_zip_files(
            "Project-scale acceptance fixture: build a small project for scale=small "
            "and flow=artifact_production."
        ).items():
            archive.writestr(path, content)

    assert _workspace_bundle_agent_standard_reasons(buffer.getvalue()) == ()


@pytest.mark.parametrize(
    ("scale", "api_resource", "domain_text"),
    (
        ("small", "/tasks", "Open tasks"),
        ("medium", "/tenants/:tenant/accounts", "Open deals"),
        ("large", "/orders", "Orders at risk"),
        ("ultra", "/portfolio/read-model", "Active programs"),
    ),
)
def test_project_scale_fixture_files_include_interactive_web_preview(
    scale: str,
    api_resource: str,
    domain_text: str,
) -> None:
    files = project_scale_artifact_zip_files(
        f"Build a real {scale} business project for flow=artifact_production. "
        "Also include a complete interactive website with a preview.html entrypoint."
    )

    preview = files["preview.html"]

    assert "<!doctype html>" in preview.casefold()
    assert "data-preview-action" in preview
    assert "addEventListener" in preview
    assert "@media" in preview
    assert api_resource in preview
    assert domain_text in preview


def test_project_scale_small_fixture_files_include_start_script() -> None:
    files = project_scale_artifact_zip_files(
        "Project-scale acceptance fixture: build a small project for scale=small "
        "and flow=artifact_production."
    )

    package_json = json.loads(files["package.json"])

    assert package_json["scripts"]["build"] == "node --check src/main.js && node --check src/server.js"
    assert package_json["scripts"]["test"] == "node --test"
    assert package_json["scripts"]["start"] == "node src/server.js"
    assert package_json["dependencies"] == {}
    assert package_json["devDependencies"] == {}
    assert "tsconfig.json" not in files
    assert "src/main.js" in files
    assert "src/server.js" in files
    assert "tests/app.test.js" in files
    assert "GET' && url.pathname === '/tasks'" in files["src/server.js"]
    assert "supports create/list/update/delete/restore" in files["VERIFICATION.md"]


def test_project_scale_medium_fixture_files_are_tenant_crm() -> None:
    files = project_scale_artifact_zip_files(
        "Build a real medium business project for flow=direct. "
        "Acceptance conditions require tenant CRM APIs."
    )

    package_json = json.loads(files["package.json"])

    assert package_json["dependencies"] == {}
    assert package_json["devDependencies"] == {}
    assert package_json["scripts"]["build"] == "node --check src/server.js"
    assert package_json["scripts"]["test"] == "node --test"
    assert {"package.json", "src/server.js", "tests/crm.test.js"}.issubset(files)
    assert "parts[0] !== 'tenants'" in files["src/server.js"]
    assert "opportunities" in files["PROJECT_REQUIREMENTS.md"]
    assert "tenant-isolated" in files["README.md"]


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


def test_direct_prompt_compacts_artifact_text_to_small_runtime_window() -> None:
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
        token_budget=4_096,
        routing_decision={"main_agent_context_window_tokens": 4_096},
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    request = runtime._build_request(context).request

    assert request is not None
    user_content = request.messages[-1].content
    assert isinstance(user_content, str)
    assert original_text not in user_content
    assert '"compacted_artifact_count":1' in user_content
    assert request.max_output_tokens <= 8192
    assert len(user_content.encode("utf-8")) < len(original_text.encode("utf-8"))
    assert artifact.content["text"] == original_text


def test_direct_prompt_compacts_many_artifacts_at_soft_waterline() -> None:
    artifacts = tuple(
        Artifact(
            id=uuid4(),
            type="text",
            producer="planner",
            content={"text": f"artifact-{index}:" + "x" * 60_000},
        )
        for index in range(64)
    )
    task = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="Synthesize every referenced artifact without dropping provenance.",
        artifacts=artifacts,
        token_budget=1_000_000,
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    outcome = runtime._build_prompt(task)

    assert outcome.messages is not None
    assert outcome.error_code is None
    assert outcome.included_source_ids == tuple(str(artifact.id) for artifact in artifacts)
    assert outcome.prompt_estimate > 196_608
    assert outcome.prompt_estimate < task.token_budget
    rendered = "\n".join(cast(str, item.content) for item in outcome.messages)
    assert "compacted_artifact_count" in rendered


def test_direct_prompt_uses_larger_runtime_window_for_artifact_history() -> None:
    marker = "EARLY_DECISION_MUST_SURVIVE"
    artifact = Artifact(
        id=uuid4(),
        type="text",
        producer="context_loader",
        content={"text": "x" * 55_000 + marker},
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    small = runtime._build_prompt(
        TaskContext(
            run_id=uuid4(),
            tenant_id=uuid4(),
            mode=TaskMode.DIRECT,
            request="Recall the earlier decision.",
            artifacts=(artifact,),
            token_budget=8_192,
            routing_decision={"main_agent_context_window_tokens": 8_192},
        )
    )
    large = runtime._build_prompt(
        TaskContext(
            run_id=uuid4(),
            tenant_id=uuid4(),
            mode=TaskMode.DIRECT,
            request="Recall the earlier decision.",
            artifacts=(artifact,),
            token_budget=128_000,
            routing_decision={"main_agent_context_window_tokens": 128_000},
        )
    )

    assert small.messages is not None
    assert large.messages is not None
    small_text = "\n".join(cast(str, message.content) for message in small.messages)
    large_text = "\n".join(cast(str, message.content) for message in large.messages)
    assert marker not in small_text
    assert marker in large_text
    assert large.prompt_estimate > small.prompt_estimate


def test_direct_request_respects_deployment_context_window() -> None:
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="Summarize the project.",
        token_budget=128_000,
        routing_decision={"main_agent_context_window_tokens": 8_192},
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    outcome = runtime._build_request(context)

    assert outcome.request is not None
    assert outcome.prompt_estimate + outcome.request.max_output_tokens <= 8_192


@pytest.mark.parametrize("scale", ("small", "medium", "large", "ultra"))
def test_direct_project_output_budget_uses_remaining_runtime_budget_not_scale_cap(
    scale: str,
) -> None:
    task = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request=f"Build a {scale} project.",
        token_budget=200_000,
        routing_decision={
            "project_scale": scale,
            "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle",
        },
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    outcome = runtime._build_request(task)

    assert outcome.request is not None
    assert outcome.request.max_output_tokens == task.token_budget - outcome.prompt_estimate
    assert outcome.request.max_output_tokens > 65_536


def test_direct_workspace_bundle_limits_are_storage_fuses_not_inline_waterlines() -> None:
    over_inline_file_count = {
        "files": {f"src/file_{index}.txt": "ok" for index in range(201)}
    }
    over_inline_bytes = {
        "files": {
            "src/a.txt": "a" * 400_000,
            "src/b.txt": "b" * 400_000,
            "src/c.txt": "c" * 400_000,
        }
    }

    assert _normalized_workspace_bundle(over_inline_file_count) is not None
    assert _normalized_workspace_bundle(over_inline_bytes) is not None
    assert _normalized_workspace_bundle(
        {"files": {f"src/file_{index}.txt": "ok" for index in range(513)}}
    ) is None


@pytest.mark.asyncio
async def test_direct_large_bundle_switches_to_incremental_workspace_delivery() -> None:
    files = {
        f"src/module_{index}.txt": (str(index) * 6_000)
        for index in range(201)
    }
    response_text = json.dumps({"workspace_bundle": {"files": files}})
    gateway = FakeGateway(
        ModelResponse(text=response_text, usage=TokenUsage(100, 100_000, 100_100))
    )
    capabilities = RecordingCapabilityGateway()
    runtime = DirectRuntime(
        gateway,
        logical_model="main",
        capability_gateway=capabilities,
    )
    task = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="Build a large project.",
        timeout_seconds=600,
        token_budget=500_000,
        routing_decision={
            "project_scale": "large",
            "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle",
        },
    )

    events = [event async for event in runtime.run(task)]

    assert [name for name, _arguments, _key in capabilities.calls].count(
        "workspace.write_text"
    ) == 202
    assert capabilities.calls[-2][1]["path"] == "DELIVERY_MANIFEST.json"
    assert capabilities.calls[-1][0] == "workspace.bundle"
    artifact_event = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED)
    assert artifact_event.artifact is not None
    assert "workspace_bundle" not in artifact_event.artifact.content
    delivery = artifact_event.artifact.content["workspace_delivery"]
    assert isinstance(delivery, Mapping)
    assert delivery["bundle_download_url"] == "/api/workspaces/project/session/bundle"


@pytest.mark.parametrize(
    ("text", "expected_files", "expected_complete"),
    (
        (
            """```json
{"workspace_batch":{"files":{"src/main.js":"export const ready = true;\\n"},"complete":true,"continuation":""},"summary":"done"}
```""",
            {"src/main.js": "export const ready = true;\n"},
            True,
        ),
        (
            (
                "Here is the requested batch:\n"
                '{"workspace_batch":{"files":{"README.md":"ready\\n"},'
                '"complete":false,"continuation":"write tests"}}\n'
                "The JSON above is the machine-readable result."
            ),
            {"README.md": "ready\n"},
            False,
        ),
        (
            (
                '{"workspace_bundle":{"files":{"package.json":"{}\\n"}},'
                '"summary":"complete bundle"}'
            ),
            {"package.json": "{}\n"},
            True,
        ),
        (
            '{"files":{"index.html":"<main>ready</main>\\n"}}',
            {"index.html": "<main>ready</main>\n"},
            True,
        ),
        (
            (
                '{"workspace_batch":{"files":{"README.md":"ready\\n"},'
                '"complete":true,"continuation":null},"summary":"done"}'
            ),
            {"README.md": "ready\n"},
            True,
        ),
        (
            """Project files follow.\n\n### `package.json`\n```json\n{\"scripts\":{\"test\":\"node --test\"}}\n```\n\n### `src/main.js`\n```js\nexport const ready = true;\n```""",
            {
                "package.json": '{"scripts":{"test":"node --test"}}\n',
                "src/main.js": "export const ready = true;\n",
            },
            False,
        ),
    ),
)
def test_workspace_batch_parser_accepts_real_model_json_variants(
    text: str,
    expected_files: dict[str, str],
    expected_complete: bool,
) -> None:
    batch = _workspace_batch_from_model_text(text)

    assert batch is not None
    assert batch.files == expected_files
    assert batch.complete is expected_complete


def test_workspace_batch_parser_recovers_complete_files_from_truncated_json() -> None:
    text = """```json
{"workspace_batch":{"files":{
  "package.json":"{\\"scripts\\":{\\"test\\":\\"node --test\\"}}\\n",
  "src/main.js":"export const ready = true;\\n",
  "src/incomplete.js":"export const unfinished =
"""

    batch = _workspace_batch_from_model_text(text)

    assert batch is not None
    assert batch.files == {
        "package.json": '{"scripts":{"test":"node --test"}}\n',
        "src/main.js": "export const ready = true;\n",
    }
    assert batch.complete is False
    assert batch.continuation == "continue with the remaining project files"


def test_workspace_batch_parser_normalizes_structured_continuation() -> None:
    text = json.dumps(
        {
            "workspace_batch": {
                "files": {"src/main.js": "export const ready = true;\n"},
                "complete": False,
                "continuation": {
                    "remaining_files": ["README.md", "tests/main.test.js"],
                    "delivered": ["src/main.js"],
                },
            }
        }
    )

    batch = _workspace_batch_from_model_text(text)

    assert batch is not None
    assert json.loads(batch.continuation) == {
        "delivered": ["src/main.js"],
        "remaining_files": ["README.md", "tests/main.test.js"],
    }
    assert batch.complete is False


def test_workspace_batch_parser_rejects_truncated_json_without_complete_file() -> None:
    text = '{"workspace_batch":{"files":{"src/incomplete.js":"unfinished'

    assert _workspace_batch_from_model_text(text) is None


@pytest.mark.parametrize(
    "text",
    (
        '{"workspace_batch":{"files":{},"complete":true,"continuation":""}}',
        '{"workspace_batch":{"files":{"../escape.txt":"no"},"complete":true,"continuation":""}}',
        '{"workspace_batch":{"files":{"src/main.py":42},"complete":true,"continuation":""}}',
        '{"workspace_batch":{"files":{"src/main.py":"ok"},"complete":false,"continuation":null}}',
        "I created the project files and everything is ready.",
    ),
)
def test_workspace_batch_parser_rejects_unsafe_or_non_file_outputs(text: str) -> None:
    assert _workspace_batch_from_model_text(text) is None


@pytest.mark.asyncio
async def test_direct_project_delivery_writes_fenced_model_batch() -> None:
    gateway = SequencedDirectGateway(
        (
            ModelResponse(
                text="""```json
{"workspace_batch":{"files":{"package.json":"{\\"scripts\\":{\\"test\\":\\"node --test\\"}}"},"complete":true,"continuation":""},"summary":"Project completed."}
```""",
                usage=TokenUsage(100, 80, 180),
            ),
        )
    )
    capabilities = RecordingCapabilityGateway()
    runtime = DirectRuntime(
        gateway,  # type: ignore[arg-type]
        logical_model="main",
        capability_gateway=capabilities,
    )
    task = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="Build a real project workspace.",
        timeout_seconds=600,
        token_budget=50_000,
        routing_decision={
            "project_scale": "large",
            "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle",
        },
    )

    events = [event async for event in runtime.run(task)]

    assert [call[0] for call in capabilities.calls] == [
        "workspace.write_text",
        "workspace.write_text",
        "workspace.bundle",
    ]
    assert capabilities.calls[0][1] == {
        "path": "package.json",
        "content": '{"scripts":{"test":"node --test"}}',
    }
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


@pytest.mark.asyncio
async def test_direct_authoritative_workspace_delivery_prunes_after_all_project_files() -> None:
    gateway = SequencedDirectGateway(
        (
            ModelResponse(
                text=json.dumps(
                    {
                        "workspace_bundle": {
                            "files": {
                                "package.json": '{"scripts":{"test":"node --test"}}',
                                "README.md": "# Project\n",
                                "IMPLEMENTATION_PLAN.md": "# Plan\n",
                                "VERIFICATION.md": "# Verification\n",
                                "src/main.js": "export const ready = true;\n",
                                "tests/main.test.js": "// test\n",
                            }
                        }
                    }
                ),
                usage=TokenUsage(100, 80, 180),
            ),
        )
    )
    capabilities = RecordingCapabilityGateway()
    runtime = DirectRuntime(
        gateway,  # type: ignore[arg-type]
        logical_model="main",
        capability_gateway=capabilities,
    )
    task = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="Repair the complete project workspace.",
        timeout_seconds=600,
        token_budget=50_000,
        routing_decision={
            "project_scale": "large",
            "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle",
            "replace_workspace_files": True,
        },
    )

    events = [event async for event in runtime.run(task)]

    assert [call[0] for call in capabilities.calls] == [
        "workspace.write_text",
        "workspace.write_text",
        "workspace.write_text",
        "workspace.write_text",
        "workspace.write_text",
        "workspace.write_text",
        "workspace.prune",
        "workspace.write_text",
        "workspace.bundle",
    ]
    assert capabilities.calls[6][1] == {
        "keep_paths": (
            "IMPLEMENTATION_PLAN.md",
            "README.md",
            "VERIFICATION.md",
            "package.json",
            "src/main.js",
            "tests/main.test.js",
        )
    }
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


@pytest.mark.asyncio
async def test_direct_project_delivery_generates_and_writes_multiple_model_batches() -> None:
    gateway = SequencedDirectGateway(
        (
            ModelResponse(
                text=json.dumps(
                    {
                        "workspace_batch": {
                            "files": {"package.json": '{"scripts":{"test":"node --test"}}'},
                            "complete": False,
                            "continuation": "continue with source and tests",
                        },
                        "summary": "Project manifest written.",
                    }
                ),
                usage=TokenUsage(100, 80, 180),
            ),
            ModelResponse(
                text=json.dumps(
                    {
                        "workspace_batch": {
                            "files": {
                                "src/main.js": "export const ready = true;\n",
                                "tests/main.test.js": "// verified\n",
                                "IMPLEMENTATION_PLAN.md": (
                                    "Read before implementation: AGENTS.md workspace rules, "
                                    "HANDOFF current-state index, PROJECT_REQUIREMENTS.md, "
                                    "and applicable SKILL.md agent-standard rules.\n"
                                ),
                                "VERIFICATION.md": "npm run build and npm test passed.\n",
                            },
                            "complete": True,
                            "continuation": "",
                        },
                        "summary": "Project source and tests completed.",
                    }
                ),
                usage=TokenUsage(120, 100, 220),
            ),
        )
    )
    capabilities = RecordingCapabilityGateway()
    runtime = DirectRuntime(
        gateway,  # type: ignore[arg-type]
        logical_model="main",
        capability_gateway=capabilities,
    )
    task = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="Build a large project with source and tests.",
        timeout_seconds=600,
        token_budget=50_000,
        routing_decision={
            "project_scale": "large",
            "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle",
        },
    )

    events = [event async for event in runtime.run(task)]

    assert len(gateway.requests) == 2
    assert [call[1]["path"] for call in capabilities.calls[:-1]] == [
        "package.json",
        "src/main.js",
        "tests/main.test.js",
        "IMPLEMENTATION_PLAN.md",
        "VERIFICATION.md",
        "DELIVERY_MANIFEST.json",
    ]
    assert capabilities.calls[-1][0] == "workspace.bundle"
    second_request = cast(ModelRequest, gateway.requests[1])
    rendered = "\n".join(cast(str, message.content) for message in second_request.messages)
    assert "continue with source and tests" in rendered
    assert "package.json" not in rendered
    completed = next(event for event in events if event.kind is EventKind.RUNTIME_COMPLETED)
    assert completed.payload["workspace_delivery"]
    agent_standard = cast(
        Mapping[str, JsonValue],
        completed.payload["agent_standard_verification"],
    )
    assert agent_standard["constraints_read"] is True


@pytest.mark.asyncio
async def test_direct_project_delivery_keeps_request_window_separate_from_run_budget() -> None:
    responses = tuple(
        ModelResponse(
            text=json.dumps(
                {
                    "workspace_batch": {
                        "files": {f"src/part-{index}.txt": f"part {index}\n"},
                        "complete": index == 2,
                        "continuation": "continue" if index < 2 else "",
                    },
                    "summary": f"Part {index} written.",
                }
            ),
            usage=TokenUsage(200, 300, 500),
        )
        for index in range(3)
    )
    gateway = SequencedDirectGateway(responses)
    runtime = DirectRuntime(
        gateway,  # type: ignore[arg-type]
        logical_model="main",
        capability_gateway=RecordingCapabilityGateway(),
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="Build a large project in several workspace batches.",
        timeout_seconds=600,
        token_budget=5_000,
        routing_decision={
            "project_scale": "large",
            "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle",
            "main_agent_context_window_tokens": 1_000,
        },
    )

    events = [event async for event in runtime.run(context)]

    assert len(gateway.requests) == 3
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


def test_workspace_delivery_bootstrap_earns_one_dynamic_progress_slice() -> None:
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="Build a project after a long model response.",
        timeout_seconds=600,
        token_budget=5_000,
        routing_decision={
            "project_scale": "small",
            "project_delivery": "workspace",
            "runtime_timeout_source": "project_scale_soft_budget",
            "runtime_timeout_soft_seconds": 300.0,
            "runtime_timeout_absolute_seconds": 3_600.0,
            "critical_path_complexity_units": 6,
        },
    )

    assert _workspace_delivery_initial_seconds(
        context,
        initial_remaining_seconds=0.001,
        absolute_remaining_seconds=3_000.0,
    ) == 50.0


def test_workspace_delivery_bootstrap_respects_remaining_absolute_budget() -> None:
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="Build a project at its absolute deadline.",
        timeout_seconds=600,
        token_budget=5_000,
        routing_decision={
            "runtime_timeout_source": "project_scale_soft_budget",
            "runtime_timeout_soft_seconds": 1_200.0,
            "runtime_timeout_absolute_seconds": 3_600.0,
            "critical_path_complexity_units": 4,
        },
    )

    assert _workspace_delivery_initial_seconds(
        context,
        initial_remaining_seconds=0.001,
        absolute_remaining_seconds=12.0,
    ) == 12.0


def test_workspace_delivery_bootstrap_scales_with_initial_batch_work() -> None:
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="Build a multi-file project after a long model response.",
        timeout_seconds=600,
        token_budget=5_000,
        routing_decision={
            "runtime_timeout_source": "project_scale_soft_budget",
            "runtime_timeout_soft_seconds": 300.0,
            "runtime_timeout_absolute_seconds": 3_600.0,
            "critical_path_complexity_units": 6,
        },
    )

    assert _workspace_delivery_initial_seconds(
        context,
        initial_remaining_seconds=0.001,
        absolute_remaining_seconds=3_000.0,
        initial_progress_units=8,
    ) == 300.0


@pytest.mark.asyncio
@pytest.mark.parametrize("project_scale", ("large", "ultra"))
async def test_direct_workspace_batches_extend_soft_token_budget_from_progress(
    project_scale: str,
) -> None:
    gateway = SequencedDirectGateway(
        (
            ModelResponse(
                text=json.dumps(
                    {
                        "workspace_batch": {
                            "files": {"src/first.txt": "first\n"},
                            "complete": False,
                            "continuation": "continue",
                        }
                    }
                ),
                usage=TokenUsage(1_500, 2_000, 3_500),
            ),
            ModelResponse(
                text=json.dumps(
                    {
                        "workspace_batch": {
                            "files": {"src/second.txt": "second\n"},
                            "complete": True,
                            "continuation": "",
                        }
                    }
                ),
                usage=TokenUsage(800, 1_200, 2_000),
            ),
        )
    )
    runtime = DirectRuntime(
        gateway,  # type: ignore[arg-type]
        logical_model="main",
        capability_gateway=RecordingCapabilityGateway(),
    )
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request=f"Build a {project_scale} project in multiple batches.",
        timeout_seconds=600,
        token_budget=5_000,
        routing_decision={
            "project_scale": project_scale,
            "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle",
            "critical_path_complexity_units": 4,
            "runtime_token_soft_base_tokens": 2_000,
            "runtime_token_absolute_tokens": 6_500,
        },
    )

    events = [event async for event in runtime.run(context)]

    assert len(gateway.requests) == 2
    assert events[-1].kind is EventKind.RUNTIME_COMPLETED


def test_direct_workspace_budget_does_not_grow_without_completed_progress() -> None:
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="Build an ultra project.",
        token_budget=5_000,
        routing_decision={
            "project_scale": "ultra",
            "critical_path_complexity_units": 4,
            "runtime_token_soft_base_tokens": 2_000,
            "runtime_token_absolute_tokens": 9_000,
        },
    )

    assert _workspace_delivery_token_limit(
        context,
        initial_soft_limit=5_000,
        completed_files=0,
        completed_batches=0,
        consumed_tokens=4_900,
        last_batch_tokens=4_900,
    ) == 5_000


def test_direct_workspace_budget_never_exceeds_configured_hard_limit() -> None:
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="Build an ultra project.",
        token_budget=5_000,
        routing_decision={
            "project_scale": "ultra",
            "critical_path_complexity_units": 1,
            "runtime_token_soft_base_tokens": 2_000,
            "runtime_token_absolute_tokens": 5_500,
        },
    )

    assert _workspace_delivery_token_limit(
        context,
        initial_soft_limit=5_000,
        completed_files=20,
        completed_batches=20,
        consumed_tokens=5_400,
        last_batch_tokens=4_000,
    ) == 5_500


@pytest.mark.asyncio
async def test_direct_retry_budget_allows_distinct_retryable_failures_then_success() -> None:
    gateway = SequencedDirectGateway(
        (
            ModelTransportError("model transport failed", status_code=503),
            ModelTransportError("model transport failed", status_code=429),
            ModelResponse(text="Recovered", usage=TokenUsage(10, 2, 12)),
        )
    )
    runtime = DirectRuntime(
        gateway,  # type: ignore[arg-type]
        logical_model="main",
        available_model_attempts=3,
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.DIRECT,
                request="Build a medium project.",
                timeout_seconds=60,
                token_budget=20_000,
            )
        )
    ]

    assert events[-1].kind is EventKind.RUNTIME_COMPLETED
    assert len(gateway.requests) == 3


@pytest.mark.asyncio
async def test_direct_retry_budget_stops_on_repeated_error_fingerprint() -> None:
    gateway = SequencedDirectGateway(
        (
            ModelTransportError("model transport failed", status_code=503),
            ModelTransportError("model transport failed", status_code=503),
            ModelResponse(text="must not be reached", usage=TokenUsage(10, 2, 12)),
        )
    )
    runtime = DirectRuntime(
        gateway,  # type: ignore[arg-type]
        logical_model="main",
        available_model_attempts=3,
    )

    with pytest.raises(RuntimeExecutionError, match="model transport failed"):
        _ = [
            event
            async for event in runtime.run(
                TaskContext(
                    run_id=uuid4(),
                    tenant_id=uuid4(),
                    mode=TaskMode.DIRECT,
                    request="Build a medium project.",
                    timeout_seconds=60,
                    token_budget=20_000,
                )
            )
        ]

    assert len(gateway.requests) == 2


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


def test_direct_natural_website_request_receives_workspace_preview_contract() -> None:
    context = TaskContext(
        run_id=uuid4(),
        tenant_id=uuid4(),
        mode=TaskMode.DIRECT,
        request="编写一个网盘网站",
        timeout_seconds=1200,
        token_budget=100_000,
        routing_decision={
            "project_scale": "large",
            "project_delivery": "workspace",
            "artifact_strategy": "workspace_bundle",
            "website_preview_required": True,
        },
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    model_request = runtime._build_request(context).request

    assert model_request is not None
    assert model_request.max_output_tokens > 8_192
    serialized = "\n".join(cast(str, message.content) for message in model_request.messages)
    assert "workspace_bundle.files" in serialized
    assert "preview.html" in serialized
    assert "self-contained" in serialized


@pytest.mark.asyncio
async def test_direct_natural_website_wraps_single_html_block_as_preview_workspace_file() -> None:
    response_text = """已完成可运行预览：
```html
<!doctype html><html><body><button id="upload">上传</button></body></html>
```
"""
    capabilities = RecordingCapabilityGateway()
    runtime = DirectRuntime(
        FakeGateway(ModelResponse(text=response_text, usage=TokenUsage(20, 30, 50))),
        logical_model="main",
        capability_gateway=capabilities,
    )

    events = [
        event
        async for event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.DIRECT,
                request="编写一个网盘网站",
                timeout_seconds=1200,
                token_budget=100_000,
                routing_decision={
                    "project_scale": "large",
                    "project_delivery": "workspace",
                    "artifact_strategy": "workspace_bundle",
                    "website_preview_required": True,
                },
            )
        )
    ]

    artifact_event = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED)
    assert artifact_event.artifact is not None
    assert artifact_event.artifact.type == "tool_result"
    assert artifact_event.artifact.content["artifact_origin"] == "model_workspace_bundle"
    workspace_delivery = cast(
        Mapping[str, JsonValue], artifact_event.artifact.content["workspace_delivery"]
    )
    assert workspace_delivery["artifact_origin"] == "incremental_workspace_delivery"
    assert [name for name, _arguments, _key in capabilities.calls] == [
        "workspace.write_text",
        "workspace.write_text",
        "workspace.bundle",
    ]
    assert capabilities.calls[0][1] == {
        "path": "preview.html",
        "content": (
            '<!doctype html><html><body><button id="upload">上传</button></body></html>\n'
        ),
    }


@pytest.mark.asyncio
async def test_direct_website_bundle_without_preview_entry_is_rejected() -> None:
    response_text = json.dumps(
        {
            "workspace_bundle": {
                "files": {
                    "README.md": "# Website\n",
                    "src/main.js": "console.log('ready');\n",
                }
            }
        }
    )
    runtime = DirectRuntime(
        FakeGateway(ModelResponse(text=response_text, usage=TokenUsage(20, 30, 50))),
        logical_model="main",
    )

    with pytest.raises(RuntimeExecutionError, match="website preview entry is missing"):
        async for _event in runtime.run(
            TaskContext(
                run_id=uuid4(),
                tenant_id=uuid4(),
                mode=TaskMode.DIRECT,
                request="创建一个简单网站",
                timeout_seconds=300,
                token_budget=100_000,
                routing_decision={
                    "project_scale": "small",
                    "project_delivery": "workspace",
                    "artifact_strategy": "workspace_bundle",
                    "website_preview_required": True,
                },
            )
        ):
            pass


@pytest.mark.asyncio
async def test_direct_capability_request_without_model_workspace_bundle_fails() -> None:
    request = (
        "Build a real medium business project for flow=direct. Return full bundle as "
        "workspace_bundle.files with package.json build/test/start scripts. "
        "Acceptance conditions: source, tests, verification, and interaction evidence."
    )
    runtime = DirectRuntime(
        FakeGateway(ModelResponse(text="Here is a short summary only.", usage=TokenUsage(20, 8, 28))),
        logical_model="main",
    )

    with pytest.raises(RuntimeExecutionError, match="workspace bundle is missing"):
        _ = [
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
async def test_direct_capability_request_uses_model_bundle_and_materializes_workspace() -> None:
    request = (
        "Build a real large business project for flow=direct. Return full bundle as "
        "workspace_bundle.files with package.json build/test/start scripts. "
        "Acceptance conditions: source, tests, verification, and interaction evidence."
    )
    capabilities = RecordingCapabilityGateway()
    model_bundle = {
        "workspace_bundle": {
            "files": {
                "README.md": "# Real model project\n",
                "src/server.js": "export const ready = true;\n",
            }
        }
    }
    runtime = DirectRuntime(
        FakeGateway(ModelResponse(text=json.dumps(model_bundle), usage=TokenUsage(20, 8, 28))),
        logical_model="main",
        capability_gateway=capabilities,
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

    assert any(event.kind is EventKind.MODEL_STARTED for event in events)
    artifact_event = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED)
    assert artifact_event.artifact is not None
    assert artifact_event.artifact.content["artifact_origin"] == "model_workspace_bundle"
    assert artifact_event.payload["artifact_origin"] == "model_workspace_bundle"
    assert [name for name, _arguments, _key in capabilities.calls] == [
        "workspace.write_text",
        "workspace.write_text",
        "workspace.write_text",
        "workspace.bundle",
    ]


@pytest.mark.asyncio
async def test_direct_large_capability_request_does_not_replace_model_workspace_bundle() -> None:
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
    capabilities = RecordingCapabilityGateway()
    runtime = DirectRuntime(
        FakeGateway(ModelResponse(text=json.dumps(bad_bundle), usage=TokenUsage(60, 20, 80))),
        logical_model="main",
        capability_gateway=capabilities,
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
    assert artifact_event.artifact.content["artifact_origin"] == "model_workspace_bundle"
    writes = [arguments for name, arguments, _key in capabilities.calls if name == "workspace.write_text"]
    assert {item["path"] for item in writes} == {
        "package.json",
        "broken.js",
        "DELIVERY_MANIFEST.json",
    }
    assert all("tests/order-ops.test.js" not in str(item) for item in writes)


@pytest.mark.asyncio
async def test_direct_ultra_capability_request_uses_model_workspace_bundle() -> None:
    request = (
        "Build a real ultra-large business project for flow=direct. Return full bundle as "
        "workspace_bundle.files with package.json build/test/start scripts. "
        "Acceptance conditions: enterprise portfolio OS APIs, analytics, RBAC, persistence, "
        "source, tests, verification, and interaction evidence."
    )
    model_bundle = {
        "workspace_bundle": {
            "files": {
                "README.md": "# Portfolio generated by model\n",
                "src/app.ts": "export const portfolio = true;\n",
            }
        }
    }
    capabilities = RecordingCapabilityGateway()
    runtime = DirectRuntime(
        FakeGateway(ModelResponse(text=json.dumps(model_bundle), usage=TokenUsage(60, 20, 80))),
        logical_model="main",
        capability_gateway=capabilities,
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

    assert any(event.kind is EventKind.MODEL_STARTED for event in events)
    artifact_event = next(event for event in events if event.kind is EventKind.ARTIFACT_CREATED)
    assert artifact_event.artifact is not None
    assert artifact_event.artifact.content["artifact_origin"] == "model_workspace_bundle"
    writes = [arguments for name, arguments, _key in capabilities.calls if name == "workspace.write_text"]
    assert {item["path"] for item in writes} == {
        "README.md",
        "src/app.ts",
        "DELIVERY_MANIFEST.json",
    }


@pytest.mark.asyncio
async def test_direct_ultra_capability_request_does_not_emit_fixture_without_gateway() -> None:
    request = (
        "Build a real ultra business project for flow=direct. Build an ultra-large project: "
        "a TypeScript/Node enterprise project portfolio operating system. Return strict JSON "
        "workspace_bundle.files. Acceptance conditions: source, tests, verification, "
        "portfolio APIs, RBAC, analytics, and persistence."
    )
    runtime = DirectRuntime(UnusedGateway(), logical_model="main")  # type: ignore[arg-type]

    with pytest.raises(RuntimeExecutionError, match="model gateway failed"):
        _ = [
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
