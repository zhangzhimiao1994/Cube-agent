import copy
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Mapping, Sequence
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol, Self, cast
from urllib.error import HTTPError

import pytest

from agent_hub.domain.runs import TaskMode
from agent_hub.harness import project_scale_runner as project_scale_runner_module
from agent_hub.harness.project_scale import (
    PROJECT_SCALE_FLOW_KINDS,
    PROJECT_SCALE_TIERS,
    ProjectScaleBenchmarkKind,
    ProjectScaleRunPlan,
    ProjectScaleRunRequest,
    build_project_scale_run_plan,
)
from agent_hub.harness.project_scale_runner import (
    ProjectScaleCaseResult,
    ProjectScaleExecutionReport,
    UrllibAcceptanceClient,
    _acceptance_credentials_from_env,
    _bundle_has_build_test_execution_evidence,
    _deliverable_repair_body,
    _discussion_trace_payload_passes,
    _drop_recovered_workspace_bundle_errors,
    _embedded_workspace_bundle_from_text,
    _evaluate_agent_standard_verification,
    _has_agent_standard_verification,
    _has_deliverable_repair_trace,
    _has_self_repair_trace,
    _multi_agent_participation,
    _plugin_contract_payload_passes,
    _safe_zip_member_path,
    _should_attempt_deliverable_repair,
    _workspace_bundle_agent_standard_reasons,
    execute_project_scale_plan,
    format_project_scale_result_line,
)
from agent_hub.harness.project_validation_sandbox import (
    generated_command as sandbox_generated_command,
)
from agent_hub.runtime.project_scale_artifact import project_scale_artifact_zip_files
from agent_hub.runtime.role_planner import RolePlanningRequest

_AGENT_STANDARD_IMPLEMENTATION_PLAN = (
    "- Read before implementation: AGENTS.md workspace rules, HANDOFF current-state index, "
    "and PROJECT_REQUIREMENTS.md.\n"
    "- Skill/rule sources checked before implementation: AGENTS.md workspace rules, "
    "applicable SKILL.md inventory, and no project-specific SKILL.md required for this fixture.\n"
    "- Build project\n"
)


@pytest.fixture
def trusted_python_fixture_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run only test-authored commands; production never permits this host backend."""
    original = sandbox_generated_command

    def fixture_command(
        command: Sequence[str], *, cwd: Path, config: Mapping[str, str],
    ) -> list[str]:
        if command[0] == sys.executable:
            return [sys.executable, "-I", *command[1:]]
        return original(command, cwd=cwd, config=config)

    monkeypatch.setattr(project_scale_runner_module, "generated_command", fixture_command)


def test_default_generated_project_install_disables_dependency_lifecycle_scripts() -> None:
    assert project_scale_runner_module._DEFAULT_GENERATED_PROJECT_COMMANDS[0] == (
        "npm",
        "install",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    )


def test_ultra_generated_bundle_requires_independent_load_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(project_scale_runner_module, "_run_generated_project_command",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(project_scale_runner_module, "validate_ultra_portfolio_api",
                        lambda *args, **kwargs: ())

    def load_check(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append("load")
        return {"status": "unknown", "reasons": ["not executed"]}

    monkeypatch.setattr(project_scale_runner_module, "validate_ultra_portfolio_storage",
                        load_check, raising=False)
    result = project_scale_runner_module._validate_generated_project_bundle(
        _project_bundle({"package.json": "{}"}), commands=(("npm", "test"),),
        timeout_seconds=1, requirements_case_id="ultra:direct",
    )

    assert result.passed is False
    assert calls == ["load"]


def _ultra_load_result() -> dict[str, object]:
    fixture = Path(__file__).resolve().parents[2] / "fixtures/project_business/ultra_storage_result.json"
    return cast(dict[str, object], json.loads(fixture.read_text(encoding="utf-8")))


def test_storage_gate_rejects_legacy_load_only_binding() -> None:
    fixture = Path(__file__).resolve().parents[2] / "fixtures/project_business/ultra_load_result.json"
    legacy = json.loads(fixture.read_text(encoding="utf-8"))
    assert project_scale_runner_module._bind_scale_validation(
        legacy, "ultra:direct", "run-ultra", {"src/app.js": (1, "a" * 64)},
    ) is None


def test_storage_freezes_before_legacy_business_probe_can_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    fixture = Path(__file__).resolve().parents[2] / "fixtures/project_business/ultra_storage_result.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    monkeypatch.setattr(project_scale_runner_module, "_run_generated_project_command",
                        lambda *args, **kwargs: None)

    def storage(root: Path, timeout: float) -> dict[str, object]:
        assert not (root / "contaminated").exists()
        calls.append("storage")
        return cast(dict[str, object], payload)

    def business(root: Path, timeout_seconds: float) -> tuple[str, ...]:
        (root / "contaminated").write_text("legacy app state", encoding="utf-8")
        calls.append("business")
        return ()

    monkeypatch.setattr(project_scale_runner_module, "validate_ultra_portfolio_storage",
                        storage, raising=False)
    monkeypatch.setattr(project_scale_runner_module, "validate_ultra_portfolio_api", business)
    checked = project_scale_runner_module._validate_generated_project_bundle(
        _project_bundle({"package.json": "{}"}), commands=(("npm", "test"),),
        timeout_seconds=10, requirements_case_id="ultra:direct",
    )
    assert checked.passed, checked.reasons
    assert calls == ["storage", "business"]


def test_storage_unavailable_does_not_invoke_model_repair_or_bind_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.harness.project_validation_result import scale_validation_unknown

    monkeypatch.setattr(project_scale_runner_module, "_run_generated_project_command",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(project_scale_runner_module, "validate_ultra_portfolio_storage",
                        lambda *args: scale_validation_unknown(
                            "nested namespace unavailable", profile="ultra-load-storage-v1",
                        ))
    checked = project_scale_runner_module._validate_generated_project_bundle(
        _project_bundle({"package.json": "{}"}), commands=(("npm", "test"),),
        timeout_seconds=10, requirements_case_id="ultra:direct",
    )
    assert not checked.passed and checked.scale_validation is None
    assert not project_scale_runner_module._generated_project_validation_is_repairable(checked)


def test_ultra_generated_bundle_preserves_verified_load_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(project_scale_runner_module, "_run_generated_project_command",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(project_scale_runner_module, "validate_ultra_portfolio_api",
                        lambda *args, **kwargs: ())
    measurements = _ultra_load_result()
    monkeypatch.setattr(project_scale_runner_module, "validate_ultra_portfolio_storage",
                        lambda *args, **kwargs: measurements)
    result = project_scale_runner_module._validate_generated_project_bundle(
        _project_bundle({"package.json": "{}"}), commands=(("npm", "test"),),
        timeout_seconds=10, requirements_case_id="ultra:direct",
    )
    assert result.passed, result.reasons
    assert result.scale_validation == measurements
    assert result.scale_validation is not measurements


@pytest.mark.parametrize("missing", ("result", "run", "manifest", "failed_result"))
def test_ultra_load_binding_drops_incomplete_validation(missing: str) -> None:
    measurements = _ultra_load_result()
    if missing == "failed_result":
        measurements.update(status="failed", reasons=["wrong data"])
    bound = project_scale_runner_module._bind_scale_validation(
        None if missing == "result" else measurements,
        "ultra:direct", None if missing == "run" else "run-ultra",
        None if missing == "manifest" else {"src/app.js": (1, "a" * 64)},
    )
    assert bound is None


def test_ultra_load_binding_is_checked_against_current_manifest_and_run() -> None:
    manifest = {"src/app.js": (1, "a" * 64)}
    bound = project_scale_runner_module._bind_scale_validation(
        _ultra_load_result(), "ultra:direct", "run-ultra", manifest,
    )
    result = ProjectScaleCaseResult(
        case_id="ultra:direct", run_id="run-ultra", status="completed", evidence={},
        validated_workspace_manifest=manifest, scale_validation=bound,
    )
    assert result.scale_specific_evidence_ok
    assert result.to_payload()["scale_validation"] == bound
    assert result.to_payload()["scale_validation"] is not bound
    manifest["src/app.js"] = (1, "b" * 64)
    assert not result.scale_specific_evidence_ok


def test_generated_project_command_env_uses_stable_default_npm_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    monkeypatch.delenv("AGENT_HUB_GENERATED_PROJECT_NPM_REGISTRY", raising=False)

    env = project_scale_runner_module._generated_project_command_env()

    assert env["NPM_CONFIG_REGISTRY"] == "https://registry.npmmirror.com"
    assert env["NPM_CONFIG_AUDIT"] == "false"
    assert env["NPM_CONFIG_FUND"] == "false"
    assert env["NPM_CONFIG_UPDATE_NOTIFIER"] == "false"


def test_generated_project_command_env_allows_npm_registry_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NPM_CONFIG_REGISTRY", raising=False)
    monkeypatch.setenv("AGENT_HUB_GENERATED_PROJECT_NPM_REGISTRY", "https://registry.npmjs.org/")

    env = project_scale_runner_module._generated_project_command_env()

    assert env["NPM_CONFIG_REGISTRY"] == "https://registry.npmjs.org/"


def test_incremental_workspace_merge_overwrites_valid_json_when_patch_is_invalid() -> None:
    base = _project_bundle(
        {
            "package.json": json.dumps(
                {"scripts": {"build": "tsc", "test": "vitest run"}}
            ),
            "src/main.ts": "export const value = 1;\n",
        }
    )
    patch = _project_bundle(
        {
            "package.json": "{'scripts': {'build': 'tsc', 'test': 'vitest run'}}\n",
            "preview.html": "<!doctype html><title>Preview</title>\n",
        }
    )

    merged = project_scale_runner_module._merged_workspace_bundle(base, patch)

    assert merged is not None
    with zipfile.ZipFile(BytesIO(merged)) as archive:
        package = archive.read("package.json").decode("utf-8")
        assert package == "{'scripts': {'build': 'tsc', 'test': 'vitest run'}}\n"
        assert archive.read("src/main.ts") == b"export const value = 1;\n"
        assert archive.read("preview.html").decode("utf-8").startswith("<!doctype html>")


@pytest.mark.parametrize(
    "failed_reason",
    (
        "failure at file:///tmp/agent-hub-project-scale-a1/tests/concurrency.test.mjs:70:14",
        r"failure at C:\Temp\agent-hub-project-scale-a1\tests\concurrency.test.mjs:70:14",
    ),
)
def test_repair_context_maps_absolute_failure_paths_to_workspace_files(
    failed_reason: str,
) -> None:
    selected = project_scale_runner_module._repair_context_relevant_paths(
        (
            "package.json",
            "src/service.ts",
            "tests/concurrency.test.mjs",
            "tsconfig.json",
        ),
        (failed_reason,),
    )

    assert selected[0] == "tests/concurrency.test.mjs"
    assert "package.json" in selected
    assert "tsconfig.json" in selected


def test_repair_context_snippet_focuses_on_reported_failure_line() -> None:
    test_lines = [f"const filler{index} = {index};" for index in range(1, 49)]
    test_lines.extend(
        (
            "const approval = await request('/approvals', {",
            "  body: JSON.stringify({ project_id: project.body.id, type: 'portfolio' }),",
            "});",
            "expect(approval.status).toBe(201);",
        )
    )
    bundle = _project_bundle(
        {
            "package.json": json.dumps({"scripts": {"test": "vitest run"}}),
            "tests/rbac.test.ts": "\n".join(test_lines),
        }
    )

    context = project_scale_runner_module._workspace_repair_context(
        bundle,
        failed_reasons=(
            (
                "AssertionError: expected 400 to be 201 at "
                "/tmp/project/tests/rbac.test.ts:52:27"
            ),
        ),
    )

    assert "project_id: project.body.id" in context
    assert "expect(approval.status).toBe(201)" in context


def test_repair_context_snippet_supports_parenthesized_typescript_location() -> None:
    lines = [f"const filler{index} = {index};" for index in range(1, 60)]
    lines[51] = "const exactFailure = await createApproval();"

    snippet = project_scale_runner_module._focused_repair_snippet(
        "tests/rbac.test.ts",
        "\n".join(lines),
        failed_reasons=(r"C:\project\tests\rbac.test.ts(52,27): error TS2322",),
    )

    assert "exactFailure" in snippet


def test_repair_context_snippet_uses_longest_matching_workspace_path() -> None:
    files = {
        "src/app.ts": b"const wrongFile = true;",
        "generated/src/app.ts": b"const rightFile = true;",
    }

    selected = project_scale_runner_module._repair_context_relevant_paths(
        tuple(files),
        ("failure at /tmp/project/generated/src/app.ts:1:1",),
        file_bytes=files,
    )

    assert selected[0] == "generated/src/app.ts"
    assert "src/app.ts" not in selected


def test_repair_context_snippet_ignores_out_of_range_failure_line() -> None:
    text = "const beginning = true;\n" + "\n".join(
        f"const filler{index} = {index};" for index in range(2, 40)
    )

    snippet = project_scale_runner_module._focused_repair_snippet(
        "src/app.ts",
        text,
        failed_reasons=("src/app.ts:999:1 failed",),
    )

    assert "beginning" in snippet
    assert "filler39" not in snippet


def test_repair_context_prioritizes_runtime_endpoint_and_module_matches() -> None:
    files = {
        "package.json": b'{"scripts":{"test":"node --test"}}',
        "tsconfig.json": b'{"compilerOptions":{"strict":true}}',
        "src/app.ts": b"if (path === '/inventory/stock') return inventory.addStock(body);",
        "src/modules/inventory.ts": b"export class InventoryService {}",
        "src/modules/orders.ts": b"export class OrderService {}",
        "tests/api.test.ts": b"test('inventory endpoint', () => {});",
    }

    selected = project_scale_runner_module._repair_context_relevant_paths(
        tuple(files),
        ("order operations workflow: POST /inventory/stock: missing id",),
        file_bytes=files,
    )

    assert selected[:2] == ["src/app.ts", "src/modules/inventory.ts"]
    assert "package.json" in selected
    assert "tsconfig.json" in selected


def test_repair_context_prioritizes_typescript_export_providers() -> None:
    paths = (
        "package.json",
        "tsconfig.json",
        "vitest.config.ts",
        "vite.config.ts",
        "src/rbac.ts",
        "src/readModel.ts",
        "src/storage.ts",
        "src/store.ts",
        "src/types.ts",
        "tests/helpers.ts",
        "tests/scenario.test.ts",
    )
    failures = (
        (
            "src/rbac.ts(1,10): error TS2305: Module './types' has no exported member "
            "'Action'. src/readModel.ts(1,15): error TS2305: Module './store' has no "
            "exported member 'PortfolioState'. src/storage.ts(3,15): error TS2305: "
            "Module './types' has no exported member 'PortfolioSnapshot'. "
            "tests/helpers.ts(4,10): error TS2305: Module '../src/store' has no exported "
            "member 'getStore'."
        ),
    )

    selected = project_scale_runner_module._repair_context_relevant_paths(paths, failures)

    assert "src/types.ts" in selected
    assert "src/store.ts" in selected
    assert selected.index("src/types.ts") < selected.index("src/rbac.ts")
    assert selected.index("src/store.ts") < selected.index("src/readModel.ts")


def test_repair_context_ts_export_hint_preserves_public_interfaces() -> None:
    hint = project_scale_runner_module._repair_context_failure_hints(
        ("src/rbac.ts(1,10): error TS2305: Module './types' has no exported member 'Action'",)
    )

    assert "preserve existing public interfaces" in hint


def test_repair_context_ts_export_provider_summarizes_late_exports() -> None:
    provider = "\n".join(
        (
            *(f"const internal{index} = {index};" for index in range(30)),
            "export type Role = 'admin' | 'viewer';",
            "export interface PortfolioState { projects: unknown[] }",
        )
    )

    snippet = project_scale_runner_module._focused_repair_snippet(
        "src/types.ts",
        provider,
        failed_reasons=(
            "src/rbac.ts(1,10): error TS2305: Module './types' has no exported member 'Action'",
        ),
        max_chars=500,
    )

    assert "export type Role" in snippet
    assert "export interface PortfolioState" in snippet


def test_repair_context_reserves_test_configuration_when_many_tests_fail() -> None:
    paths = (
        "package.json",
        "tsconfig.json",
        "vitest.config.ts",
        "vite.config.ts",
        *(f"src/module-{index}.ts" for index in range(6)),
        *(f"tests/case-{index}.test.ts" for index in range(10)),
    )
    failures = tuple(
        f"ReferenceError: describe is not defined at /tmp/project/{path}:4:1"
        for path in paths
        if path.startswith(("src/", "tests/"))
    )

    selected = project_scale_runner_module._repair_context_relevant_paths(paths, failures)

    assert "package.json" in selected
    assert "tsconfig.json" in selected
    assert "vitest.config.ts" in selected
    assert "vite.config.ts" in selected
    assert len([path for path in selected if path.startswith("tests/")]) <= 3


def test_repair_context_keeps_distinct_test_failures() -> None:
    paths = (
        "package.json",
        "tsconfig.json",
        "vitest.config.ts",
        "vite.config.ts",
        *(f"tests/case-{index}.test.ts" for index in range(4)),
    )
    failures = tuple(
        f"AssertionError: distinct failure {index} at /tmp/project/tests/case-{index}.test.ts:4:1"
        for index in range(4)
    )

    selected = project_scale_runner_module._repair_context_relevant_paths(paths, failures)

    assert {path for path in selected if path.startswith("tests/")} == {
        f"tests/case-{index}.test.ts" for index in range(4)
    }


def test_repair_context_keeps_distinct_test_failure_mixed_with_global_errors() -> None:
    paths = (
        "package.json",
        "tsconfig.json",
        "vitest.config.ts",
        "vite.config.ts",
        *(f"tests/global-{index}.test.ts" for index in range(4)),
        "tests/business-rule.test.ts",
    )
    failures = (
        *(
            f"ReferenceError: describe is not defined at /tmp/project/tests/global-{index}.test.ts:4:1"
            for index in range(4)
        ),
        "AssertionError: expected 409 at /tmp/project/tests/business-rule.test.ts:20:3",
    )

    selected = project_scale_runner_module._repair_context_relevant_paths(paths, failures)

    assert "tests/business-rule.test.ts" in selected
    assert len([path for path in selected if path.startswith("tests/global-")]) == 3


def test_repair_context_adds_vitest_global_api_hint() -> None:
    hint = project_scale_runner_module._repair_context_failure_hints(
        ("ReferenceError: describe is not defined",)
    )

    assert "Vitest" in hint
    assert "explicitly import" in hint
    assert "globals" in hint


def test_repair_context_adds_missing_build_tool_dependency_hint() -> None:
    hint = project_scale_runner_module._repair_context_failure_hints(
        ("npm run build output_tail=\"sh: 1: tsc: not found\"",)
    )

    assert "package.json" in hint
    assert "typescript" in hint
    assert "devDependencies" in hint


@pytest.mark.parametrize(
    "failure",
    (
        "sh: vitest: command not found",
        "'tsc' is not recognized as an internal or external command",
    ),
)
def test_repair_context_recognizes_supported_shell_missing_tool_formats(
    failure: str,
) -> None:
    hint = project_scale_runner_module._repair_context_failure_hints((failure,))

    assert "package.json" in hint


@pytest.mark.parametrize(
    "failure",
    (
        "GET /orders/abc: not found",
        "customer: not found",
        "sh: 1: python3: not found",
    ),
)
def test_repair_context_does_not_treat_business_or_system_errors_as_npm_tools(
    failure: str,
) -> None:
    hint = project_scale_runner_module._repair_context_failure_hints((failure,))

    assert "Missing npm-script executable" not in hint


def test_capability_repair_keeps_workspace_context_after_long_failure_output() -> None:
    bundle = _project_bundle(
        {
            "package.json": json.dumps({"scripts": {"test": "vitest run"}}),
            "tsconfig.json": json.dumps({"compilerOptions": {"strict": True}}),
            "vitest.config.ts": "export default { test: { globals: false } };\n",
            "src/app.ts": "export const app = true;\n",
            "tests/app.test.ts": "describe('app', () => {});\n",
            **{
                f"src/module-{index}.ts": "export const value = '" + ("x" * 2_000) + "';\n"
                for index in range(8)
            },
            "README.md": "# App\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": "- npm test: failed\n",
        }
    )
    long_failure = (
        "generated_project_validation: command failed exit=1 command=npm test output_tail=\""
        + "stack trace noise " * 180
        + "ReferenceError: describe is not defined at /tmp/project/tests/app.test.ts:1:1\""
    )

    repair = _deliverable_repair_body(
        {
            "message": "Build a tested TypeScript service.",
            "mode": "direct",
            "project_id": "project-1",
            "workspace_session_id": "session-1",
        },
        "large:direct",
        benchmark_kind="capability",
        source_workspace_bundle=bundle,
        failed_reasons=(long_failure,),
    )

    message = str(repair["message"])
    assert repair["replace_workspace_files"] is False
    assert "Return only complete changed files" in message
    assert "Unchanged workspace files remain authoritative" in message
    assert "Current workspace context for precise repair" in message
    assert "vitest.config.ts" in message
    assert "tests/app.test.ts" in message
    assert "Vitest test-runtime repair hint" in message
    assert len(message) <= 6_000


def test_capability_repair_preserves_failure_and_request_with_many_long_snippets() -> None:
    files = {
        f"src/module-{index}.ts": (
            "\n".join(f"const line{line} = '{index}-{'x' * 80}';" for line in range(1, 80))
        )
        for index in range(8)
    }
    bundle = _project_bundle(files)
    failures = tuple(
        f"UNIQUE_FAILURE_{index} at /tmp/project/src/module-{index}.ts:52:3"
        for index in range(8)
    )

    repair = _deliverable_repair_body(
        {
            "message": "ORIGINAL_REQUIREMENT build a non-web event processor",
            "mode": "direct",
            "project_id": "project-1",
            "workspace_session_id": "session-1",
        },
        "ultra:direct",
        benchmark_kind="capability",
        source_workspace_bundle=bundle,
        failed_reasons=failures,
    )

    message = str(repair["message"])
    assert "Previous failed evidence" in message
    assert "UNIQUE_FAILURE_0" in message
    assert "Original request" in message
    assert "ORIGINAL_REQUIREMENT" in message
    assert "Current workspace context for precise repair" in message
    contract, evidence_and_context = message.split("Previous failed evidence", 1)
    assert "Dependency lifecycle is exact" in contract
    assert "GET /portfolio/read-model" in contract
    assert len(evidence_and_context) <= 1_100 + 800 + 1_500 + 22


def test_authoritative_capability_repair_requires_preview_entrypoint() -> None:
    repair = _deliverable_repair_body(
        {
            "message": "Build a complete interactive website.",
            "mode": "direct",
            "project_id": "project-1",
            "workspace_session_id": "session-1",
        },
        "ultra:direct",
        benchmark_kind="capability",
        source_workspace_bundle=_project_bundle(
            {"package.json": json.dumps({"scripts": {"build": "tsc"}})}
        ),
        force_workspace_replacement=True,
        failed_reasons=(
            "requirements: requested web preview entrypoint missing; add preview.html or index.html",
        ),
    )

    message = str(repair["message"])
    assert repair["replace_workspace_files"] is True
    assert "Every authoritative replacement must include preview.html or index.html" in message


def test_authoritative_non_web_repair_does_not_infer_preview_from_failure_text() -> None:
    repair = _deliverable_repair_body(
        {
            "message": "Build a command-line event processor.",
            "mode": "direct",
            "project_id": "project-1",
            "workspace_session_id": "session-1",
        },
        "ultra:direct",
        benchmark_kind="capability",
        source_workspace_bundle=_project_bundle(
            {"package.json": json.dumps({"scripts": {"build": "tsc"}})}
        ),
        force_workspace_replacement=True,
        failed_reasons=("tests/preview.html.test.ts:52:3 failed",),
    )

    assert "Every authoritative replacement must include preview.html" not in str(
        repair["message"]
    )


def test_generated_project_output_excerpt_preserves_middle_test_failure() -> None:
    output = "\n".join(
        (
            "> project@test\n> node --test",
            "ok 1 - startup works",
            "setup noise " * 120,
            "not ok 2 - duplicate request returns conflict",
            "  failureType: 'testCodeFailure'",
            "  error: 'Expected values to be strictly equal: 200 !== 409'",
            "  actual: 200",
            "  expected: 409",
            "  operator: 'strictEqual'",
            "passing test output " * 180,
            "not ok 3 - cancelled job cannot be completed",
            "  failureType: 'testCodeFailure'",
            "  error: 'Expected values to be strictly equal: 200 !== 409'",
            "  actual: 200",
            "  expected: 409",
            "  operator: 'strictEqual'",
            "# tests 13",
            "# pass 11",
            "# fail 2",
        )
    )

    encoded = project_scale_runner_module._generated_project_output_tail(output)
    excerpt = json.loads(encoded)

    assert len(excerpt) <= project_scale_runner_module._GENERATED_PROJECT_OUTPUT_TAIL_CHARS
    assert "not ok 2 - duplicate request returns conflict" in excerpt
    assert "not ok 3 - cancelled job cannot be completed" in excerpt
    assert "actual: 200" in excerpt
    assert "expected: 409" in excerpt
    assert "# fail 2" in excerpt


def test_generated_project_output_excerpt_redacts_secrets_and_bounds_json() -> None:
    output = "\n".join(
        (
            "Authorization: Bearer secret-bearer-value:still-secret",
            "2026-09-29 INFO Authorization: Basic prefixed-basic-secret",
            "password: alpha beta gamma",
            "access_token=raw-token client_secret=client-secret-value",
            'payload={\\"access_token\\":\\"escaped-secret\\"}',
            "AWS_SECRET_ACCESS_KEY=aws-secret-value",
            "bare key sk-project-secret must not escape",
            "Cookie: session=private-cookie",
            "DEBUG Cookie: sid=prefixed-cookie-secret",
            "Set-Cookie: sid=one; refresh=two",
            "curl --user admin:curl-password https://example.test",
            "request=https://user:pass@example.test/run?token=query-secret",
            "database=postgres://dbuser:dbpass@example.test/app",
            "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
            "not ok 1 - token validation reports a useful error",
            "  error: " + ('path C:\\\\workspace\\\\' + '\"quoted\" ') * 250,
            "# fail 1",
        )
    )

    encoded = project_scale_runner_module._generated_project_output_tail(output)
    excerpt = json.loads(encoded)

    assert len(encoded) <= project_scale_runner_module._GENERATED_PROJECT_OUTPUT_TAIL_CHARS
    assert "secret-bearer-value" not in excerpt
    assert "prefixed-basic-secret" not in excerpt
    assert "alpha beta gamma" not in excerpt
    assert "raw-token" not in excerpt
    assert "client-secret-value" not in excerpt
    assert "escaped-secret" not in excerpt
    assert "aws-secret-value" not in excerpt
    assert "sk-project-secret" not in excerpt
    assert "private-cookie" not in excerpt
    assert "prefixed-cookie-secret" not in excerpt
    assert "refresh=two" not in excerpt
    assert "curl-password" not in excerpt
    assert "user:pass" not in excerpt
    assert "dbuser:dbpass" not in excerpt
    assert "query-secret" not in excerpt
    assert "eyJhbGciOiJIUzI1NiJ9" not in excerpt
    assert "<redacted>" in excerpt
    assert "token validation reports a useful error" in excerpt


def test_generated_project_output_excerpt_keeps_failure_after_json_expansion() -> None:
    output = (
        "command output "
        + ("C:\\\\quoted\\\\path " * 90)
        + "\nnot ok 4 - final contract\n"
        + "  error: expected conflict response\n"
        + "  actual: 200\n"
        + "  expected: 409\n"
        + "# fail 1"
    )
    assert len(output) < project_scale_runner_module._GENERATED_PROJECT_OUTPUT_TAIL_CHARS

    encoded = project_scale_runner_module._generated_project_output_tail(output)
    excerpt = json.loads(encoded)

    assert len(encoded) <= project_scale_runner_module._GENERATED_PROJECT_OUTPUT_TAIL_CHARS
    assert "not ok 4 - final contract" in excerpt
    assert "expected: 409" in excerpt
    assert "# fail 1" in excerpt


def test_generated_project_output_excerpt_reports_omitted_failure_blocks() -> None:
    failures = "\n".join(
        f"not ok {index} - contract {index}\n  error: failure {index}\n  expected: {index + 1}"
        for index in range(1, 6)
    )
    output = "\n".join((*("passing noise " * 20 for _ in range(30)), failures, "# fail 5"))

    excerpt = json.loads(project_scale_runner_module._generated_project_output_tail(output))

    assert "not ok 1 - contract 1" in excerpt
    assert "not ok 2 - contract 2" in excerpt
    assert "not ok 3 - contract 3" in excerpt
    assert "2 additional failure blocks omitted" in excerpt
    assert "# fail 5" in excerpt


def test_generated_project_output_excerpt_deduplicates_failures_before_budgeting() -> None:
    duplicate = (
        "not ok {index} - duplicate request conflict\n"
        "  error: expected conflict\n"
        "  actual: 200\n"
        "  expected: 409"
    )
    independent = (
        "not ok 4 - cancelled fulfillment transition\n"
        "  error: cancelled job completed\n"
        "  actual: 200\n"
        "  expected: 409"
    )
    output = "\n".join(
        (
            *(duplicate.format(index=index) for index in range(1, 4)),
            independent,
            *("passing noise " * 20 for _ in range(30)),
            "# fail 4",
        )
    )

    excerpt = json.loads(project_scale_runner_module._generated_project_output_tail(output))

    assert "duplicate request conflict" in excerpt
    assert "cancelled fulfillment transition" in excerpt
    assert "cancelled job completed" in excerpt
    assert "# fail 4" in excerpt


def test_generated_project_npm_commands_fail_closed_without_isolated_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX",
        raising=False,
    )
    invoked = False

    def unexpected_run(*args: object, **kwargs: object) -> object:
        del args, kwargs
        nonlocal invoked
        invoked = True
        raise AssertionError("untrusted npm command reached the host subprocess runner")

    monkeypatch.setattr(subprocess, "run", unexpected_run)

    reason = project_scale_runner_module._run_generated_project_command(
        ("npm", "test"),
        cwd=tmp_path,
        timeout_seconds=30,
    )

    assert invoked is False
    assert reason is not None and "bwrap sandbox required" in reason


def test_generated_python_commands_cannot_bypass_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoked = False

    def record_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        nonlocal invoked
        invoked = True
        return subprocess.CompletedProcess(("python", "-c", "pass"), 0, "", "")

    monkeypatch.setattr(
        project_scale_runner_module,
        "_generated_project_validation_is_isolated",
        lambda: True,
    )
    monkeypatch.setattr(subprocess, "run", record_run)

    reason = project_scale_runner_module._run_generated_project_command(
        (sys.executable, "-c", "pass"),
        cwd=tmp_path,
        timeout_seconds=30,
    )

    assert invoked is False
    assert reason is not None and "unsupported generated executable" in reason


def test_isolation_marker_never_authorizes_host_node_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.harness import project_validation_sandbox

    monkeypatch.setenv("AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX", "systemd")
    monkeypatch.setattr(project_validation_sandbox, "_BWRAP", tmp_path / "missing-bwrap")
    monkeypatch.setattr(
        project_scale_runner_module, "_generated_project_validation_is_isolated", lambda: True,
    )
    invocations: list[object] = []

    def record_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        invocations.append(args[0])
        return subprocess.CompletedProcess(args[0], 0, "", "")  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "run", record_run)
    reason = project_scale_runner_module._run_generated_project_command(
        ("/usr/bin/node", "-e", "process.exit(0)"), cwd=tmp_path, timeout_seconds=2,
    )
    assert reason is not None
    assert invocations == [], "an env/cgroup marker cannot authorize host Node"


def test_requirements_fail_closed_before_host_npm_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_hub.harness import project_requirements

    monkeypatch.delenv("AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX", raising=False)
    monkeypatch.setattr(project_requirements, "_PLATFORM", "posix")
    monkeypatch.setattr(shutil, "which", lambda value: "/usr/bin/" + value)
    (tmp_path / "package.json").write_text('{"scripts":{"start":"node server.js"}}')
    invocations: list[object] = []

    def unexpected_start(*args: object, **kwargs: object) -> object:
        invocations.append(args[0])
        raise OSError("host npm start reached")

    monkeypatch.setattr(subprocess, "Popen", unexpected_start)
    failures = project_requirements.validate_small_task_api(tmp_path, timeout_seconds=2)
    assert invocations == [], "business validation must not launch generated npm on host"
    assert failures and "sandbox" in failures[0]


@pytest.mark.parametrize("reason", (
    "generated_project_validation: command failed exit=1 command=npm test",
    "requirements: timeout: bwrap sandbox requirements validation deadline exceeded",
    (
        'generated_project_validation: command failed exit=1 command=npm install '
        'output_tail="generated dependency source rejected: unsupported dependency source git"'
    ),
))
def test_sandbox_project_failure_and_timeout_remain_repairable(reason: str) -> None:
    assert project_scale_runner_module._generated_project_validation_is_repairable(
        project_scale_runner_module._EvidenceCheck(passed=False, reasons=(reason,))
    )


@pytest.mark.parametrize(
    "reason",
    (
        (
            "generated_project_validation: isolated systemd validator is required "
            "for generated npm/node commands"
        ),
        "requirements: npm executable unavailable; large order API was not validated",
        "requirements: unsupported platform for process-tree cleanup: windows",
        "requirements: process-tree cleanup failed: permission denied",
        "requirements: temporary DATA_DIR cleanup failed: permission denied",
        "requirements: ultra load failed: temporary runtime cleanup failed: permission denied",
        "requirements: ultra load unavailable: bwrap sandbox validator failed: exit=1",
    ),
)
def test_validator_infrastructure_failure_is_not_treated_as_project_repair(reason: str) -> None:
    result = project_scale_runner_module._EvidenceCheck(
        passed=False,
        reasons=(reason,),
    )

    assert not project_scale_runner_module._generated_project_validation_is_repairable(result)


def test_remaining_wait_seconds_uses_case_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_hub.harness.project_scale_runner.time.monotonic", lambda: 120.0)

    assert project_scale_runner_module._remaining_wait_seconds(150.0, 60.0) == 30.0
    assert project_scale_runner_module._remaining_wait_seconds(110.0, 60.0) == 0
    assert project_scale_runner_module._remaining_wait_seconds(150.0, 0) == 0


def test_effective_execute_wait_seconds_reserves_capability_repair_budget() -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=("small",),
        flows=("hybrid",),
        execute=True,
    )

    assert (
        project_scale_runner_module._effective_execute_wait_seconds(
            plan,
            120,
            generated_project_timeout_seconds=240,
        )
        == 2760
    )
    assert (
        project_scale_runner_module._effective_execute_wait_seconds(
            plan,
            5000,
            generated_project_timeout_seconds=240,
        )
        == 5000
    )

    assert (
        project_scale_runner_module._effective_execute_wait_seconds(
            plan,
            120,
            generated_project_timeout_seconds=240,
            generated_project_command_count=5,
        )
        == 3240
    )


def test_repair_deadline_never_extends_past_case_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent_hub.harness.project_scale_runner.time.monotonic", lambda: 50.0)

    assert (
        project_scale_runner_module._extend_repair_deadline(
            100.0,
            configured_wait_seconds=120.0,
            request_body={"runtime_timeout_seconds": 900},
            benchmark_kind="capability",
            generated_project_timeout_seconds=240.0,
            generated_project_command_count=3,
            absolute_deadline=500.0,
        )
        == 500.0
    )


def test_case_absolute_deadline_scales_from_case_id() -> None:
    assert project_scale_runner_module._case_absolute_deadline(
        50.0,
        configured_wait_seconds=100.0,
        case_id="small:direct",
        benchmark_kind="capability",
    ) == 200.0
    assert project_scale_runner_module._case_absolute_deadline(
        50.0,
        configured_wait_seconds=100.0,
        case_id="ultra:auto",
        benchmark_kind="capability",
    ) == 275.0


def test_generated_project_validation_stops_at_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_calls: list[float] = []
    monkeypatch.setattr(
        project_scale_runner_module,
        "_run_generated_project_command",
        lambda *args, **kwargs: command_calls.append(float(kwargs["timeout_seconds"])),
    )
    monkeypatch.setattr(
        "agent_hub.harness.project_scale_runner.time.monotonic",
        lambda: 50.0,
    )

    result = project_scale_runner_module._validate_generated_project_bundle(
        _project_bundle({"package.json": "{}"}),
        commands=((sys.executable, "-c", "pass"),),
        timeout_seconds=120,
        absolute_deadline=50.0,
    )

    assert result.passed is False
    assert result.reasons == (
        "generated_project_validation: absolute validation deadline exhausted",
    )
    assert command_calls == []


def test_generated_project_validation_clamps_command_to_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_timeouts: list[float] = []

    def run_command(*args: object, **kwargs: object) -> None:
        command_timeout = kwargs["timeout_seconds"]
        assert isinstance(command_timeout, int | float)
        command_timeouts.append(float(command_timeout))

    monkeypatch.setattr(
        project_scale_runner_module,
        "_run_generated_project_command",
        run_command,
    )
    monkeypatch.setattr(
        "agent_hub.harness.project_scale_runner.time.monotonic",
        lambda: 50.0,
    )

    result = project_scale_runner_module._validate_generated_project_bundle(
        _project_bundle({"package.json": "{}"}),
        commands=((sys.executable, "-c", "pass"),),
        timeout_seconds=120,
        absolute_deadline=55.0,
    )

    assert result.passed is True
    assert command_timeouts == [5.0]


def test_deliverable_repair_attempt_limit_rejects_unknown_scale() -> None:
    with pytest.raises(ValueError, match="unknown project scale"):
        project_scale_runner_module._deliverable_repair_attempt_limit(
            "unexpected:direct",
            benchmark_kind="capability",
        )


def test_deliverable_repair_progress_ignores_volatile_failure_and_bundle_changes() -> None:
    evidence = {
        "workspace_bundle": True,
        "deliverable_quality": False,
        "agent_standard_verification": True,
        "discussion_trace": True,
        "plugin_contract": True,
        "multi_agent_participation": True,
        "generated_project_validation": False,
        "self_repair_trace": True,
    }
    previous = project_scale_runner_module._deliverable_repair_progress_state(
        evidence,
        case_id="small:direct",
        generated_project_validation=project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=(
                (
                    'generated_project_validation: command failed exit=1 command=npm test '
                    'output_tail="failed at 2026-09-28T10:11:12.123Z in '
                    '/tmp/agent-hub-project-scale-abcd/tests/api.test.ts(41,16)"'
                ),
            ),
        ),
    )
    current = project_scale_runner_module._deliverable_repair_progress_state(
        evidence,
        case_id="small:direct",
        generated_project_validation=project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=(
                (
                    'generated_project_validation: command failed exit=1 command=npm test '
                    'output_tail="failed at 2026-09-28T10:12:13.456Z in '
                    '/tmp/agent-hub-project-scale-wxyz/tests/api.test.ts(99,2)"'
                ),
            ),
        ),
    )

    assert current.signature == previous.signature
    assert not project_scale_runner_module._deliverable_repair_made_progress(
        previous,
        current,
        seen_signatures={previous.signature},
    )


def test_deliverable_repair_progress_rejects_unseen_error_rotation() -> None:
    evidence = {
        "workspace_bundle": True,
        "deliverable_quality": False,
        "agent_standard_verification": True,
        "generated_project_validation": False,
    }
    state_a = project_scale_runner_module._deliverable_repair_progress_state(
        evidence,
        case_id="medium:direct",
        generated_project_validation=project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=("generated_project_validation: error TS2322 incompatible type",),
        ),
    )
    state_b = project_scale_runner_module._deliverable_repair_progress_state(
        evidence,
        case_id="medium:direct",
        generated_project_validation=project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=("generated_project_validation: error TS2554 missing argument",),
        ),
    )

    assert not project_scale_runner_module._deliverable_repair_made_progress(
        state_a,
        state_b,
        seen_signatures={state_a.signature},
    )
    assert not project_scale_runner_module._deliverable_repair_made_progress(
        state_b,
        state_a,
        seen_signatures={state_a.signature, state_b.signature},
    )


def test_deliverable_repair_progress_rejects_same_stage_error_rotation() -> None:
    previous = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("npm run build failed with ts2322",),
        validation_stage=1,
    )
    current = project_scale_runner_module._DeliverableRepairProgress(
        deficits=previous.deficits,
        failure_fingerprints=("npm run build failed with ts2305",),
        validation_stage=1,
    )

    assert not project_scale_runner_module._deliverable_repair_made_progress(
        previous,
        current,
        seen_signatures={previous.signature},
    )


def test_deliverable_repair_progress_includes_non_validation_evidence() -> None:
    before = project_scale_runner_module._deliverable_repair_progress_state(
        {
            "workspace_bundle": True,
            "deliverable_quality": True,
            "agent_standard_verification": False,
            "generated_project_validation": True,
        },
        case_id="small:direct",
        generated_project_validation=project_scale_runner_module._EvidenceCheck(
            passed=True,
            reasons=(),
        ),
    )
    after = project_scale_runner_module._deliverable_repair_progress_state(
        {
            "workspace_bundle": True,
            "deliverable_quality": True,
            "agent_standard_verification": True,
            "generated_project_validation": True,
        },
        case_id="small:direct",
        generated_project_validation=project_scale_runner_module._EvidenceCheck(
            passed=True,
            reasons=(),
        ),
    )

    assert "agent_standard_verification" in before.deficits
    assert "agent_standard_verification" not in after.deficits
    assert project_scale_runner_module._deliverable_repair_made_progress(
        before,
        after,
        seen_signatures={before.signature},
    )


def test_deliverable_repair_progress_rejects_evidence_regression() -> None:
    before = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("build failed",),
    )
    after = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("agent_standard_verification", "generated_project_validation"),
        failure_fingerprints=("test failed",),
    )

    assert not project_scale_runner_module._deliverable_repair_made_progress(
        before,
        after,
        seen_signatures={before.signature},
    )


def test_deliverable_repair_progress_rejects_validation_stage_regression() -> None:
    previous = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("npm test failed",),
        validation_stage=2,
    )
    current = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("npm install failed",),
        validation_stage=0,
    )

    assert not project_scale_runner_module._deliverable_repair_made_progress(
        previous,
        current,
        seen_signatures={previous.signature},
    )


def test_deliverable_repair_progress_accepts_new_failure_after_validation_stage_advance() -> None:
    previous = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("npm test failed",),
        validation_stage=2,
    )
    current = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=(
            "npm test failed",
            "post /inventory/stock: missing id",
        ),
        validation_stage=3,
    )

    assert project_scale_runner_module._deliverable_repair_made_progress(
        previous,
        current,
        seen_signatures={previous.signature},
    )


def test_soft_repair_limit_allows_unseen_actionable_regression() -> None:
    previous = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("npm test failed",),
        validation_stage=2,
    )
    current = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("npm run build failed with ts2305",),
        validation_stage=1,
    )

    assert project_scale_runner_module._deliverable_repair_followup_warranted(
        previous,
        current,
        seen_signatures={previous.signature},
    )
    assert not project_scale_runner_module._deliverable_repair_followup_warranted(
        current,
        previous,
        seen_signatures={previous.signature, current.signature},
    )
    deeper_regression = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("npm install failed",),
        validation_stage=0,
    )
    assert not project_scale_runner_module._deliverable_repair_followup_warranted(
        previous,
        deeper_regression,
        seen_signatures={previous.signature},
    )


def test_soft_repair_limit_rejects_unseen_same_stage_build_failure() -> None:
    previous = project_scale_runner_module._DeliverableRepairProgress(
        deficits=(
            "deliverable_quality",
            "generated_project_validation",
            "requirements_validation",
        ),
        failure_fingerprints=(
            "npm run build failed in tests/helpers.ts with missing jsonstore export",
        ),
        validation_stage=1,
    )
    current = project_scale_runner_module._DeliverableRepairProgress(
        deficits=previous.deficits,
        failure_fingerprints=(
            "npm run build failed in src/services/payment.ts with missing newid export",
        ),
        validation_stage=1,
    )

    assert not project_scale_runner_module._deliverable_repair_followup_warranted(
        previous,
        current,
        seen_signatures={previous.signature},
    )
    assert not project_scale_runner_module._deliverable_repair_followup_warranted(
        previous,
        current,
        seen_signatures={previous.signature, current.signature},
    )


def test_same_stage_replacement_failure_gets_one_bounded_followup() -> None:
    previous = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("src/one.ts: error ts2322",),
        validation_stage=1,
    )
    current = project_scale_runner_module._DeliverableRepairProgress(
        deficits=previous.deficits,
        failure_fingerprints=("src/two.ts: error ts2305",),
        validation_stage=1,
    )

    assert project_scale_runner_module._deliverable_repair_exposes_new_same_stage_failure(
        previous,
        current,
        seen_signatures={previous.signature},
    )
    assert not project_scale_runner_module._deliverable_repair_exposes_new_same_stage_failure(
        previous,
        current,
        seen_signatures={previous.signature, current.signature},
    )


def _typescript_build_validation(output: str) -> project_scale_runner_module._EvidenceCheck:
    output_tail = project_scale_runner_module._generated_project_output_tail(output)
    reason = (
        f"generated_project_validation: command failed exit=2 command=npm run build "
        f"output_tail={output_tail}"
    )
    return project_scale_runner_module._EvidenceCheck(
        passed=False,
        reasons=(reason,),
    )


class _ControlledMonotonicClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _typescript_build_progress_state(output: str) -> project_scale_runner_module._DeliverableRepairProgress:
    validation = _typescript_build_validation(output)
    return project_scale_runner_module._deliverable_repair_progress_state(
        {
            "workspace_bundle": True,
            "deliverable_quality": False,
            "agent_standard_verification": True,
            "generated_project_validation": False,
            "requirements_validation": False,
        },
        case_id="medium:direct",
        generated_project_validation=validation,
        failure_reasons=validation.reasons,
    )


def test_typescript_build_progress_accepts_overlapping_real_tsc_error_sets() -> None:
    previous = _typescript_build_progress_state(
        """src/store.ts(11,7): error TS2322: incompatible store type
src/handlers.ts:22:5 - error TS2345: incompatible handler argument
Found 2 errors in 2 files.
npm error Lifecycle script `build` failed with error"""
    )
    current = _typescript_build_progress_state(
        """src/handlers.ts:22:5 - error TS2345: incompatible handler argument
error TS18003: No inputs were found in config file 'tsconfig.json'
Found 2 errors in 1 file.
npm error Lifecycle script `build` failed with error"""
    )

    previous_failures = project_scale_runner_module._typescript_failure_fingerprints(previous)
    current_failures = project_scale_runner_module._typescript_failure_fingerprints(current)

    assert project_scale_runner_module._deliverable_repair_exposes_new_typescript_build_failure(
        previous,
        current,
        seen_signatures={previous.signature},
    )
    assert any("src/handlers.ts" in failure for failure in previous_failures)
    assert any("ts18003" in failure for failure in current_failures)
    assert all("found 2 errors" not in failure for failure in previous_failures)
    assert all("npm error" not in failure for failure in current_failures)


def test_typescript_build_progress_rejects_real_tsc_error_accumulation() -> None:
    previous = _typescript_build_progress_state(
        """src/store.ts(11,7): error TS2322: incompatible store type
Found 1 error in src/store.ts:11"""
    )
    current = _typescript_build_progress_state(
        """src/store.ts(11,7): error TS2322: incompatible store type
src/router.ts:47:3 - error TS2769: no overload matches this call
Found 2 errors in 2 files.
Build failed in 1.2s"""
    )

    assert not (
        project_scale_runner_module._deliverable_repair_exposes_new_typescript_build_failure(
            previous,
            current,
            seen_signatures={previous.signature},
        )
    )


def test_soft_repair_limit_allows_one_coupled_validation_regression() -> None:
    previous = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("deliverable_quality",),
        failure_fingerprints=("workspace_bundle: missing constraints reading evidence",),
        validation_stage=4,
    )
    current = project_scale_runner_module._DeliverableRepairProgress(
        deficits=(
            "deliverable_quality",
            "generated_project_validation",
            "requirements_validation",
        ),
        failure_fingerprints=("npm run build failed with ts2322",),
        validation_stage=1,
    )

    assert project_scale_runner_module._deliverable_repair_followup_warranted(
        previous,
        current,
        seen_signatures={previous.signature},
    )

    requirements_regression = project_scale_runner_module._DeliverableRepairProgress(
        deficits=current.deficits,
        failure_fingerprints=(
            "requirements: crm workflow: startup: npm start exited before crm api became ready",
        ),
        validation_stage=3,
    )
    assert project_scale_runner_module._deliverable_repair_followup_warranted(
        previous,
        requirements_regression,
        seen_signatures={previous.signature},
    )

    unrelated_regression = project_scale_runner_module._DeliverableRepairProgress(
        deficits=(
            "agent_standard_verification",
            "deliverable_quality",
            "generated_project_validation",
            "requirements_validation",
        ),
        failure_fingerprints=("npm run build failed with ts2322",),
        validation_stage=1,
    )
    assert not project_scale_runner_module._deliverable_repair_followup_warranted(
        previous,
        unrelated_regression,
        seen_signatures={previous.signature},
    )

    infrastructure_regression = project_scale_runner_module._DeliverableRepairProgress(
        deficits=current.deficits,
        failure_fingerprints=("npm executable unavailable",),
        validation_stage=0,
    )
    assert not project_scale_runner_module._deliverable_repair_followup_warranted(
        previous,
        infrastructure_regression,
        seen_signatures={previous.signature},
    )

    one_stage_added_deficit = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("deliverable_quality", "generated_project_validation"),
        failure_fingerprints=("npm test failed",),
        validation_stage=3,
    )
    assert not project_scale_runner_module._deliverable_repair_followup_warranted(
        previous,
        one_stage_added_deficit,
        seen_signatures={previous.signature},
    )


def test_validation_stage_uses_failed_command_not_output_tail_text() -> None:
    validation = project_scale_runner_module._EvidenceCheck(
        passed=False,
        reasons=(
            (
                "generated_project_validation: command=npm test "
                'output_tail="setup message says run npm install first"'
            ),
        ),
    )

    assert project_scale_runner_module._generated_project_validation_stage(validation) == 2


def test_deliverable_repair_progress_accepts_quantified_improvement() -> None:
    evidence = {
        "workspace_bundle": True,
        "deliverable_quality": False,
        "agent_standard_verification": True,
        "generated_project_validation": False,
    }
    previous = project_scale_runner_module._deliverable_repair_progress_state(
        evidence,
        case_id="small:direct",
        generated_project_validation=project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=('command=npm test output_tail="concurrent creates: 1 !== 25"',),
        ),
    )
    current = project_scale_runner_module._deliverable_repair_progress_state(
        evidence,
        case_id="small:direct",
        generated_project_validation=project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=('command=npm test output_tail="concurrent creates: 24 !== 25"',),
        ),
    )

    assert project_scale_runner_module._deliverable_repair_made_progress(
        previous,
        current,
        seen_signatures={previous.signature},
    )


def test_deliverable_repair_progress_accepts_partial_quality_reason_reduction() -> None:
    evidence = {
        "workspace_bundle": True,
        "deliverable_quality": False,
        "agent_standard_verification": True,
        "generated_project_validation": True,
    }
    validation = project_scale_runner_module._EvidenceCheck(passed=True, reasons=())
    previous = project_scale_runner_module._deliverable_repair_progress_state(
        evidence,
        case_id="small:direct",
        generated_project_validation=validation,
        failure_reasons=("missing plan", "missing verification"),
    )
    current = project_scale_runner_module._deliverable_repair_progress_state(
        evidence,
        case_id="small:direct",
        generated_project_validation=validation,
        failure_reasons=("missing verification",),
    )

    assert project_scale_runner_module._deliverable_repair_made_progress(
        previous,
        current,
        seen_signatures={previous.signature},
    )


def test_deliverable_repair_progress_rejects_added_failure_reason() -> None:
    previous = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("deliverable_quality",),
        failure_fingerprints=("missing verification",),
        validation_stage=4,
    )
    current = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("deliverable_quality",),
        failure_fingerprints=("missing plan", "missing verification"),
        validation_stage=4,
    )

    assert not project_scale_runner_module._deliverable_repair_made_progress(
        previous,
        current,
        seen_signatures={previous.signature},
    )


def test_deliverable_repair_progress_does_not_mix_quantified_metrics() -> None:
    previous = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("quantified failures",),
        validation_stage=2,
        progress_metrics=(("create", 25, 1), ("update", 25, 10)),
    )
    current = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("quantified failures",),
        validation_stage=2,
        progress_metrics=(("create", 25, 5), ("update", 25, 0)),
    )

    assert not project_scale_runner_module._deliverable_repair_made_progress(
        previous,
        current,
        seen_signatures={previous.signature},
    )


def test_deliverable_repair_progress_rejects_cross_dimension_regression() -> None:
    previous = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("deliverable_quality", "generated_project_validation"),
        failure_fingerprints=("quality and concurrency",),
        validation_stage=2,
        progress_metrics=(("create", 25, 1),),
    )
    current = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("concurrency",),
        validation_stage=2,
        progress_metrics=(("create", 25, 20),),
    )

    assert not project_scale_runner_module._deliverable_repair_made_progress(
        previous,
        current,
        seen_signatures={previous.signature},
    )


def test_followup_repair_includes_agent_standard_verification_deficit() -> None:
    assert project_scale_runner_module._has_followup_deliverable_repair_reason(
        {
            "workspace_bundle": True,
            "deliverable_quality": True,
            "agent_standard_verification": False,
        },
        case_id="small:direct",
    )


def run_project_scale_runner(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_hub.harness.project_scale_runner", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_project_scale_runner_prints_dry_run_plan_json() -> None:
    result = run_project_scale_runner(
        "--scale", "ultra", "--flow", "self_repair", "--json", "--benchmark-kind", "fixture"
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["execute"] is False
    assert payload["case_count"] == 1
    assert payload["requests"][0]["case_id"] == "ultra:self_repair"
    assert payload["requests"][0]["body"]["workspace_session_id"] == (
        "project-scale-ultra-self_repair"
    )
    assert "run_events" in payload["required_evidence"]
    assert "agent_standard_verification" in payload["required_evidence"]
    assert "discussion_trace" in payload["required_evidence"]
    assert "plugin_contract" in payload["required_evidence"]
    assert "delete_workspace" in payload["cleanup_actions"]


def test_project_scale_runner_defaults_to_full_matrix_plan_json() -> None:
    result = run_project_scale_runner("--json", "--benchmark-kind", "fixture")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    expected_case_ids = [
        f"{scale}:{flow}" for scale in PROJECT_SCALE_TIERS for flow in PROJECT_SCALE_FLOW_KINDS
    ]
    actual_case_ids = [request["case_id"] for request in payload["requests"]]

    assert payload["dry_run"] is True
    assert payload["execute"] is False
    assert payload["case_count"] == len(expected_case_ids)
    assert actual_case_ids == expected_case_ids
    assert "small:direct" in actual_case_ids
    assert "medium:hybrid" in actual_case_ids
    assert "large:plugin" in actual_case_ids
    assert "ultra:capability_validation" in actual_case_ids


def test_fixture_execution_report_does_not_claim_real_capability() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    report = execute_project_scale_plan(plan, FakeAcceptanceClient())

    payload = report.to_payload()
    assert payload["benchmark_kind"] == "fixture"
    assert payload["capability_verified"] is False
    assert "synthetic" in str(payload["verification_scope"])


def test_capability_plan_cli_does_not_trigger_preseed_runtime() -> None:
    result = run_project_scale_runner(
        "--benchmark-kind", "capability", "--scale", "small", "--flow", "direct", "--json"
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["benchmark_kind"] == "capability"
    assert payload["capability_verified"] is False
    message = payload["requests"][0]["body"]["message"]
    lowered = message.lower()
    assert "project-scale acceptance fixture" not in lowered
    assert "task management API" in message
    assert "constraints_reading_evidence.json" in message
    assert "AGENTS.md workspace rules" in message
    assert "HANDOFF current-state index" in message
    assert "PROJECT_REQUIREMENTS.md" in message
    assert "applicable SKILL.md" in message


def test_capability_benchmark_can_be_selected_by_acceptance_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_HUB_PROJECT_SCALE_BENCHMARK_KIND", "capability")
    result = run_project_scale_runner("--scale", "small", "--flow", "direct", "--json")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["benchmark_kind"] == "capability"


def test_medium_capability_message_declares_list_response_envelope() -> None:
    plan = build_project_scale_run_plan(
        scales=("medium",), flows=("direct",), benchmark_kind="capability"
    )
    message = str(plan.requests[0].body["message"])

    assert "All GET list endpoints must return 200 with {items:[...]}" in message
    assert "GET /tenants/:tenant_id/opportunities" in message
    assert "opportunities list endpoint is required" in message


def test_capability_repair_preserves_business_request_without_claiming_success() -> None:
    plan = build_project_scale_run_plan(
        scales=("small",), flows=("direct",), benchmark_kind="capability"
    )
    body = plan.requests[0].body
    repaired = _deliverable_repair_body(
        body, "small:direct", failed_reasons=("requirements: GET /tasks returns 404",),
        benchmark_kind="capability",
    )
    message = str(repaired["message"])
    assert "project_scale=small" in message
    assert "Build a real small business project for flow=direct" in message
    assert "persistent task management API" in message
    assert "POST /tasks" in message
    assert "npm start must listen on the PORT environment variable" in message
    assert "GET /tasks returns 404" in message
    assert "workspace_bundle.files" in message
    assert "build, test, start" in message
    assert "No ellipses" in message
    assert "VERIFICATION.md" in message
    assert "constraints_reading_evidence.json" in message
    assert "single-flight initialization" in message
    assert "serialized read-modify-write" in message
    assert "all true" not in message
    assert len(message) <= 2_000
    RolePlanningRequest(task=message, mode=TaskMode.DIRECT)


def test_ultra_capability_repair_explains_rbac_acceptance_boundary() -> None:
    plan = build_project_scale_run_plan(
        scales=("ultra",), flows=("direct",), benchmark_kind="capability"
    )

    repaired = _deliverable_repair_body(
        plan.requests[0].body,
        "ultra:direct",
        failed_reasons=(
            "requirements: portfolio workflow: POST /programs: expected 201, got 403",
        ),
        benchmark_kind="capability",
    )

    message = str(repaired["message"])
    assert "POST /programs" in message
    assert "must return 201 without requiring an authorization header" in message
    assert "PATCH /approvals/:id" in message
    assert "portfolio_admin" in message
    assert "viewer" in message
    assert "403 or 409" in message
    assert "error.code" in message
    assert "error.message" in message
    assert "created resource directly at the JSON top level" in message
    assert "do not put it inside program, project, milestone, budget, staffing, risk, dependency, or approval" in message
    assert "HTTP JSON helpers must return explicit generic or interface types" in message
    assert "tests must compile under strict TypeScript" in message
    assert "must not invent assertions for internal codes such as CORE" in message
    assert "Customer Migration" in message
    assert "GET /analytics/portfolio.csv" in message
    assert "GET /portfolio/read-model" in message
    assert "GET /dependencies/:id" in message
    assert "same stable id" in message
    assert "after restart" in message
    assert "missing-project" in message
    assert "Do not prefill pass records or fabricate execution" in message
    assert len(message) <= 6_000
    RolePlanningRequest(task=message, mode=TaskMode.DIRECT)


def test_capability_repair_preserves_trusted_standard_events_across_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=("medium",),
        flows=("direct",),
        execute=True,
    )
    validation_results = [
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=("generated_project_validation: first build failed",),
        ),
        project_scale_runner_module._EvidenceCheck(passed=True, reasons=()),
    ]
    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        lambda bundle, **kwargs: validation_results.pop(0),
    )
    standard_event: dict[str, object] = {
        "kind": "artifact.created",
        "payload": {
            "agent_standard_verification": {
                "constraints_read": True,
                "plan_before_implementation": True,
                "reproducible_verification": True,
                "root_cause_repair": True,
            }
        },
    }
    client = FakeAcceptanceClient(
        run_id="run-medium-standard-event",
        session_id="project-scale-medium-direct",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[standard_event],
        repair_events=[],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Service\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0\n"
                    "- npm test: passed exit 0\n"
                    "- interaction smoke: passed\n"
                ),
                "constraints_reading_evidence.json": json.dumps(
                    {
                        "read_before_implementation": True,
                        "constraints": ["AGENTS.md", "HANDOFF", "PROJECT_REQUIREMENTS.md"],
                        "skills": ["applicable SKILL.md rules"],
                    }
                ),
                "package.json": json.dumps({"scripts": {"build": "tsc", "test": "node --test"}}),
                "src/app.ts": _functional_ts_source(),
                "tests/app.test.ts": _functional_ts_test(),
            }
        ),
    )

    report = execute_project_scale_plan(plan, client, validate_generated_project=True)

    assert report.ok is True
    assert report.results[0].evidence["agent_standard_verification"] is True
    assert len(client.submitted_bodies) == 2
    assert validation_results == []


def test_capability_repair_pins_the_observed_mode_for_auto_requests() -> None:
    plan = build_project_scale_run_plan(
        scales=("small",), flows=("direct",), benchmark_kind="capability"
    )
    body = dict(plan.requests[0].body)
    body["mode"] = "auto"

    repaired = _deliverable_repair_body(
        body,
        "small:auto",
        failed_reasons=("generated_project_validation: source files missing",),
        benchmark_kind="capability",
        effective_mode="direct",
    )

    assert repaired["mode"] == "direct"


@pytest.mark.parametrize(
    ("requested_mode", "status", "reason", "expected"),
    (
        ("auto", "failed", "structured output invalid", "direct"),
        ("dispatch", "failed", "structured output invalid", "dispatch"),
        ("auto", "completed", "structured output invalid", "dispatch"),
        ("auto", "failed", "capability execution failed", "direct"),
    ),
)
def test_capability_repair_mode_falls_back_for_failed_auto_dispatch(
    requested_mode: str,
    status: str,
    reason: str,
    expected: str,
) -> None:
    mode = project_scale_runner_module._deliverable_repair_mode(
        {"mode": requested_mode},
        effective_mode="dispatch",
        status=status,
        events=(
            {
                "kind": "runtime.failed",
                "reason": reason,
                "payload": {
                    "error_code": (
                        "model.structured_output_invalid"
                        if reason == "structured output invalid"
                        else "runtime.capability_execution_failed"
                    )
                },
            },
        ),
    )

    assert mode == expected


def test_auto_hybrid_partial_discussion_completion_falls_back_to_direct() -> None:
    mode = project_scale_runner_module._deliverable_repair_mode(
        {"mode": "auto"},
        effective_mode="hybrid",
        status="completed",
        events=(
            {
                "kind": "runtime.completed",
                "reason": "partial_hybrid_after_discussion_failure",
                "payload": {},
            },
        ),
    )

    assert mode == "direct"

    repair = project_scale_runner_module._deliverable_repair_body(
        {"mode": "auto", "message": "Build a real large business project."},
        "large:auto",
        benchmark_kind="capability",
        effective_mode=mode,
    )
    assert repair["allow_scale_mode_upgrade"] is False
    assert repair["replace_workspace_files"] is True


def test_explicit_direct_safe_upgrade_partial_discussion_falls_back_to_pinned_direct() -> None:
    mode = project_scale_runner_module._deliverable_repair_mode(
        {"mode": "direct"},
        effective_mode="hybrid",
        status="completed",
        events=(
            {
                "kind": "runtime.completed",
                "reason": "partial_hybrid_after_discussion_failure",
                "payload": {},
            },
        ),
    )

    assert mode == "direct"

    repair = project_scale_runner_module._deliverable_repair_body(
        {"mode": "direct", "message": "Build a real ultra-large business project."},
        "ultra:direct",
        benchmark_kind="capability",
        effective_mode=mode,
    )
    assert repair["mode"] == "direct"
    assert repair["allow_scale_mode_upgrade"] is False


def test_auto_dispatch_runtime_failure_does_not_start_deliverable_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=("medium",),
        flows=("dispatch",),
        execute=True,
    )
    plan.requests[0].body["mode"] = "auto"
    failed_validation = project_scale_runner_module._EvidenceCheck(
        passed=False,
        reasons=("generated_project_validation: missing workspace bundle",),
    )
    validation_results = [
        failed_validation,
        failed_validation,
        project_scale_runner_module._EvidenceCheck(passed=True, reasons=()),
    ]
    monkeypatch.setattr(
        project_scale_runner_module,
        "_CAPABILITY_DELIVERABLE_REPAIR_SOFT_ATTEMPTS",
        1,
    )
    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        lambda bundle, **kwargs: validation_results.pop(0),
    )
    client = FakeAcceptanceClient(
        run_id="run-medium-auto-dispatch-recovery",
        session_id="project-scale-medium-dispatch",
        statuses=("failed", "failed", "completed"),
        actual_mode="dispatch",
        repair_events=[
            {
                "kind": "runtime.failed",
                "reason": "structured output invalid",
                "payload": {"error_code": "model.structured_output_invalid"},
            }
        ],
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=_project_bundle(
            dict(project_scale_artifact_zip_files("Build a medium CRM service."))
        ),
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert report.ok is False
    assert result.run_id == "run-medium-auto-dispatch-recovery"
    assert result.status == "failed"
    assert result.evidence["deliverable_repair_trace"] is False
    assert [body["mode"] for body in client.submitted_bodies] == ["auto"]


@pytest.mark.parametrize("scale", ("small", "medium", "large", "ultra"))
@pytest.mark.parametrize("status", ("failed", "cancelled", "timed_out", "running"))
def test_capability_auto_runtime_failure_cannot_be_replaced_by_direct_success(
    monkeypatch: pytest.MonkeyPatch,
    scale: str,
    status: str,
) -> None:
    plan = _auto_scale_plan(scale, benchmark_kind="capability")
    validations = [
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=("generated_project_validation: command failed exit=1 command=npm test",),
        ),
        project_scale_runner_module._EvidenceCheck(passed=True, reasons=()),
    ]
    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        lambda bundle, **kwargs: validations.pop(0),
    )
    run_id = f"run-{scale}-auto-runtime-failure"
    client = FakeAcceptanceClient(
        run_id=run_id,
        session_id=f"project-scale-{scale}-auto",
        statuses=(status, "completed"),
        actual_mode="dispatch" if scale in {"small", "medium"} else "hybrid",
        repair_actual_mode="direct",
        create_status="waiting_approval" if scale in {"large", "ultra"} else None,
        repair_create_status="completed",
        decision_token="preflight-test-token",
        decision_version=1,
        artifacts=[{"id": "artifact-1"}],
        events=[_trusted_agent_standard_event()],
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert report.ok is False
    assert report.to_payload()["capability_verified"] is False
    assert result.run_id == run_id
    assert result.evidence["deliverable_repair_trace"] is False
    assert len(client.submitted_bodies) == 1
    assert any("runtime_mode_execution" in error and status in error for error in result.errors)
    assert result.evidence["cleanup_cancel"] is True


@pytest.mark.parametrize("mode", ("direct", "dispatch", "hybrid"))
def test_capability_completed_auto_run_can_repair_failed_npm_test(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    plan = _auto_scale_plan("small", benchmark_kind="capability")
    validations = [
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=(
                (
                    "generated_project_validation: command failed exit=1 command=npm test "
                    "output_tail=TypeError: createApp is not a function"
                ),
            ),
        ),
        project_scale_runner_module._EvidenceCheck(passed=True, reasons=()),
    ]
    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        lambda bundle, **kwargs: validations.pop(0),
    )
    client = FakeAcceptanceClient(
        run_id="run-small-auto-completed",
        session_id="project-scale-small-auto",
        status="completed",
        actual_mode=mode,
        artifacts=[{"id": "artifact-1"}],
        events=[_trusted_agent_standard_event()],
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    assert report.to_payload()["capability_verified"] is True
    assert report.results[0].run_id == client.repair_run_id
    assert report.results[0].evidence["generated_project_validation"] is True
    assert report.results[0].evidence["deliverable_repair_trace"] is True
    assert [body["mode"] for body in client.submitted_bodies] == ["auto", mode]
    assert "createApp is not a function" in str(client.submitted_bodies[1]["message"])
    assert validations == []


@pytest.mark.parametrize("repair_mode", ("dispatch", "direct", "hybrid"))
def test_capability_public_accept_repair_requires_original_auto_route(
    monkeypatch: pytest.MonkeyPatch,
    repair_mode: str,
) -> None:
    plan = _auto_scale_plan("medium", benchmark_kind="capability")
    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        lambda bundle, **kwargs: project_scale_runner_module._EvidenceCheck(
            passed=True, reasons=()
        ),
    )
    client = FakeAcceptanceClient(
        run_id="run-medium-auto-public-recovery",
        session_id="project-scale-medium-auto",
        statuses=("failed", "completed"),
        actual_mode="dispatch",
        repair_actual_mode=repair_mode,
        artifacts=[{"id": "artifact-1"}],
        events=[_trusted_agent_standard_event()],
        self_repair_decision_token="repair-token-12345678901234567890",
        self_repair_decision_version=7,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert report.ok is (repair_mode == "dispatch")
    assert result.run_id == client.repair_run_id
    assert result.observed_mode == "dispatch"
    assert result.final_observed_mode == repair_mode
    assert result.evidence["deliverable_repair_trace"] is True
    assert len(client.submitted_bodies) == 1
    assert (
        "POST", f"/api/v1/runs/{client.run_id}/accept-repair", None
    ) in client.calls
    assert not any("/api/v1/admin/runs/" in path for _, path, _ in client.calls)
    if repair_mode != "dispatch":
        assert any("mode_recovery" in error for error in result.errors)


@pytest.mark.parametrize(
    ("response_mode", "details_mode", "expected_ok", "expected_error"),
    [
        (response_mode, details_mode, False, "unknown")
        for response_mode in (None, "", "auto", "invalid")
        for details_mode in (None, "", "auto", "invalid")
    ]
    + [
        ("direct", "dispatch", False, "conflicting"),
        ("dispatch", "direct", False, "conflicting"),
        ("dispatch", None, True, None),
        (None, "dispatch", True, None),
    ],
)
def test_capability_public_accept_repair_requires_consistent_route_evidence(
    monkeypatch: pytest.MonkeyPatch,
    response_mode: str | None,
    details_mode: str | None,
    expected_ok: bool,
    expected_error: str | None,
) -> None:
    plan = _auto_scale_plan("medium", benchmark_kind="capability")
    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        lambda bundle, **kwargs: project_scale_runner_module._EvidenceCheck(
            passed=True, reasons=()
        ),
    )

    class RecoveryRouteClient(FakeAcceptanceClient):
        def request_json(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object] | list[object]:
            response = super().request_json(
                method, path, body=body, idempotency_key=idempotency_key
            )
            if isinstance(response, dict):
                if path == f"/api/v1/runs/{self.run_id}/accept-repair":
                    mode = response_mode
                elif path == f"/api/v1/runs/{self.repair_run_id}/details":
                    mode = details_mode
                else:
                    return response
                if mode is None:
                    response.pop("mode", None)
                else:
                    response["mode"] = mode
            return response

    client = RecoveryRouteClient(
        run_id="run-medium-auto-unproven-recovery",
        session_id="project-scale-medium-auto",
        statuses=("failed", "completed"),
        actual_mode="dispatch",
        repair_actual_mode="direct",
        artifacts=[{"id": "artifact-1"}],
        events=[_trusted_agent_standard_event()],
        self_repair_decision_token="repair-token-12345678901234567890",
        self_repair_decision_version=7,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert report.ok is expected_ok
    assert report.to_payload()["capability_verified"] is expected_ok
    assert result.run_id == client.repair_run_id
    assert result.status == "completed"
    assert result.observed_mode == "dispatch"
    if expected_error is not None:
        assert any(
            "mode_recovery" in error and expected_error in error for error in result.errors
        )
    else:
        assert result.final_observed_mode == "dispatch"
    assert len(client.submitted_bodies) == 1
    assert result.evidence["cleanup_cancel"] is True


@pytest.mark.parametrize("mode", ("direct", "dispatch", "hybrid"))
def test_capability_public_accept_repair_accepts_explicit_same_route(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    plan = _auto_scale_plan("medium", benchmark_kind="capability")
    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        lambda bundle, **kwargs: project_scale_runner_module._EvidenceCheck(
            passed=True, reasons=()
        ),
    )
    client = FakeAcceptanceClient(
        run_id="run-medium-auto-proven-recovery",
        session_id="project-scale-medium-auto",
        statuses=("failed", "completed"),
        actual_mode=mode,
        repair_actual_mode=mode,
        artifacts=[{"id": "artifact-1"}],
        events=[_trusted_agent_standard_event()],
        self_repair_decision_token="repair-token-12345678901234567890",
        self_repair_decision_version=7,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert report.ok is True
    assert report.to_payload()["capability_verified"] is True
    assert result.run_id == client.repair_run_id
    assert result.status == "completed"
    assert result.observed_mode == mode
    assert result.final_observed_mode == mode
    assert len(client.submitted_bodies) == 1


def test_capability_repair_bounds_long_medium_request_without_blocking_repair() -> None:
    plan = build_project_scale_run_plan(
        scales=("medium",), flows=("direct",), benchmark_kind="capability"
    )
    repaired = _deliverable_repair_body(
        plan.requests[0].body,
        "medium:direct",
        failed_reasons=("CRM workflow: GET accounts: expected object",),
        benchmark_kind="capability",
    )
    message = str(repaired["message"])

    assert "Repair same project" in message
    assert "GET accounts: expected object" in message
    assert "workspace_bundle.files" in message
    assert "build, test, start" in message
    assert "read_before_implementation:true" in message
    assert "AGENTS.md workspace rules" in message
    assert "GET /tenants/:tenant_id/opportunities" in message
    assert "top-level id" in message
    assert "never {item:...}, {data:...}" in message
    assert "opportunities {account_id,name,amount,stage}" in message
    assert "reminders {contact_id,due_at,note}" in message
    assert "stages exactly open, won, lost" in message
    assert "Reference validation order is frozen" in message
    assert "missing or foreign reference returns 404 NOT_FOUND" in message
    assert "Request<{tenant_id:string" in message
    assert "Original request:" in message
    assert len(message) <= 2_000
    RolePlanningRequest(task=message, mode=TaskMode.DIRECT)


def test_capability_execution_enforces_generated_project_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validations: list[bytes | None] = []

    def validate(bundle: bytes | None, **kwargs: object) -> object:
        validations.append(bundle)
        return project_scale_runner_module._EvidenceCheck(passed=True, reasons=())

    monkeypatch.setattr(project_scale_runner_module, "_validate_generated_project_bundle", validate)
    plan = build_project_scale_run_plan(
        scales=("small",), flows=("direct",), execute=True, benchmark_kind="capability"
    )
    report = execute_project_scale_plan(plan, FakeAcceptanceClient())

    assert validations
    assert report.results[0].evidence["generated_project_validation"] is True
    assert report.to_payload()["benchmark_kind"] == "capability"
    assert report.to_payload()["capability_verified"] is False


def test_requested_web_preview_requires_a_workspace_html_entrypoint() -> None:
    request = {
        "message": (
            "Build the API and include a complete interactive website. "
            "Put a self-contained preview.html or index.html entrypoint in the workspace."
        )
    }

    missing = project_scale_runner_module._validate_requested_web_preview(
        _project_bundle({"README.md": "# API\n", "src/server.ts": "export {};\n"}),
        request,
    )
    present = project_scale_runner_module._validate_requested_web_preview(
        _project_bundle(
            {
                "README.md": "# API and UI\n",
                "public/index.html": "<!doctype html><html><body><button>Run</button></body></html>",
            }
        ),
        request,
    )

    assert missing.passed is False
    assert missing.reasons == (
        "requirements: requested web preview entrypoint missing; add preview.html or index.html",
    )
    assert present.passed is True


def test_non_preview_request_does_not_require_html_entrypoint() -> None:
    result = project_scale_runner_module._validate_requested_web_preview(
        _project_bundle({"README.md": "# API only\n"}),
        {"message": "Build a REST API with tests."},
    )

    assert result.passed is True


@pytest.mark.usefixtures("trusted_python_fixture_commands")
def test_capability_build_success_cannot_replace_independent_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        project_scale_runner_module, "validate_small_task_api",
        lambda root, timeout_seconds: ("requirements: task API missing",),
        raising=False,
    )
    result = project_scale_runner_module._validate_generated_project_bundle(
        _project_bundle({"package.json": "{}"}),
        commands=((sys.executable, "-c", "pass"),),
        timeout_seconds=10,
        requirements_case_id="small:direct",
    )
    assert result.passed is False
    assert "requirements: task API missing" in result.reasons


@pytest.mark.usefixtures("trusted_python_fixture_commands")
def test_capability_medium_uses_independent_crm_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        project_scale_runner_module,
        "validate_medium_crm_api",
        lambda root, timeout_seconds: ("requirements: tenant isolation missing",),
        raising=False,
    )
    result = project_scale_runner_module._validate_generated_project_bundle(
        _project_bundle({"package.json": "{}"}),
        commands=((sys.executable, "-c", "pass"),),
        timeout_seconds=10,
        requirements_case_id="medium:direct",
    )
    assert result.passed is False
    assert "requirements: tenant isolation missing" in result.reasons


@pytest.mark.usefixtures("trusted_python_fixture_commands")
def test_capability_large_uses_independent_order_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        project_scale_runner_module,
        "validate_large_order_ops_api",
        lambda root, timeout_seconds: (
            "order operations workflow: POST /inventory/stock: missing id",
        ),
        raising=False,
    )
    result = project_scale_runner_module._validate_generated_project_bundle(
        _project_bundle({"package.json": "{}"}),
        commands=((sys.executable, "-c", "pass"),),
        timeout_seconds=10,
        requirements_case_id="large:direct",
    )
    assert result.passed is False
    assert result.reasons == (
        "requirements: order operations workflow: POST /inventory/stock: missing id",
    )
    assert project_scale_runner_module._generated_project_validation_stage(result) == 3


@pytest.mark.usefixtures("trusted_python_fixture_commands")
def test_capability_ultra_uses_independent_portfolio_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(project_scale_runner_module, "validate_ultra_portfolio_storage",
                        lambda *args, **kwargs: _ultra_load_result())
    monkeypatch.setattr(
        project_scale_runner_module,
        "validate_ultra_portfolio_api",
        lambda root, timeout_seconds: ("requirements: RBAC approval missing",),
        raising=False,
    )
    result = project_scale_runner_module._validate_generated_project_bundle(
        _project_bundle({"package.json": "{}"}),
        commands=((sys.executable, "-c", "pass"),),
        timeout_seconds=10,
        requirements_case_id="ultra:direct",
    )
    assert result.passed is False
    assert "requirements: RBAC approval missing" in result.reasons


def test_controlled_ultra_project_bundle_without_pagination_is_rejected() -> None:
    if shutil.which("npm") is None:
        pytest.skip("npm is required for generated project validation")
    if not project_scale_runner_module._generated_project_validation_is_isolated():
        pytest.skip("generated npm projects require the isolated systemd validator")
    bundle = _project_bundle(
        dict(
            project_scale_artifact_zip_files(
                "Build a real ultra-large business project for flow=direct. "
                "Acceptance conditions require enterprise portfolio OS APIs, analytics, "
                "RBAC, persistence, tests, and verification evidence."
            )
        )
    )

    result = project_scale_runner_module._validate_generated_project_bundle(
        bundle,
        commands=project_scale_runner_module._DEFAULT_GENERATED_PROJECT_COMMANDS,
        timeout_seconds=30,
        requirements_case_id="ultra:direct",
    )

    assert result.passed is False
    assert any("requirements: ultra load failed:" in reason for reason in result.reasons)
    assert result.scale_validation is None


def test_capability_quality_uses_executed_checks_instead_of_claimed_pass_records() -> None:
    bundle = _project_bundle({
        "README.md": "# Task API",
        "src/main.js": _functional_js_source(),
        "tests/main.test.js": _functional_js_test() + "\n// input example: hello world\n",
        "VERIFICATION.md": "Not executed by the author; run independent verification.",
    })
    passed = project_scale_runner_module._EvidenceCheck(passed=True, reasons=())
    failed = project_scale_runner_module._EvidenceCheck(
        passed=False, reasons=("requirements: persistence lost",)
    )
    assert project_scale_runner_module._executed_capability_quality(bundle, passed).passed
    assert not project_scale_runner_module._executed_capability_quality(bundle, failed).passed


@pytest.mark.parametrize("claimed_standard", (False, True))
@pytest.mark.parametrize("validation_failure", (None, "build failed", "requirements failed"))
def test_capability_standard_stays_unverified_after_delivery_validation_and_repair(
    monkeypatch: pytest.MonkeyPatch,
    claimed_standard: bool,
    validation_failure: str | None,
) -> None:
    outcomes = [False, True, True, True] if validation_failure else [True, True, True, True]

    def validate(bundle: bytes | None, **kwargs: object) -> object:
        passed = outcomes.pop(0)
        return project_scale_runner_module._EvidenceCheck(
            passed=passed, reasons=() if passed else (str(validation_failure),)
        )

    monkeypatch.setattr(project_scale_runner_module, "_validate_generated_project_bundle", validate)
    plan = build_project_scale_run_plan(
        scales=("small",), flows=("direct",), execute=True, benchmark_kind="capability"
    )
    client = FakeAcceptanceClient(
        status="completed", artifacts=[{"id": "artifact-1"}], agent_standard=claimed_standard
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert len(client.submitted_bodies) == 4
    assert result.evidence["agent_standard_verification"] is False
    assert result.evidence["generated_project_validation"] is True
    assert result.evidence["requirements_validation"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.missing_evidence == ("agent_standard_verification",)
    assert (
        "agent_standard_verification: trusted runtime context/plan evidence unavailable"
        in result.errors
    )
    assert report.ok is False
    assert report.to_payload()["capability_verified"] is False
    assert result.repair_attempted is True
    assert result.run_id == client.repair_run_id
    assert outcomes == []
    if validation_failure:
        assert validation_failure in str(client.submitted_bodies[1]["message"])


@pytest.mark.parametrize("benchmark_kind", ("fixture", "capability"))
@pytest.mark.parametrize(
    "failure", (None, "workspace_bundle", "deliverable_quality", "generated_project_validation")
)
def test_process_evidence_only_triggers_fixture_repair(
    benchmark_kind: ProjectScaleBenchmarkKind, failure: str | None
) -> None:
    evidence = {
        "final_artifacts": True,
        "workspace_bundle": True,
        "deliverable_quality": True,
        "generated_project_validation": True,
        "agent_standard_verification": False,
    }
    if failure:
        evidence[failure] = False

    assert _should_attempt_deliverable_repair(
        status="completed", evidence=evidence, case_id="small:direct", benchmark_kind=benchmark_kind
    ) is True


def test_running_run_with_final_bundle_and_failed_validation_triggers_repair() -> None:
    evidence = {
        "final_artifacts": True,
        "workspace_bundle": True,
        "deliverable_quality": False,
        "generated_project_validation": False,
        "agent_standard_verification": False,
    }

    assert _should_attempt_deliverable_repair(
        status="running",
        evidence=evidence,
        case_id="small:dispatch",
        benchmark_kind="capability",
    ) is True


def test_completed_capability_bundle_without_final_attachment_triggers_repair() -> None:
    evidence = {
        "final_artifacts": False,
        "workspace_bundle": True,
        "deliverable_quality": False,
        "generated_project_validation": True,
        "requirements_validation": True,
        "agent_standard_verification": False,
    }

    assert _should_attempt_deliverable_repair(
        status="completed",
        evidence=evidence,
        case_id="small:auto",
        benchmark_kind="capability",
    ) is True


@pytest.mark.parametrize("benchmark_kind", ("fixture", "capability"))
@pytest.mark.parametrize(
    "source", ("details", "model_text", "event_flags", "zip_flags", "zip_reading", "zip_plan")
)
def test_agent_standard_self_reports_are_fixture_only(
    benchmark_kind: ProjectScaleBenchmarkKind, source: str
) -> None:
    claim: dict[str, object] = {"agent_standard_verification": {
        "constraints_read": True,
        "plan_before_implementation": True,
        "reproducible_verification": True,
        "root_cause_repair": True,
    }}
    details: dict[str, object] | None = None
    events: list[object] | None = None
    files: dict[str, str] = {}
    if source == "details":
        details = claim
    elif source == "model_text":
        details = {"artifact": {"content": {"text": json.dumps(claim)}}}
    elif source == "event_flags":
        events = [{"kind": "tool.completed", "tool_name": "project.generate_zip", "payload": claim}]
    elif source == "zip_flags":
        files["verification.json"] = json.dumps(claim)
    else:
        files["IMPLEMENTATION_PLAN.md"] = _AGENT_STANDARD_IMPLEMENTATION_PLAN if (
            source == "zip_plan"
        ) else "Implement the API."
        files["VERIFICATION.md"] = "All checks passed."
        if source == "zip_reading":
            files["constraints_reading_evidence.json"] = json.dumps({
                "read_before_implementation": True,
                "constraints": ["AGENTS.md", "HANDOFF.md", "PROJECT_REQUIREMENTS.md"],
                "skills": ["SKILL.md"],
            })
    bundle = _project_bundle(files) if files else None

    check = _evaluate_agent_standard_verification(
        details, events, bundle, benchmark_kind=benchmark_kind
    )

    assert check.passed is (benchmark_kind == "fixture")
    assert _has_agent_standard_verification(
        details, events, bundle, benchmark_kind=benchmark_kind
    ) is check.passed
    if benchmark_kind == "capability":
        assert check.reasons
        if source == "event_flags":
            assert "workspace_bundle: missing project bundle" in check.reasons
        else:
            assert (
                "agent_standard_verification: trusted runtime context/plan evidence unavailable"
                in check.reasons
            )


@pytest.mark.parametrize("events", (
    None,
    [],
    [{"kind": "tool.completed", "tool_name": "workspace.read", "payload": {
        "status": "succeeded", "workspace_files": [{"path": "AGENTS.md", "sha256": "a" * 64}],
    }}],
    [{"kind": "checkpoint.saved", "checkpoint": {"state": {"plan_digest": "a" * 64}}}],
))
def test_capability_standard_requires_runtime_contract(events: list[object] | None) -> None:
    check = _evaluate_agent_standard_verification(
        None, events, None, benchmark_kind="capability"
    )

    assert check.passed is False
    assert check.reasons


def test_capability_standard_accepts_trusted_runtime_context_plan_and_execution_trace() -> None:
    bundle = _project_bundle(
        {
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "verification/report.md": "- npm test: passed\n- interaction smoke: passed\n",
        }
    )
    events: list[object] = [
        {"kind": "runtime.context_loaded", "payload": {"source_count": 3}},
        {"kind": "runtime.plan_created", "payload": {"step_count": 2}},
        {"kind": "step.completed", "step_id": "implementer_step"},
    ]

    check = _evaluate_agent_standard_verification(
        None,
        events,
        bundle,
        benchmark_kind="capability",
    )

    assert check.passed is True
    assert check.reasons == ()


def test_capability_standard_accepts_completed_direct_runtime_trace_with_workspace_plan() -> None:
    bundle = _project_bundle(
        {
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "verification/report.md": "- npm test: passed\n- interaction smoke: passed\n",
        }
    )
    events: list[object] = [
        {"kind": "context.loaded", "payload": {"source_count": 3}},
        {"kind": "runtime.completed", "payload": {"status": "completed"}},
    ]

    check = _evaluate_agent_standard_verification(
        None,
        events,
        bundle,
        benchmark_kind="capability",
    )

    assert check.passed is True
    assert check.reasons == ()


@pytest.mark.parametrize("scale", ("small", "medium", "large", "ultra"))
@pytest.mark.parametrize("delivery", ("incremental", "replacement", "forced_replacement"))
def test_capability_repair_requires_canonical_report_without_changing_delivery(
    scale: str, delivery: str,
) -> None:
    body: dict[str, object] = {
        "message": f"Build a real {scale} business API.",
        "mode": "hybrid",
        "project_id": "report-contract",
        "workspace_session_id": "report-contract-session",
    }
    bundle = _project_bundle({"src/main.ts": "export const value = 1;\n"})

    repair = _deliverable_repair_body(
        body,
        f"{scale}:hybrid",
        benchmark_kind="capability",
        source_workspace_bundle=bundle if delivery != "replacement" else None,
        force_workspace_replacement=delivery == "forced_replacement",
        failed_reasons=("workspace_bundle: missing verification report artifact",),
    )

    message = str(repair["message"])
    assert repair["mode"] == "hybrid"
    assert repair["project_id"] == "report-contract"
    assert repair["workspace_session_id"] == "report-contract-session"
    assert repair["replace_workspace_files"] is (delivery != "incremental")
    assert f"case_id={scale}:hybrid project_scale={scale} flow=hybrid" in message
    assert "workspace_bundle.files" in message
    assert "### `path` fences" in message
    assert "root-level VERIFICATION.md" in message
    assert "build, test, and interaction commands" in message
    assert "package.json scripts: build, test, start" in message
    if delivery == "incremental":
        assert "Return only complete changed files" in message
        assert "Unchanged workspace files remain authoritative" in message
        assert "If only the report is missing, return only VERIFICATION.md" in message
    else:
        assert "Replace the entire workspace" in message
    assert len(message) <= (6_000 if delivery != "replacement" or scale == "ultra" else 2_000)


@pytest.mark.parametrize("scale", ("small", "medium", "large", "ultra"))
@pytest.mark.parametrize("delivery", ("incremental", "replacement", "forced_replacement"))
def test_capability_repair_requires_honest_unexecuted_checks(scale: str, delivery: str) -> None:
    bundle = _project_bundle({"src/main.ts": "export const value = 1;\n"})

    repair = _deliverable_repair_body(
        {"message": f"Build a real {scale} business API.", "mode": "direct"},
        f"{scale}:direct",
        benchmark_kind="capability",
        source_workspace_bundle=bundle if delivery != "replacement" else None,
        force_workspace_replacement=delivery == "forced_replacement",
        failed_reasons=("workspace_bundle: missing verification report artifact",),
    )

    message = str(repair["message"])
    assert "mark checks not executed when they were not run" in message
    assert "Do not prefill pass records or fabricate execution" in message
    assert "all true" not in message


@pytest.mark.parametrize("saturated_evidence", (False, True))
@pytest.mark.parametrize("preview_required", (False, True))
@pytest.mark.parametrize("flow", (
    "artifact_production", "direct", "dispatch", "hybrid", "multi_agent",
))
def test_medium_compact_report_repair_preserves_complete_business_guidance(
    saturated_evidence: bool, preview_required: bool, flow: str,
) -> None:
    plan = build_project_scale_run_plan(
        scales=("medium",), flows=(flow,), benchmark_kind="capability",
    )
    body = dict(plan.requests[0].body)
    if preview_required:
        body["message"] = (
            str(body["message"])
            + " Put a self-contained preview.html or index.html entrypoint in the workspace."
        )
    reasons = ["workspace_bundle: missing verification report artifact"]
    if saturated_evidence:
        reasons.insert(0, "generated_project_validation: " + "compile evidence " * 100)

    repair = _deliverable_repair_body(
        body, f"medium:{flow}", benchmark_kind="capability",
        failed_reasons=tuple(reasons),
    )

    message = str(repair["message"])
    assert len(message) <= 6_000
    assert "Reference validation order is frozen" in message
    assert "before validating unrelated fields" in message
    assert "even if email, due_at, note, amount, or stage is absent or invalid" in message
    assert "Request<{tenant_id:string,...}>" in message
    assert "root-level VERIFICATION.md" in message
    assert "mark checks not executed when they were not run" in message
    assert "Do not prefill pass records or fabricate execution" in message
    assert "constraints_reading_evidence.json" in message
    assert "read_before_implementation:true" in message
    assert "AGENTS.md workspace rules, HANDOFF, PROJECT_REQUIREMENTS.md" in message
    assert "applicable SKILL.md or agent-standard rules" in message
    if not preview_required and flow in {"direct", "dispatch", "hybrid"}:
        assert len(message) <= 2_000
    if preview_required:
        assert "Every authoritative replacement must include preview.html or index.html" in message
    if flow == "multi_agent":
        assert "normalized agent_id values architect, implementer, tester, and synthesizer" in message
        assert "explicit handoffs" in message


def test_compact_repair_budget_only_expands_for_guidance_not_external_text() -> None:
    guidance = "MANDATORY_CONTRACT " * 130
    external = "UNTRUSTED_NOISE " * 20_000
    message = project_scale_runner_module._compose_capability_repair_message(
        guidance=guidance, failed_evidence=external, original_request=external,
        workspace_context="", max_chars=2_000,
    )

    assert message.startswith(" ".join(guidance.split()))
    assert 2_000 < len(message) <= len(" ".join(guidance.split())) + 260
    assert message.count("UNTRUSTED_NOISE") < 20


def test_compact_repair_budget_has_bounded_contract_expansion() -> None:
    message = project_scale_runner_module._compose_capability_repair_message(
        guidance="OVERSIZED_CONTRACT " * 20_000, failed_evidence="FAILURE",
        original_request="REQUEST", workspace_context="", max_chars=2_000,
    )

    assert len(message) <= 6_000
    assert "FAILURE" in message


def test_full_repair_budget_preserves_guidance_with_bounded_external_sections() -> None:
    guidance = "MANDATORY_CONTRACT " * 400
    evidence = "FAILURE_NOISE " * 20_000
    original = "REQUEST_NOISE " * 20_000
    context = "WORKSPACE_NOISE " * 20_000
    message = project_scale_runner_module._compose_capability_repair_message(
        guidance=guidance, failed_evidence=evidence, original_request=original,
        workspace_context=context, max_chars=6_000,
    )

    contract = " ".join(guidance.split())
    assert message.startswith(contract + " ")
    assert len(message) <= len(contract) + 1_100 + 800 + 1_500 + 22
    assert message.count("FAILURE_NOISE") < 90
    assert message.count("REQUEST_NOISE") < 65
    assert message.count("WORKSPACE_NOISE") < 105
    assert "Original request:" in message


def test_report_only_incremental_patch_preserves_source_and_package() -> None:
    source = "export const value = 1;\n"
    package = '{"scripts":{"build":"tsc","test":"node --test","start":"node dist/main.js"}}'
    report = "Build, test, and interaction checks not executed; run npm run build and npm test.\n"
    base = _project_bundle({"src/main.ts": source, "package.json": package})

    merged = project_scale_runner_module._merged_workspace_bundle(
        base, _project_bundle({"VERIFICATION.md": report})
    )

    assert merged is not None
    with zipfile.ZipFile(BytesIO(merged)) as archive:
        assert set(archive.namelist()) == {"src/main.ts", "package.json", "VERIFICATION.md"}
        assert archive.read("src/main.ts").decode("utf-8") == source
        assert archive.read("package.json").decode("utf-8") == package
        assert archive.read("VERIFICATION.md").decode("utf-8") == report


@pytest.mark.parametrize("report_path", (
    "VERIFICATION.md", "docs/verification-report.md", "VERIFICATION_REPORT.md",
    "test-report.md", "test_report.md", "acceptance-report.md", "acceptance_report.md",
    "validation.md", "verification/report.md", "Verification/REPORT.md",
))
def test_recognized_verification_report_body_supplies_execution_evidence(report_path: str) -> None:
    bundle = _project_bundle({
        "README.md": "# Task API\n",
        "src/main.js": _functional_js_source(),
        "tests/main.test.js": _functional_js_test(),
        "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
        report_path: "- npm run build: passed exit 0\n- npm test: passed exit 0\n"
        "- interaction smoke: passed\n",
    })

    assert _workspace_bundle_agent_standard_reasons(bundle) == ()
    assert project_scale_runner_module._workspace_bundle_project_quality_reasons(bundle) == ()


@pytest.mark.parametrize("report_path", (
    "report.md", "docs/report.md", "other/report.md", "docs/verification/report.md", "README.md",
))
def test_unrecognized_report_body_cannot_supply_execution_evidence(report_path: str) -> None:
    files = {
        "README.md": "# Task API\n",
        "src/main.js": _functional_js_source(),
        "tests/main.test.js": _functional_js_test(),
        "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
    }
    files[report_path] = (
        "- npm run build: passed exit 0\n- npm test: passed exit 0\n- interaction smoke: passed\n"
    )
    bundle = _project_bundle(files)

    assert "workspace_bundle: missing verification report artifact" in (
        _workspace_bundle_agent_standard_reasons(bundle)
    )
    reasons = project_scale_runner_module._workspace_bundle_project_quality_reasons(bundle)
    assert "workspace_bundle: missing build/test execution evidence" in reasons
    assert "workspace_bundle: missing interaction execution evidence" in reasons


def test_workspace_bundle_accepts_report_inside_verification_directory() -> None:
    bundle = _project_bundle(
        {
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "verification/report.md": "- npm test: passed\n- interaction smoke: passed\n",
        }
    )

    reasons = _workspace_bundle_agent_standard_reasons(bundle)

    assert "workspace_bundle: missing verification report artifact" not in reasons


def test_capability_standard_accepts_public_event_with_workspace_plan_evidence() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Capability Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )
    events: list[object] = [
        {
            "kind": "artifact.created",
            "payload": {
                "agent_standard_verification": {
                    "constraints_read": True,
                    "plan_before_implementation": True,
                    "reproducible_verification": True,
                    "root_cause_repair": True,
                },
            },
        }
    ]

    check = _evaluate_agent_standard_verification(None, events, bundle, benchmark_kind="capability")

    assert check.passed is True
    assert check.reasons == ()


def test_capability_standard_rejects_details_only_self_report() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Capability Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": "- npm run build: passed exit 0; vite build completed\n",
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )
    details: dict[str, object] = {
        "agent_standard_verification": {
            "constraints_read": True,
            "plan_before_implementation": True,
            "reproducible_verification": True,
            "root_cause_repair": True,
        },
    }

    check = _evaluate_agent_standard_verification(
        details, None, bundle, benchmark_kind="capability"
    )

    assert check.passed is False
    assert check.reasons == (
        "agent_standard_verification: trusted runtime context/plan evidence unavailable",
    )


def test_execute_project_scale_plan_capability_accepts_public_event_and_workspace_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def validate(bundle: bytes | None, **kwargs: object) -> object:
        return project_scale_runner_module._EvidenceCheck(passed=True, reasons=())

    monkeypatch.setattr(project_scale_runner_module, "_validate_generated_project_bundle", validate)
    plan = build_project_scale_run_plan(
        scales=("small",), flows=("direct",), execute=True, benchmark_kind="capability"
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "run_id": "run-small-direct",
                "payload": {
                    "agent_standard_verification": {
                        "constraints_read": True,
                        "plan_before_implementation": True,
                        "reproducible_verification": True,
                        "root_cause_repair": True,
                    },
                },
            }
        ],
    )

    report = execute_project_scale_plan(
        plan,
        client,
        validate_generated_project=True,
    )

    assert report.ok is True
    assert report.results[0].evidence["agent_standard_verification"] is True
    assert report.to_payload()["capability_verified"] is True


@pytest.mark.parametrize("benchmark_kind", ("fixture", "capability"))
def test_execution_report_describes_benchmark_verification_scope(
    benchmark_kind: ProjectScaleBenchmarkKind,
) -> None:
    payload = ProjectScaleExecutionReport(results=(), benchmark_kind=benchmark_kind).to_payload()

    assert payload["capability_verified"] is False
    if benchmark_kind == "capability":
        assert payload["verification_scope"] == (
            "actual build/test and per-case independent business checks; "
            "runtime process evidence unverified"
        )
    else:
        assert payload["verification_scope"] == (
            "synthetic fixture regression; not real project capability or recovery proof"
        )


def test_execution_report_marks_capability_verified_when_all_capability_cases_pass() -> None:
    result = ProjectScaleCaseResult(
        case_id="small:direct",
        run_id="run-1",
        status="completed",
        evidence={
            "agent_standard_verification": True,
            "cleanup_cancel": True,
            "deliverable_quality": True,
            "final_artifacts": True,
            "generated_project_validation": True,
            "requirements_validation": True,
            "run_details": True,
            "run_events": True,
            "terminal_status": True,
            "workspace_bundle": True,
        },
    )

    payload = ProjectScaleExecutionReport(
        results=(result,), benchmark_kind="capability"
    ).to_payload()

    assert payload["ok"] is True
    assert payload["capability_verified"] is True


def test_plain_execution_output_uses_report_capability_verified(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = ProjectScaleCaseResult(
        case_id="small:direct",
        run_id="run-1",
        status="completed",
        evidence={
            "agent_standard_verification": True,
            "cleanup_cancel": True,
            "deliverable_quality": True,
            "final_artifacts": True,
            "run_details": True,
            "run_events": True,
            "terminal_status": True,
            "workspace_bundle": True,
        },
    )

    def execute(*args: object, **kwargs: object) -> ProjectScaleExecutionReport:
        return ProjectScaleExecutionReport(results=(result,), benchmark_kind="capability")

    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "token")
    monkeypatch.setattr(project_scale_runner_module, "execute_project_scale_plan", execute)

    exit_code = project_scale_runner_module.main(
        ["--benchmark-kind", "capability", "--scale", "small", "--flow", "direct", "--execute"]
    )

    assert exit_code == 0
    assert "benchmark_kind=capability capability_verified=true" in capsys.readouterr().out


def test_project_scale_runner_execute_defaults_to_full_matrix_without_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_execute_project_scale_plan(
        *args: object, **kwargs: object
    ) -> ProjectScaleExecutionReport:
        captured["plan"] = args[0]
        captured["kwargs"] = kwargs
        return ProjectScaleExecutionReport(results=(), benchmark_kind="fixture")

    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(
        project_scale_runner_module,
        "execute_project_scale_plan",
        fake_execute_project_scale_plan,
    )

    exit_code = project_scale_runner_module.main(
        ["--execute", "--json", "--benchmark-kind", "fixture"]
    )

    output = capsys.readouterr()
    payload = json.loads(output.out)
    plan = captured["plan"]
    assert exit_code == 0
    assert payload["execute"] is True
    assert payload["dry_run"] is False
    assert isinstance(plan, ProjectScaleRunPlan)
    assert plan.execute is True
    assert plan.dry_run is False
    assert plan.case_count == len(PROJECT_SCALE_TIERS) * len(PROJECT_SCALE_FLOW_KINDS)
    assert {request.case_id for request in plan.requests} == {
        f"{scale}:{flow}" for scale in PROJECT_SCALE_TIERS for flow in PROJECT_SCALE_FLOW_KINDS
    }
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["validate_generated_project"] is False


def test_project_scale_runner_execute_can_enable_generated_project_validation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_execute_project_scale_plan(
        *args: object, **kwargs: object
    ) -> ProjectScaleExecutionReport:
        captured["plan"] = args[0]
        captured["kwargs"] = kwargs
        return ProjectScaleExecutionReport(results=(), benchmark_kind="fixture")

    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "test-token")
    monkeypatch.setenv("AGENT_HUB_PROJECT_SCALE_VERIFY_ARTIFACT_BUILD", "1")
    monkeypatch.setattr(
        project_scale_runner_module,
        "execute_project_scale_plan",
        fake_execute_project_scale_plan,
    )

    exit_code = project_scale_runner_module.main(
        ["--execute", "--artifact-build-timeout", "9", "--json", "--benchmark-kind", "fixture"]
    )

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert exit_code == 0
    assert payload["execute"] is True
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["validate_generated_project"] is True
    assert kwargs["generated_project_timeout_seconds"] == 9


def test_project_scale_runner_prints_dry_run_plan_focus_in_text() -> None:
    result = run_project_scale_runner(
        "--scale", "small", "--flow", "capability_validation", "--benchmark-kind", "fixture"
    )

    assert result.returncode == 0
    assert (
        "small:capability_validation "
        "focus=interaction_stability,final_result,deliverable_quality,"
        "agent_standard_verification,capability_matrix,mode_control,no_silent_downgrade"
    ) in result.stdout


def test_discussion_trace_rejects_empty_disagreement_evidence() -> None:
    assert (
        _discussion_trace_payload_passes(
            {
                "participants": ["architect", "reviewer"],
                "member_statements": [
                    {"member": "architect", "position": "Plan first."},
                    {"member": "reviewer", "position": "Verify before release."},
                ],
                "disagreements": [],
                "verification_steps": ["Run the acceptance suite."],
                "final_decision": "Proceed after verification.",
            }
        )
        is False
    )


@pytest.mark.parametrize(
    "override",
    [
        {"participants": [""]},
        {"member_statements": [{}]},
        {"member_statements": [{"member": [""], "position": "Plan first."}]},
        {"disagreements": [" "]},
        {"disagreements": [{}]},
        {"verification_steps": [""]},
    ],
)
def test_discussion_trace_rejects_empty_coordination_details(
    override: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "participants": ["architect", "reviewer"],
        "member_statements": [
            {"member": "architect", "position": "Plan first."},
            {"member": "reviewer", "position": "Verify before release."},
        ],
        "disagreements": ["Scope risk needs verification."],
        "verification_steps": ["Run the acceptance suite."],
        "final_decision": "Proceed after verification.",
    }
    payload.update(override)

    assert _discussion_trace_payload_passes(payload) is False


def test_self_repair_trace_rejects_generic_repair_text() -> None:
    assert (
        _has_self_repair_trace(
            [
                {
                    "kind": "message.created",
                    "message": "The plan mentions repair readiness but no repair event occurred.",
                }
            ]
        )
        is False
    )


def test_self_repair_trace_rejects_note_style_marker() -> None:
    assert _has_self_repair_trace([{"kind": "message.self_repair_note"}]) is False


@pytest.mark.parametrize(
    "event",
    [
        {"kind": "repair.classified"},
        {"kind": "runtime.self_repair.completed"},
    ],
)
def test_self_repair_trace_accepts_explicit_repair_events(event: dict[str, object]) -> None:
    assert _has_self_repair_trace([event]) is True


def test_deliverable_repair_trace_rejects_generic_keyword_text() -> None:
    assert (
        _has_deliverable_repair_trace(
            [
                {
                    "kind": "message.created",
                    "message": "Operator asked for deliverable.repair evidence in the prompt.",
                }
            ]
        )
        is False
    )


@pytest.mark.parametrize(
    "event",
    [
        {"kind": "deliverable.repair.completed", "run_id": "repair-run"},
        {"payload": {"event": "deliverable.repair.started"}},
    ],
)
def test_deliverable_repair_trace_accepts_explicit_event_markers(event: object) -> None:
    assert _has_deliverable_repair_trace([event]) is True


def test_plugin_contract_payload_rejects_boolean_only_shell() -> None:
    assert (
        _plugin_contract_payload_passes(
            {
                "manifest_discovered": True,
                "adapter_contract_checked": True,
                "policy_boundary_checked": True,
                "sandbox_profile_checked": True,
                "failure_recovery_checked": True,
            }
        )
        is False
    )


def test_plugin_contract_payload_accepts_auditable_contract_details() -> None:
    assert (
        _plugin_contract_payload_passes(
            {
                "manifest_discovered": True,
                "adapter_contract_checked": True,
                "policy_boundary_checked": True,
                "sandbox_profile_checked": True,
                "failure_recovery_checked": True,
                "manifest_ref": "project-scale-plugin-manifest",
                "adapter_ref": "project.generate_zip",
                "policy_ref": "fail-closed plugin policy",
                "sandbox_ref": "workspace_write",
                "recovery_ref": "install/start failure recovery",
            }
        )
        is True
    )


def test_project_scale_runner_writes_json_report_to_output_path(tmp_path: Path) -> None:
    output_path = tmp_path / "project-scale-report.json"

    result = run_project_scale_runner(
        "--scale",
        "small",
        "--flow",
        "direct",
        "--json",
        "--output",
        str(output_path),
        "--benchmark-kind",
        "fixture",
    )

    assert result.returncode == 0
    stdout_payload = json.loads(result.stdout)
    file_payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert file_payload == stdout_payload
    assert file_payload["requests"][0]["case_id"] == "small:direct"


def test_project_scale_runner_rejects_execute_without_token() -> None:
    result = run_project_scale_runner(
        "--execute", "--scale", "small", "--flow", "direct", "--benchmark-kind", "fixture"
    )

    assert result.returncode == 2
    assert (
        "AGENT_HUB_ACCEPTANCE_BEARER_TOKEN or "
        "AGENT_HUB_ACCEPTANCE_USERNAME/PASSWORD is required for --execute"
    ) in result.stderr
    assert "AGENT_HUB_ACCEPTANCE_LOGIN_USERNAME/PASSWORD" in result.stderr


def test_project_scale_runner_accepts_harness_login_env_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_USERNAME", raising=False)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_PASSWORD", raising=False)
    monkeypatch.delenv("AGENT_HUB_ACCEPTANCE_TENANT_ID", raising=False)
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_LOGIN_USERNAME", "admin")
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_LOGIN_PASSWORD", "valid-password")
    monkeypatch.setenv("AGENT_HUB_ACCEPTANCE_LOGIN_TENANT_ID", "tenant-1")

    assert _acceptance_credentials_from_env() == ("admin", "valid-password", "tenant-1")


def test_project_scale_runner_rejects_unknown_filters() -> None:
    result = run_project_scale_runner("--scale", "tiny", "--json", "--benchmark-kind", "fixture")

    assert result.returncode == 2
    assert "unknown project scale: tiny" in result.stderr


def test_urllib_acceptance_client_reauthenticates_once_on_expired_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None, bytes | None]] = []

    class UrlopenRequest(Protocol):
        full_url: str
        data: bytes | None

        def get_header(self, header_name: str) -> str | None: ...

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return self.payload

    def fake_urlopen(request: object, *, timeout: float) -> Response:
        del timeout
        request = cast(UrlopenRequest, request)
        url = request.full_url
        auth = request.get_header("Authorization")
        data = request.data
        calls.append((url, auth, data))
        if len(calls) == 1:
            raise HTTPError(
                url,
                401,
                "Unauthorized",
                hdrs=Message(),
                fp=BytesIO(
                    b'{"error":{"code":"invalid_token","message":"invalid access token"}}'
                ),
            )
        if url.endswith("/api/v1/auth/login"):
            assert auth is None
            return Response(
                b'{"access_token":"fresh-token","token_type":"bearer",'
                b'"principal":{"user_id":"11111111-1111-4111-8111-111111111111",'
                b'"tenant_id":"22222222-2222-4222-8222-222222222222",'
                b'"role":"super_admin"}}'
            )
        return Response(b'{"ok":true}')

    monkeypatch.setattr("agent_hub.harness.project_scale_runner.urlopen", fake_urlopen)
    client = UrllibAcceptanceClient(
        base_url="http://agent-hub.local",
        bearer_token="expired-token",
        username="test",
        password="valid password",
    )

    result = client.request_json("GET", "/api/v1/runs/run-1/details")

    assert result == {"ok": True}
    assert [call[1] for call in calls] == [
        "Bearer expired-token",
        None,
        "Bearer fresh-token",
    ]


@pytest.mark.parametrize("status_code", [404, 410])
def test_urllib_acceptance_client_preserves_http_error_status(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    def fake_urlopen(request: object, *, timeout: float) -> object:
        del timeout
        url = cast(Any, request).full_url
        raise HTTPError(
            url,
            status_code,
            "Gone",
            hdrs=Message(),
            fp=BytesIO(b'{"error":{"code":"run_not_found"}}'),
        )

    monkeypatch.setattr("agent_hub.harness.project_scale_runner.urlopen", fake_urlopen)
    client = UrllibAcceptanceClient(
        base_url="http://agent-hub.local",
        bearer_token="valid-token",
    )

    with pytest.raises(
        project_scale_runner_module.AcceptanceHTTPError
    ) as captured:
        client.request_json("GET", "/api/v1/runs/run-1/details")

    assert captured.value.status_code == status_code
    assert captured.value.method == "GET"
    assert captured.value.path == "/api/v1/runs/run-1/details"


def test_urllib_acceptance_client_retries_busy_acceptance_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None, bytes | None]] = []
    sleeps: list[float] = []

    class UrlopenRequest(Protocol):
        full_url: str
        data: bytes | None

        def get_header(self, header_name: str) -> str | None: ...

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return self.payload

    def fake_urlopen(request: object, *, timeout: float) -> Response:
        del timeout
        request = cast(UrlopenRequest, request)
        url = request.full_url
        auth = request.get_header("Authorization")
        data = request.data
        calls.append((url, auth, data))
        if url.endswith("/api/v1/auth/login") and len(calls) == 1:
            raise HTTPError(
                url,
                429,
                "Too Many Requests",
                hdrs=Message(),
                fp=BytesIO(
                    b'{"error":{"code":"authentication_busy","message":"authentication busy"}}'
                ),
            )
        if url.endswith("/api/v1/auth/login"):
            assert auth is None
            return Response(
                b'{"access_token":"fresh-token","token_type":"bearer",'
                b'"principal":{"user_id":"11111111-1111-4111-8111-111111111111",'
                b'"tenant_id":"22222222-2222-4222-8222-222222222222",'
                b'"role":"super_admin"}}'
            )
        return Response(b'{"ok":true}')

    monkeypatch.setattr("agent_hub.harness.project_scale_runner.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "agent_hub.harness.project_scale_runner.time.sleep",
        lambda delay: sleeps.append(delay),
    )
    client = UrllibAcceptanceClient(
        base_url="http://agent-hub.local",
        username="test",
        password="valid password",
    )

    result = client.request_json("GET", "/api/v1/runs/run-1/details")

    assert result == {"ok": True}
    assert [call[0].removeprefix("http://agent-hub.local") for call in calls] == [
        "/api/v1/auth/login",
        "/api/v1/auth/login",
        "/api/v1/runs/run-1/details",
    ]
    assert sleeps == [1.0]


def test_urllib_acceptance_client_reuses_same_origin_response_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cookies: list[str | None] = []

    class UrlopenRequest(Protocol):
        full_url: str

        def get_header(self, header_name: str) -> str | None: ...

    class Response:
        def __init__(self, payload: bytes, *, set_cookie: str | None = None) -> None:
            self.payload = payload
            self.headers = Message()
            if set_cookie is not None:
                self.headers.add_header("Set-Cookie", set_cookie)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return self.payload

    def fake_urlopen(request: object, *, timeout: float) -> Response:
        del timeout
        request = cast(UrlopenRequest, request)
        cookies.append(request.get_header("Cookie"))
        if request.full_url.endswith("/api/v1/web-previews/start"):
            return Response(
                b'{"id":"preview-1"}',
                set_cookie=(
                    "agent_preview_preview_1=secret; HttpOnly; SameSite=Strict; "
                    "Path=/api/v1/web-previews/preview-1/content"
                ),
            )
        return Response(b"<!doctype html><title>preview</title>")

    monkeypatch.setattr("agent_hub.harness.project_scale_runner.urlopen", fake_urlopen)
    client = UrllibAcceptanceClient(
        base_url="http://agent-hub.local",
        bearer_token="token",
    )

    client.request_json("POST", "/api/v1/web-previews/start", body={})
    content = client.request_bytes("GET", "/api/v1/web-previews/preview-1/content/")

    assert content.startswith(b"<!doctype html>")
    assert cookies == [None, "agent_preview_preview_1=secret"]


def test_execute_project_scale_plan_submits_run_and_collects_evidence() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(status="completed", artifacts=[{"id": "artifact-1"}])

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    assert report.case_count == 1
    result = report.results[0]
    assert result.case_id == "small:direct"
    assert result.run_id == "run-small-direct"
    assert result.observed_mode == "direct"
    assert result.evidence == {
        "run_details": True,
        "run_events": True,
        "terminal_status": True,
        "final_artifacts": True,
        "deliverable_quality": True,
        "agent_standard_verification": True,
        "discussion_trace": False,
        "multi_agent_participation": False,
        "plugin_contract": False,
        "deliverable_repair_trace": False,
        "self_repair_trace": False,
        "project_preflight_approval": False,
        "workspace_bundle": True,
        "cleanup_cancel": True,
    }
    workspace_bundle_path = (
        "/api/v1/workspaces/projects/project-scale-acceptance/"
        "sessions/project-scale-small-direct/bundle/download"
    )
    assert client.calls == [
        ("POST", "/api/v1/runs", "project-scale-small-direct-0"),
        ("GET", "/api/v1/runs/run-small-direct/details", None),
        ("GET", "/api/v1/runs/run-small-direct/events", None),
        ("GET", workspace_bundle_path, None),
    ]


@pytest.mark.parametrize(
    ("scale", "expected_mode"),
    (
        ("small", "dispatch"),
        ("medium", "dispatch"),
        ("large", "hybrid"),
        ("ultra", "hybrid"),
    ),
)
def test_execute_project_scale_plan_recovers_auto_run_waiting_for_user_mode(
    scale: str,
    expected_mode: str,
) -> None:
    plan = _auto_scale_plan(scale)
    client = WaitingUserModeAcceptanceClient(
        run_id=f"run-{scale}-auto",
        session_id=f"project-scale-{scale}-auto",
        token_source="submission",
    )

    result = execute_project_scale_plan(
        plan,
        client,
        wait_seconds=0.05,
        poll_interval_seconds=0,
    ).results[0]

    assert result.status == "completed"
    assert result.observed_mode == expected_mode
    assert client.choose_mode_bodies == [
        {
            "mode": expected_mode,
            "decision_token": client.decision_token,
            "version": 1,
        }
    ]
    assert not any("/api/v1/admin/runs" in path for _, path, _ in client.calls)


def test_execute_project_scale_plan_uses_waiting_details_mode_decision() -> None:
    plan = _auto_scale_plan("large")
    client = WaitingUserModeAcceptanceClient(
        run_id="run-large-auto-details-token",
        session_id="project-scale-large-auto",
        token_source="details",
    )

    result = execute_project_scale_plan(
        plan,
        client,
        wait_seconds=0.05,
        poll_interval_seconds=0,
    ).results[0]

    assert result.status == "completed"
    assert client.choose_mode_bodies == [
        {
            "mode": "hybrid",
            "decision_token": client.decision_token,
            "version": 2,
        }
    ]
    assert not any("/api/v1/admin/runs" in path for _, path, _ in client.calls)


def test_execute_project_scale_plan_publicly_approves_capability_once_after_mode_choice() -> None:
    plan = _auto_scale_plan("medium")
    client = WaitingModeThenCapabilityApprovalClient(
        run_id="run-medium-auto-capability",
        session_id="project-scale-medium-auto",
    )

    result = execute_project_scale_plan(
        plan,
        client,
        wait_seconds=0.05,
        poll_interval_seconds=0,
        auto_approve_capability_requests=True,
    ).results[0]

    assert result.status == "completed"
    assert result.observed_mode == "dispatch"
    assert result.evidence["capability_approval"] is True
    assert client.choose_mode_bodies == [
        {
            "mode": "dispatch",
            "decision_token": client.decision_token,
            "version": 1,
        }
    ]
    assert client.capability_approval_bodies == [
        {
            "approval_id": client.capability_approval_id,
            "version": client.capability_approval_version,
        }
    ]
    assert not any("/api/v1/admin/runs" in path for _, path, _ in client.calls)


def test_execute_project_scale_plan_retries_capability_approval_after_conflict() -> None:
    class ConflictThenApprovedClient(WaitingModeThenCapabilityApprovalClient):
        def __init__(self) -> None:
            super().__init__(
                run_id="run-medium-auto-capability-conflict",
                session_id="project-scale-medium-auto",
            )
            self.approval_attempts = 0
            self.statuses = ["waiting_approval", "waiting_approval", "completed"]

        def request_json(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object] | list[object]:
            if method == "POST" and path.endswith("/approve-capability"):
                self.approval_attempts += 1
                if self.approval_attempts == 1:
                    raise RuntimeError(
                        "POST /approve-capability failed status=409 "
                        "body=approval checkpoint is not ready"
                    )
            return super().request_json(
                method,
                path,
                body=body,
                idempotency_key=idempotency_key,
            )

    client = ConflictThenApprovedClient()

    result = execute_project_scale_plan(
        _auto_scale_plan("medium"),
        client,
        wait_seconds=0.05,
        poll_interval_seconds=0,
        auto_approve_capability_requests=True,
    ).results[0]

    assert result.status == "completed"
    assert result.evidence["capability_approval"] is True
    assert client.approval_attempts == 2


def test_execute_project_scale_plan_does_not_auto_approve_capability_without_opt_in() -> None:
    plan = _auto_scale_plan("medium")
    client = WaitingModeThenCapabilityApprovalClient(
        run_id="run-medium-auto-no-approval",
        session_id="project-scale-medium-auto",
    )

    execute_project_scale_plan(
        plan,
        client,
        wait_seconds=0,
        poll_interval_seconds=0,
    )

    assert client.capability_approval_bodies == []
    assert not any("/api/v1/admin/runs" in path for _, path, _ in client.calls)


@pytest.mark.parametrize("flow", ("direct", "dispatch", "hybrid", "multi_agent"))
def test_execute_project_scale_plan_does_not_choose_mode_for_explicit_flow(flow: str) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture",
        scales=("small",),
        flows=(flow,),
        execute=True,
    )
    client = FakeAcceptanceClient(
        run_id=f"run-small-{flow}",
        session_id=f"project-scale-small-{flow}",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
    )

    execute_project_scale_plan(plan, client)

    assert not any(path.endswith("/choose-mode") for _, path, _ in client.calls)


def test_execute_project_scale_plan_preserves_initial_mode_across_repair() -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture",
        scales=("small",),
        flows=("direct",),
        execute=True,
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        actual_mode="direct",
        repair_actual_mode="hybrid",
        deliverable_quality_sequence=(False, True),
    )

    result = execute_project_scale_plan(plan, client).results[0]

    assert result.run_id == "run-small-direct-repair"
    assert result.observed_mode == "direct"
    assert result.final_observed_mode == "hybrid"
    assert result.to_payload()["initial_observed_mode"] == "direct"


def test_execute_project_scale_plan_preserves_initial_route_evidence_across_repair() -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture",
        scales=("large",),
        flows=("direct",),
        execute=True,
    )
    client = FakeAcceptanceClient(
        run_id="run-large-direct",
        session_id="project-scale-large-direct",
        create_status="waiting_approval",
        decision_token="approve-large-route",
        decision_version=4,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        actual_mode="hybrid",
        repair_actual_mode="direct",
        deliverable_quality_sequence=(False, True),
        repair_create_status="waiting_approval",
        repair_decision_token="approve-large-route-repair",
        repair_decision_version=7,
        initial_route_evidence={
            "requested_mode": "direct",
            "effective_mode": "hybrid",
            "effective_scale": "large",
            "route_reason": "project_scale_mode_upgrade",
            "mode_source": "project_scale_assessment",
        },
        repair_route_evidence={
            "requested_mode": "direct",
            "effective_mode": "direct",
            "effective_scale": "large",
            "route_reason": "project_preflight_requires_user_approval",
            "mode_source": "project_preflight",
        },
    )

    result = execute_project_scale_plan(
        plan,
        client,
        wait_seconds=5,
        poll_interval_seconds=0,
    ).results[0]
    payload = result.to_payload()

    assert result.run_id == "run-large-direct-repair"
    assert result.route_reason == "project_scale_mode_upgrade"
    assert result.mode_source == "project_scale_assessment"
    assert result.effective_scale == "large"
    assert payload["final_route_reason"] == "project_preflight_requires_user_approval"
    assert payload["final_mode_source"] == "project_preflight"
    assert payload["final_effective_scale"] == "large"


@pytest.mark.parametrize("initial_scale", (None, "large"))
@pytest.mark.parametrize("repair_path", ("self_repair", "deliverable_repair"))
@pytest.mark.parametrize(
    ("response_evidence", "detail_evidence", "expected_scale"),
    (
        ({}, {}, None),
        ({"effective_scale": "small"}, {}, "small"),
        ({}, {"effective_scale": "small"}, "small"),
        ({"effective_scale": "large"}, {}, "large"),
        ({}, {"effective_scale": "large"}, "large"),
        ({"effective_scale": "large"}, {"effective_scale": "small"}, "small"),
        ({"effective_scale": "small"}, {"effective_scale": "large"}, "large"),
        ({"effective_scale": ""}, {"effective_scale": "unknown"}, None),
        ({"effective_scale": None}, {"effective_scale": None}, None),
    ),
    ids=(
        "absent", "response-downgrade", "detail-downgrade", "response-same", "detail-same",
        "detail-overrides-downgrade", "detail-overrides-upgrade", "invalid", "null",
    ),
)
def test_execute_project_scale_plan_completion_scale_uses_current_repair_evidence(
    initial_scale: str | None,
    repair_path: str,
    response_evidence: dict[str, object],
    detail_evidence: dict[str, object],
    expected_scale: str | None,
) -> None:
    class RepairScaleClient(FakeAcceptanceClient):
        def request_json(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object] | list[object]:
            response = super().request_json(
                method, path, body=body, idempotency_key=idempotency_key
            )
            if isinstance(response, dict) and response.get("id") == self.repair_run_id:
                response["effective_mode"] = "direct"
                response.update(
                    detail_evidence if path.endswith("/details") else response_evidence
                )
            return response

    plan = build_project_scale_run_plan(
        benchmark_kind="fixture", scales=("large",), flows=("direct",), execute=True
    )
    self_repair = repair_path == "self_repair"
    client = RepairScaleClient(
        run_id="run-large-direct",
        session_id="project-scale-large-direct",
        create_status="waiting_approval",
        decision_token="approve-large-scale",
        decision_version=1,
        repair_create_status="completed",
        statuses=("failed", "completed") if self_repair else ("completed",),
        artifacts=[{"id": "artifact-1"}],
        deliverable_quality_sequence=(True,) if self_repair else (False, True),
        self_repair_decision_token="repair-scale-token" if self_repair else None,
        self_repair_decision_version=1 if self_repair else None,
        initial_route_evidence={"effective_mode": "direct", "effective_scale": initial_scale},
    )

    result = execute_project_scale_plan(
        plan, client, wait_seconds=5, poll_interval_seconds=0
    ).results[0]

    assert result.ok, result.errors
    assert result.run_id == "run-large-direct-repair"
    assert result.repair_attempted is True
    assert result.effective_scale == initial_scale
    assert result.final_effective_scale == expected_scale
    assert result.completion_scale == expected_scale
    assert result.to_payload()["effective_scale"] == initial_scale
    assert result.to_payload()["final_effective_scale"] == expected_scale


@pytest.mark.parametrize("first_repair_path", ("self_repair", "deliverable_repair"))
def test_execute_project_scale_plan_completion_scale_forgets_previous_repair(
    first_repair_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MultipleRepairScaleClient(FakeAcceptanceClient):
        repair_count = 0

        def request_json(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object] | list[object]:
            if method == "POST" and (
                path.endswith("/accept-repair")
                or (path == "/api/v1/runs" and "deliverable-repair" in (idempotency_key or ""))
            ):
                self.repair_count += 1
                self.repair_run_id = f"{self.run_id}-repair-{self.repair_count}"
                self.repair_route_evidence = {"effective_mode": "direct"}
                if self.repair_count == 1:
                    self.repair_route_evidence["effective_scale"] = "medium"
            response = super().request_json(
                method, path, body=body, idempotency_key=idempotency_key
            )
            if isinstance(response, dict) and path.endswith("/accept-repair"):
                response.update(self.repair_route_evidence)
            return response

    monkeypatch.setattr(project_scale_runner_module, "_FIXTURE_DELIVERABLE_REPAIR_ATTEMPTS", 2)
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture", scales=("large",), flows=("direct",), execute=True
    )
    self_repair = first_repair_path == "self_repair"
    client = MultipleRepairScaleClient(
        run_id="run-large-direct",
        session_id="project-scale-large-direct",
        create_status="waiting_approval",
        decision_token="approve-large-scale",
        decision_version=1,
        repair_create_status="completed",
        statuses=("failed", "completed") if self_repair else ("completed",),
        artifacts=[{"id": "artifact-1"}],
        deliverable_quality_sequence=(True, False, True) if self_repair else (False, False, True),
        self_repair_decision_token="repair-scale-token" if self_repair else None,
        self_repair_decision_version=1 if self_repair else None,
        initial_route_evidence={"effective_mode": "direct", "effective_scale": "large"},
    )

    result = execute_project_scale_plan(
        plan, client, wait_seconds=5, poll_interval_seconds=0
    ).results[0]

    assert result.ok, result.errors
    assert client.repair_count == 2
    assert result.run_id == "run-large-direct-repair-2"
    assert result.repair_attempted is True
    assert result.effective_scale == "large"
    assert result.final_effective_scale is None
    assert result.completion_scale is None
    assert result.to_payload()["final_effective_scale"] is None


@pytest.mark.parametrize("repair_trace", (None, "self_repair_trace", "deliverable_repair_trace"))
@pytest.mark.parametrize("final_scale", (None, "small", "large", "", "unknown"))
def test_project_scale_completion_scale_preserves_unknown_and_invalid_evidence(
    repair_trace: str | None,
    final_scale: str | None,
) -> None:
    result = ProjectScaleCaseResult(
        case_id="large:direct",
        run_id="run-large-direct",
        status="completed",
        evidence={} if repair_trace is None else {repair_trace: True},
        effective_scale="large",
        final_effective_scale=final_scale,
    )
    expected_scale = "large" if final_scale is None and repair_trace is None else final_scale

    assert result.to_payload()["final_effective_scale"] == expected_scale
    assert result.completion_scale == expected_scale


def test_execute_auto_scale_repair_submits_the_observed_mode() -> None:
    plan = _auto_scale_plan("small")
    client = FakeAcceptanceClient(
        run_id="run-small-auto",
        session_id="project-scale-small-auto",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        actual_mode="direct",
        deliverable_quality_sequence=(False, True),
    )

    execute_project_scale_plan(plan, client)

    assert client.submitted_bodies[0]["mode"] == "auto"
    assert client.submitted_bodies[1]["mode"] == "direct"


def test_multi_agent_participation_requires_distinct_agents_with_events() -> None:
    participants, event_kinds = _multi_agent_participation(
        [
            {"kind": "agent.started", "agent_id": "architect"},
            {"kind": "agent.tool.completed", "agent_id": "architect"},
            {"kind": "agent.completed", "payload": {"agent_id": "reviewer"}},
        ]
    )

    assert participants == ("architect", "reviewer")
    assert event_kinds == ("agent.started", "agent.tool.completed", "agent.completed")

    duplicate_participants, _ = _multi_agent_participation(
        [
            {"kind": "agent.started", "agent_id": "Architect"},
            {"kind": "agent.completed", "agent_id": "architect"},
        ]
    )
    assert duplicate_participants == ("architect",)

    chat_roles, chat_events = _multi_agent_participation(
        [
            {"kind": "message.created", "role": "user"},
            {"kind": "message.created", "role": "assistant"},
        ]
    )
    assert chat_roles == ()
    assert chat_events == ()
    unrelated_participants, unrelated_events = _multi_agent_participation(
        [
            {
                "kind": "artifact.created",
                "participants": ["architect", "reviewer"],
                "payload": {"agent_id": "writer"},
            }
        ]
    )
    assert unrelated_participants == ()
    assert unrelated_events == ()

    crew_participants, crew_events = _multi_agent_participation(
        [
            {
                "kind": "step.started",
                "actor": "architect",
                "payload": {"agent_id": "architect", "role": "Architect"},
            },
            {
                "kind": "model.started",
                "actor": "reviewer",
                "payload": {"agent_id": "reviewer", "role": "Reviewer"},
            },
            {
                "kind": "step.completed",
                "actor": "architect",
                "payload": {"agent_id": "architect", "role": "Architect"},
            },
        ]
    )
    assert crew_participants == ("architect", "reviewer")
    assert crew_events == ("step.started", "model.started", "step.completed")

    role_is_not_identity, role_event_kinds = _multi_agent_participation(
        [
            {
                "kind": "step.started",
                "actor": "agent-1",
                "payload": {"agent_id": "agent-1", "role": "Architect"},
            },
            {
                "kind": "step.completed",
                "actor": "agent-1",
                "payload": {"agent_id": "agent-1", "role": "Tester"},
            },
        ]
    )
    assert role_is_not_identity == ("agent_1",)
    assert role_event_kinds == ("step.started", "step.completed")

    crew_actor_participants, crew_actor_events = _multi_agent_participation(
        [
            {"kind": "step.started", "actor": "architect"},
            {"kind": "step.completed", "actor": "tester"},
        ]
    )
    assert crew_actor_participants == ("architect", "tester")
    assert crew_actor_events == ("step.started", "step.completed")


def _real_crew_multi_agent_events() -> list[dict[str, object]]:
    return [
        {
            "kind": "step.started",
            "step_id": "architecture_step",
            "actor": "architect",
            "inputs": [],
            "payload": {"role": "Architect", "depends_on": ()},
        },
        {
            "kind": "step.completed",
            "step_id": "architecture_step",
            "actor": "architect",
            "payload": {"role": "Architect", "artifact_id": "artifact-architecture"},
        },
        {
            "kind": "step.started",
            "step_id": "implementation_step",
            "actor": "implementer",
            "inputs": [{"id": "artifact-architecture"}],
            "payload": {"role": "Implementer", "depends_on": ("architecture_step",)},
        },
        {
            "kind": "step.completed",
            "step_id": "implementation_step",
            "actor": "implementer",
            "payload": {"role": "Implementer", "artifact_id": "artifact-implementation"},
        },
        {
            "kind": "step.started",
            "step_id": "test_step",
            "actor": "tester",
            "inputs": [{"id": "artifact-implementation"}],
            "payload": {"role": "Tester", "depends_on": ("implementation_step",)},
        },
        {
            "kind": "step.completed",
            "step_id": "test_step",
            "actor": "tester",
            "payload": {"role": "Tester", "artifact_id": "artifact-test"},
        },
        {
            "kind": "step.started",
            "step_id": "synthesis_step",
            "actor": "final_synthesizer",
            "inputs": [
                {"id": "artifact-architecture"},
                {"id": "artifact-implementation"},
                {"id": "artifact-test"},
            ],
            "payload": {
                "role": "Final Synthesizer",
                "depends_on": (
                    "architecture_step",
                    "implementation_step",
                    "test_step",
                ),
            },
        },
        {
            "kind": "step.completed",
            "step_id": "synthesis_step",
            "actor": "final_synthesizer",
            "payload": {"role": "Final Synthesizer", "artifact_id": "artifact-final"},
        },
    ]


def test_multi_agent_contract_accepts_real_crew_event_chain() -> None:
    events = _real_crew_multi_agent_events()

    assert project_scale_runner_module._multi_agent_contract_reasons(events) == ()
    participants, event_kinds = _multi_agent_participation(events)
    assert participants == ("architect", "implementer", "synthesizer", "tester")
    assert event_kinds.count("step.started") == 4
    assert event_kinds.count("step.completed") == 4


def test_multi_agent_contract_rejects_missing_lifecycle_and_dependency_order() -> None:
    events = _real_crew_multi_agent_events()
    broken = [
        event
        for event in events
        if not (event.get("kind") == "step.completed" and event.get("actor") == "tester")
    ]
    synthesizer_start = next(
        event
        for event in broken
        if event.get("kind") == "step.started" and event.get("actor") == "final_synthesizer"
    )
    payload = dict(cast(Mapping[str, object], synthesizer_start["payload"]))
    payload["depends_on"] = ("implementation_step",)
    synthesizer_start["payload"] = payload

    reasons = project_scale_runner_module._multi_agent_contract_reasons(broken)

    assert any("tester" in reason and "completed" in reason for reason in reasons)
    assert any("synthesizer" in reason and "dependencies" in reason for reason in reasons)


def test_multi_agent_contract_requires_traceable_predecessor_artifacts() -> None:
    events = _real_crew_multi_agent_events()
    architect_completed = next(
        event
        for event in events
        if event.get("kind") == "step.completed" and event.get("actor") == "architect"
    )
    architect_completed["payload"] = {"role": "Architect"}
    synthesis_started = next(
        event
        for event in events
        if event.get("kind") == "step.started" and event.get("actor") == "final_synthesizer"
    )
    synthesis_started["inputs"] = [{"id": "artifact-implementation"}]

    reasons = project_scale_runner_module._multi_agent_contract_reasons(events)

    assert any("architect" in reason and "artifact" in reason for reason in reasons)
    assert any("synthesizer" in reason and "consume" in reason for reason in reasons)


@pytest.mark.parametrize(
    ("actor", "expected_predecessor"),
    (("implementer", "architect"), ("tester", "implementer")),
)
def test_multi_agent_contract_requires_each_intermediate_artifact_handoff(
    actor: str,
    expected_predecessor: str,
) -> None:
    events = _real_crew_multi_agent_events()
    started = next(
        event
        for event in events
        if event.get("kind") == "step.started" and event.get("actor") == actor
    )
    started["inputs"] = []

    reasons = project_scale_runner_module._multi_agent_contract_reasons(events)

    assert any(
        actor in reason and expected_predecessor in reason and "artifact" in reason
        for reason in reasons
    )


def test_public_route_evidence_proves_large_direct_upgrade_from_server_decision() -> None:
    evidence = project_scale_runner_module._public_route_evidence(
        {
            "mode": "hybrid",
            "status": "queued",
            "requested_mode": "direct",
            "effective_mode": "hybrid",
            "effective_scale": "large",
            "route_reason": "project_scale_mode_upgrade",
            "mode_source": "project_scale_assessment",
        },
        requested_body={
            "mode": "direct",
            "message": "Build a real large business project for flow=direct.",
        },
    )

    assert evidence == (
        "project_scale_mode_upgrade",
        "project_scale_assessment",
        "large",
        "direct",
    )


def test_public_route_evidence_rejects_request_text_without_server_decision() -> None:
    evidence = project_scale_runner_module._public_route_evidence(
        {"mode": "hybrid", "status": "queued"},
        requested_body={
            "mode": "direct",
            "message": "Build a real ultra business project for flow=direct.",
        },
    )

    assert evidence == (None, None, None, None)


def test_public_route_evidence_rejects_other_hybrid_reason_as_scale_upgrade() -> None:
    evidence = project_scale_runner_module._public_route_evidence(
        {
            "mode": "hybrid",
            "requested_mode": "direct",
            "effective_mode": "hybrid",
            "effective_scale": "large",
            "route_reason": "hermes_recommendation",
            "mode_source": "hermes",
        },
        requested_body={"mode": "direct", "message": "Build a large project."},
    )

    assert evidence == ("hermes_recommendation", "hermes", "large", "direct")


def test_public_route_evidence_rejects_unexposed_nested_routing_claims() -> None:
    evidence = project_scale_runner_module._public_route_evidence(
        {
            "mode": "hybrid",
            "routing_decision": {
                "reason": "project_scale_mode_upgrade",
                "mode_source": "project_scale_assessment",
                "project_scale": "large",
                "requested_mode": "direct",
            }
        },
        requested_body={"mode": "direct", "message": "Build a business project."},
    )

    assert evidence == (None, None, None, None)


def test_artifact_origin_requires_explicit_provenance_for_embedded_bundle() -> None:
    bundle = _project_bundle({"README.md": "# Current\n"})
    assert (
        project_scale_runner_module._artifact_origin(
            {"status": "completed"},
            [{"kind": "artifact.created", "payload": {"workspace_bundle": {}}}],
            bundle_source="embedded_bundle",
            workspace_bundle=bundle,
        )
        is None
    )


def test_artifact_origin_rejects_fixture_marker_even_with_claimed_real_origin() -> None:
    bundle = _project_bundle({"README.md": "# Fixture\n"})
    assert project_scale_runner_module._artifact_origin(
        None,
        [
            {
                "kind": "artifact.created",
                "payload": {
                    "artifact_origin": "model_workspace_bundle",
                    "producer": "project_scale_artifact_preseed",
                    "workspace_bundle": {"files": {"README.md": "# Fixture\n"}},
                },
            }
        ],
        bundle_source="embedded_bundle",
        workspace_bundle=bundle,
    ) == "builtin_fixture"


@pytest.mark.parametrize(
    "event",
    [
        {
            "kind": "tool.completed",
            "payload": {"result": {"artifact_origin": "tool_workspace_write"}},
        },
        {
            "kind": "artifact.created",
            "artifact": {"content": {"artifact_origin": "model_workspace_bundle"}},
        },
    ],
)
def test_artifact_origin_reads_real_nested_event_contracts(event: dict[str, object]) -> None:
    bundle = _project_bundle({"README.md": "# Current\n"})
    payload = dict(cast(Mapping[str, object], event.get("payload", {})))
    payload["workspace_bundle"] = {"files": {"README.md": "# Current\n"}}
    event["payload"] = payload
    assert project_scale_runner_module._artifact_origin(
        None,
        [event],
        bundle_source="embedded_bundle",
        workspace_bundle=bundle,
    ) in {"tool_workspace_write", "model_workspace_bundle"}


def test_artifact_origin_rejects_unrelated_provenance_event_for_public_bundle() -> None:
    bundle = _project_bundle({"README.md": "# Current\n"})

    assert (
        project_scale_runner_module._artifact_origin(
            None,
            [
                {
                    "kind": "tool.completed",
                    "payload": {
                        "artifact_id": "artifact-old",
                        "result": {"artifact_origin": "tool_workspace_write"},
                    },
                }
            ],
            bundle_source="public_workspace_api",
            workspace_bundle=bundle,
        )
        is None
    )


def test_artifact_origin_accepts_public_bundle_only_when_workspace_metadata_matches() -> None:
    content = b"# Current\n"
    bundle = _project_bundle({"README.md": content.decode("utf-8")})
    event: dict[str, object] = {
        "kind": "tool.completed",
        "tool_name": "workspace.bundle",
        "payload": {
            "artifact_id": "artifact-current",
            "result": {
                "artifact_origin": "incremental_workspace_delivery",
                "workspace_files": [
                    {
                        "path": "README.md",
                        "size_bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                ],
            },
        },
    }

    assert (
        project_scale_runner_module._artifact_origin(
            None,
            [event],
            bundle_source="public_workspace_api",
            workspace_bundle=bundle,
        )
        == "incremental_workspace_delivery"
    )

    mismatched = copy.deepcopy(event)
    payload = cast(dict[str, object], mismatched["payload"])
    result = cast(dict[str, object], payload["result"])
    result["workspace_files"] = [
        {"path": "README.md", "sha256": "0" * 64, "size_bytes": len(content)}
    ]
    assert (
        project_scale_runner_module._artifact_origin(
            None,
            [mismatched],
            bundle_source="public_workspace_api",
            workspace_bundle=bundle,
        )
        is None
    )


def test_run_submission_scope_requires_matching_conversation() -> None:
    with pytest.raises(
        RuntimeError,
        match=(
            "run scope mismatch: conversation_id expected conv-current "
            "got conv-stale"
        ),
    ):
        project_scale_runner_module._validate_run_submission_scope(
            {
                "project_id": "project-current",
                "workspace_session_id": "workspace-current",
                "conversation_id": "conv-stale",
            },
            {
                "project_id": "project-current",
                "workspace_session_id": "workspace-current",
                "conversation_id": "conv-current",
            },
        )


def test_multi_agent_participation_is_recomputed_from_successful_repair_run() -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture",
        scales=("small",),
        flows=("multi_agent",),
        execute=True,
    )
    client = FakeAcceptanceClient(
        run_id="run-small-multi-agent",
        session_id="project-scale-small-multi_agent",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[{"kind": "agent.started", "agent_id": "architect"}],
        repair_events=_real_crew_multi_agent_events(),
        deliverable_quality_sequence=(False, True),
    )

    result = execute_project_scale_plan(plan, client).results[0]

    assert result.evidence["multi_agent_participation"] is True
    assert result.participant_agent_ids == (
        "architect",
        "implementer",
        "synthesizer",
        "tester",
    ), result.errors
    assert result.participant_event_count == 8


def test_capability_multi_agent_repair_requires_real_collaboration_evidence() -> None:
    repaired = _deliverable_repair_body(
        {
            "message": "Build the requested project",
            "mode": "dispatch",
        },
        "small:multi_agent",
        benchmark_kind="capability",
        failed_reasons=(
            "discussion_trace: missing hybrid/discussion process evidence",
            "multi_agent_participation: fewer than two distinct agents",
        ),
    )

    message = str(repaired["message"])
    assert "Architecture Agent" in message
    assert "Implementation Agent" in message
    assert "Test Agent" in message
    assert "Synthesis Agent" in message
    assert "discussion_trace" in message
    assert "normalized agent_id values architect, implementer, tester, and synthesizer" in message


def test_execute_project_scale_plan_records_multi_agent_participation_evidence() -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture",
        scales=("small",),
        flows=("multi_agent",),
        execute=True,
    )
    client = FakeAcceptanceClient(
        run_id="run-small-multi-agent",
        session_id="project-scale-small-multi_agent",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=_real_crew_multi_agent_events(),
    )

    result = execute_project_scale_plan(plan, client).results[0]

    assert result.ok is True
    assert result.evidence["multi_agent_participation"] is True
    assert result.participant_agent_ids == (
        "architect",
        "implementer",
        "synthesizer",
        "tester",
    )
    assert result.participant_event_count == 8
    assert "multi_agent_participation" in result.required_evidence


def test_execute_project_scale_plan_accepts_production_events_envelope_and_artifact_ids() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[],
        artifact_ids=["artifact-1"],
        events_envelope=True,
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["run_events"] is True
    assert result.evidence["final_artifacts"] is True


def test_execute_project_scale_plan_reports_case_validation_focus() -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture",
        scales=("small",),
        flows=("capability_validation",),
        execute=True,
    )
    client = FakeAcceptanceClient(
        run_id="run-small-capability-validation",
        session_id="project-scale-small-capability_validation",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
    )

    report = execute_project_scale_plan(plan, client)

    results = cast(list[dict[str, object]], report.to_payload()["results"])

    assert results[0]["validation_focus"] == [
        "interaction_stability",
        "final_result",
        "deliverable_quality",
        "agent_standard_verification",
        "capability_matrix",
        "mode_control",
        "no_silent_downgrade",
    ]


def test_execute_project_scale_plan_reports_cancelled_status_after_cleanup() -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture",
        scales=("small",),
        flows=("direct",),
        execute=True,
    )
    client = FakeAcceptanceClient(
        status="running",
        artifacts=[{"id": "artifact-1"}],
        events=[{"kind": "run.created"}, {"kind": "artifact.created"}],
    )

    report = execute_project_scale_plan(plan, client, wait_seconds=0)

    result = report.results[0]
    assert result.status == "cancelled"
    assert result.evidence["cleanup_cancel"] is True
    assert "terminal_status: running" not in result.errors


def test_execute_project_scale_plan_refreshes_terminal_status_after_generated_project_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture",
        scales=("small",),
        flows=("direct",),
        execute=True,
    )
    validation_calls: list[bytes | None] = []

    def validate(bundle: bytes | None, **_kwargs: object) -> object:
        validation_calls.append(bundle)
        return project_scale_runner_module._EvidenceCheck(passed=True, reasons=())

    monkeypatch.setattr(project_scale_runner_module, "_validate_generated_project_bundle", validate)
    client = FakeAcceptanceClient(
        status="running",
        statuses=("running", "completed"),
        artifacts=[{"id": "artifact-1"}],
        events=[{"kind": "run.created"}, {"kind": "artifact.created"}],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0; node --check completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps({"scripts": {"build": "node --check src/main.js"}}),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
            }
        ),
    )

    report = execute_project_scale_plan(
        plan,
        client,
        wait_seconds=0,
        validate_generated_project=True,
    )

    result = report.results[0]
    assert validation_calls
    assert result.status == "completed"
    assert result.evidence["terminal_status"] is True
    assert result.errors == ()


def test_execute_project_scale_plan_requires_plugin_contract_evidence() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("plugin",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-small-plugin",
        session_id="project-scale-small-plugin",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        plugin_contract=False,
        plugin_contract_sequence=(False, True),
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert report.ok is True
    assert result.run_id == "run-small-plugin-repair"
    assert result.evidence["plugin_contract"] is True
    assert result.evidence["deliverable_repair_trace"] is True
    repair_message = str(client.submitted_bodies[1]["message"])
    assert "plugin_contract: missing or incomplete plugin capability contract evidence" in repair_message
    assert "adapter contracts" in repair_message
    assert "sandbox and policy boundaries" in repair_message


def test_deliverable_repair_body_keeps_dispatch_task_bounded_for_plugin_flow() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("plugin",), execute=True)
    body = dict(plan.requests[0].body)
    body["message"] = f"{body['message']}\n" + ("original context " * 220)

    repair_body = _deliverable_repair_body(
        body,
        "small:plugin",
        failed_reasons=(
            "plugin_contract: missing or incomplete plugin capability contract evidence",
            "discussion_trace: missing hybrid/discussion process evidence",
        ),
        benchmark_kind="fixture",
    )

    message = repair_body["message"]
    assert isinstance(message, str)
    assert message == message.strip()
    assert len(message) <= 2_000
    RolePlanningRequest(task=message, mode=TaskMode.DISPATCH)


def test_python_verification_report_counts_split_build_and_unittest_success() -> None:
    verification_text = """
    ## Reproducible build
    bash scripts/build.sh
    build: ok
    The script byte-compiles src, tests, and scripts with python -m compileall -q.

    ## Reproducible tests
    bash scripts/test.sh
    Ran 15 tests
    OK
    """

    assert _bundle_has_build_test_execution_evidence(verification_text) is True


def test_planned_interaction_smoke_does_not_count_as_execution_success() -> None:
    verification_text = """
    - npm run build
    - npm test
    - interaction smoke planned
    """

    assert _bundle_has_build_test_execution_evidence(verification_text) is False


def test_simple_passed_lines_do_not_count_as_reproducible_execution_evidence() -> None:
    verification_text = """
    - npm run build: passed
    - npm test: passed
    """

    assert _bundle_has_build_test_execution_evidence(verification_text) is False


def test_execute_project_scale_plan_fails_plugin_flow_without_contract_after_repair() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("plugin",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-small-plugin",
        session_id="project-scale-small-plugin",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        plugin_contract=False,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert report.ok is False
    assert result.evidence["plugin_contract"] is False
    assert "plugin_contract: missing or incomplete plugin capability contract evidence" in result.errors
    assert "plugin_contract" in result.missing_evidence


def test_execute_project_scale_plan_rejects_silent_mode_downgrade() -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture",
        scales=("small",),
        flows=("capability_validation",),
        execute=True,
    )
    client = FakeAcceptanceClient(
        run_id="run-small-capability-validation",
        session_id="project-scale-small-capability_validation",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        actual_mode="direct",
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert report.results[0].errors == ("mode_control: requested hybrid got direct",)


def test_project_scale_execution_report_summarizes_failed_evidence_and_focus() -> None:
    report = ProjectScaleExecutionReport(
        benchmark_kind="fixture",
        results=(
            ProjectScaleCaseResult(
                case_id="medium:artifact_production",
                run_id="run-medium-artifact",
                status="completed",
                evidence={
                    "run_details": True,
                    "run_events": True,
                    "terminal_status": True,
                    "final_artifacts": False,
                    "deliverable_quality": False,
                    "agent_standard_verification": False,
                    "workspace_bundle": True,
                    "cleanup_cancel": True,
                },
                validation_focus=("interaction_stability", "final_result", "artifact_integrity"),
            ),
            ProjectScaleCaseResult(
                case_id="ultra:self_repair",
                run_id="run-ultra-self-repair",
                status="failed",
                evidence={
                    "run_details": True,
                    "run_events": True,
                    "terminal_status": True,
                    "project_preflight_approval": True,
                    "workspace_bundle": False,
                    "deliverable_quality": False,
                    "agent_standard_verification": False,
                    "cleanup_cancel": True,
                },
                validation_focus=(
                    "interaction_stability",
                    "final_result",
                    "long_running_control",
                    "project_preflight",
                    "fault_injection",
                    "self_repair",
                ),
                errors=("terminal_status: failed",),
            ),
        )
    )

    payload = report.to_payload()

    assert payload["failed_case_count"] == 2
    assert payload["failed_cases"] == ["medium:artifact_production", "ultra:self_repair"]
    assert payload["missing_evidence_summary"] == {
        "final_artifacts": 2,
        "deliverable_quality": 2,
        "agent_standard_verification": 2,
        "discussion_trace": 2,
        "workspace_bundle": 1,
        "self_repair_trace": 1,
    }
    assert payload["failed_validation_focus"] == [
        "interaction_stability",
        "final_result",
        "artifact_integrity",
        "long_running_control",
        "project_preflight",
        "fault_injection",
        "self_repair",
    ]


def test_execute_project_scale_plan_can_scope_idempotency_to_execution_id() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        session_id="project-scale-small-direct-acceptance-20260914",
    )

    report = execute_project_scale_plan(plan, client, execution_id="acceptance-20260914")

    assert report.ok is True
    assert client.submitted_bodies[0]["workspace_session_id"] == (
        "project-scale-small-direct-acceptance-20260914"
    )
    assert client.calls[0] == (
        "POST",
        "/api/v1/runs",
        "project-scale-small-direct-0-acceptance-20260914",
    )


def test_execute_project_scale_plan_keeps_scoped_workspace_session_safe() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("dispatch",), execute=True)
    expected_session = "project-scale-small-dispatch-8775fe0-small-dispatch-auth-1790174"
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        session_id=expected_session,
    )

    report = execute_project_scale_plan(
        plan,
        client,
        execution_id="8775fe0-small-dispatch-auth-1790174660",
    )

    submitted_session = client.submitted_bodies[0]["workspace_session_id"]
    assert report.ok is True
    assert isinstance(submitted_session, str)
    assert submitted_session == expected_session
    assert len(submitted_session) <= 64


def test_project_scale_repair_attempted_counts_self_repair_trace() -> None:
    result = ProjectScaleCaseResult(
        case_id="small:self_repair",
        run_id="run-small-self-repair",
        status="completed",
        evidence={
            "run_details": True,
            "run_events": True,
            "terminal_status": True,
            "final_artifacts": True,
            "deliverable_quality": True,
            "agent_standard_verification": True,
            "discussion_trace": True,
            "project_preflight_approval": False,
            "self_repair_trace": True,
            "plugin_contract": False,
            "workspace_bundle": True,
            "cleanup_cancel": True,
            "deliverable_repair_trace": False,
        },
        validation_focus=("fault_injection", "self_repair"),
    )

    assert result.repair_attempted is True
    assert result.to_payload()["repair_attempted"] is True


def test_execute_project_scale_plan_repairs_failed_deliverable_quality() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("medium",), flows=("artifact_production",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-medium-artifact",
        session_id="project-scale-medium-artifact_production",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        deliverable_quality_sequence=(False, True),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    assert report.results[0].run_id == "run-medium-artifact-repair"
    assert report.results[0].evidence["final_artifacts"] is True
    assert report.results[0].evidence["workspace_bundle"] is True
    assert report.results[0].evidence["deliverable_quality"] is True
    assert report.results[0].evidence["agent_standard_verification"] is True
    assert report.results[0].evidence["deliverable_repair_trace"] is True
    assert report.results[0].missing_evidence == ()
    assert (
        "POST",
        "/api/v1/runs",
        "project-scale-medium-artifact-production-0-deliverable-repair",
    ) in client.calls


def test_execute_project_scale_plan_reports_repair_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture",
        scales=("medium",),
        flows=("artifact_production",),
        execute=True,
    )
    progress: list[str] = []
    validation_results = [
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=("generated_project_validation: command failed exit=1",),
        ),
        project_scale_runner_module._EvidenceCheck(passed=True, reasons=()),
    ]

    def validate_generated_project_bundle(
        bundle: bytes | None, **kwargs: object
    ) -> project_scale_runner_module._EvidenceCheck:
        assert bundle is not None
        return validation_results.pop(0)

    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        validate_generated_project_bundle,
    )
    client = FakeAcceptanceClient(
        run_id="run-medium-artifact",
        session_id="project-scale-medium-artifact_production",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0\n"
                    "- npm test: passed exit 0\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps({"scripts": {"build": "node --check src/main.js"}}),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
            }
        ),
    )

    report = execute_project_scale_plan(
        plan,
        client,
        validate_generated_project=True,
        progress=progress.append,
    )

    assert report.ok is True
    assert progress == [
        "case 1/1 medium:artifact_production: submitting run",
        "case 1/1 medium:artifact_production: observing run run-medium-artifact",
        "case 1/1 medium:artifact_production: validating deliverable",
        "case 1/1 medium:artifact_production: submitting deliverable repair 1",
        "case 1/1 medium:artifact_production: observing repair run run-medium-artifact-repair",
        "case 1/1 medium:artifact_production: validating repaired deliverable 1",
        "case 1/1 medium:artifact_production: completed status=completed ok=true",
    ]


def test_execute_project_scale_plan_repairs_missing_agent_standard_verification() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("medium",), flows=("artifact_production",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-medium-artifact",
        session_id="project-scale-medium-artifact_production",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        agent_standard_sequence=(False, True),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    assert report.results[0].run_id == "run-medium-artifact-repair"
    assert report.results[0].evidence["deliverable_quality"] is True
    assert report.results[0].evidence["agent_standard_verification"] is True
    assert report.results[0].evidence["deliverable_repair_trace"] is True
    assert report.results[0].missing_evidence == ()
    assert len(client.submitted_bodies) == 2
    repair_message = str(client.submitted_bodies[1]["message"])
    assert "agent_standard_verification" in repair_message
    assert "plan_before_implementation" in repair_message
    assert "constraints_reading_evidence.json" in repair_message
    assert "root_cause_repair" in repair_message


def test_execute_project_scale_plan_repairs_missing_build_test_execution_evidence() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("medium",), flows=("artifact_production",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-medium-artifact",
        session_id="project-scale-medium-artifact_production",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        execution_evidence_sequence=(False, True),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    assert report.results[0].run_id == "run-medium-artifact-repair"
    assert report.results[0].evidence["deliverable_quality"] is True
    assert report.results[0].evidence["deliverable_repair_trace"] is True
    repair_message = str(client.submitted_bodies[1]["message"])
    assert "workspace_bundle: missing build/test execution evidence" in repair_message
    assert "rerun build/test/interaction checks" in repair_message


def test_execute_project_scale_plan_repairs_missing_hybrid_discussion_trace() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("hybrid",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-small-hybrid",
        session_id="project-scale-small-hybrid",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        discussion_trace_sequence=(False, True),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    assert report.results[0].run_id == "run-small-hybrid-repair"
    assert report.results[0].evidence["discussion_trace"] is True
    assert report.results[0].evidence["deliverable_repair_trace"] is True
    repair_message = str(client.submitted_bodies[1]["message"])
    assert "discussion_trace: missing hybrid/discussion process evidence" in repair_message
    assert "record discussion_trace" in repair_message


def test_execute_project_scale_plan_explains_quality_and_standard_repair_reasons() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("medium",), flows=("artifact_production",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-medium-artifact",
        session_id="project-scale-medium-artifact_production",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        deliverable_quality_sequence=(False, True),
        agent_standard_sequence=(False, True),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    repair_message = str(client.submitted_bodies[1]["message"])
    assert "Previous failed evidence:" in repair_message
    assert "deliverable_quality: missing or incomplete structured quality flags" in repair_message
    assert "workspace_bundle: contains placeholder or stub markers" in repair_message
    assert "agent_standard_verification: missing or incomplete Codex/Claude standard flags" in repair_message
    assert "workspace_bundle: missing implementation plan artifact" in repair_message
    assert "workspace_bundle: missing verification report artifact" in repair_message


def test_execute_project_scale_plan_reports_failed_deliverable_repair_outcome() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("medium",), flows=("artifact_production",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-medium-artifact",
        session_id="project-scale-medium-artifact_production",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        deliverable_quality_sequence=(False, False),
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    payload = result.to_payload()
    assert report.ok is False
    assert result.run_id == "run-medium-artifact-repair"
    assert result.evidence["deliverable_repair_trace"] is True
    assert payload["repair_attempted"] is True
    assert payload["repair_outcome"] == "failed"
    assert "deliverable_quality: missing or incomplete structured quality flags" in result.errors
    assert format_project_scale_result_line(result) == (
        "medium:artifact_production run_id=run-medium-artifact-repair ok=false "
        "focus=interaction_stability,final_result,deliverable_quality,"
        "agent_standard_verification,artifact_integrity "
        "missing=deliverable_quality errors=7 repair=failed"
    )


def test_execute_project_scale_plan_rejects_scope_mismatch_from_replayed_run() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        response_project_id="project-scale-acceptance",
        response_session_id="project-scale-stale-direct",
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert report.results[0].run_id == "run-small-direct"
    assert report.results[0].errors == (
        (
            "run scope mismatch: workspace_session_id expected project-scale-small-direct "
            "got project-scale-stale-direct"
        ),
    )
    assert ("POST", "/api/v1/runs/run-small-direct/cancel", None) in client.calls


def test_execute_project_scale_plan_rejects_detail_scope_mismatch() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        details_run_id="run-stale-direct",
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert report.results[0].run_id == "run-small-direct"
    assert report.results[0].errors == (
        "run details scope mismatch: id expected run-small-direct got run-stale-direct",
    )


def test_execute_project_scale_plan_requires_non_empty_event_stream() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(status="completed", artifacts=[{"id": "artifact-1"}], events=[])

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert report.results[0].evidence["run_events"] is False
    assert report.results[0].errors == ("run_events: empty event stream",)


def test_execute_project_scale_plan_rejects_event_scope_mismatch_when_present() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[{"kind": "run.created", "run_id": "run-stale-direct"}],
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert report.results[0].run_id == "run-small-direct"
    assert report.results[0].errors == (
        "run events scope mismatch: run_id expected run-small-direct got run-stale-direct",
    )


def test_execute_project_scale_plan_records_case_failure_and_continues_cleanup() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(fail_bundle=True)

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert report.results[0].run_id == "run-small-direct"
    assert report.results[0].evidence["run_details"] is True
    assert report.results[0].evidence["run_events"] is True
    assert report.results[0].evidence["terminal_status"] is False
    assert report.results[0].evidence["workspace_bundle"] is False
    assert report.results[0].evidence["cleanup_cancel"] is True
    assert report.results[0].errors == ("workspace_bundle: workspace bundle unavailable",)


def test_execute_project_scale_plan_attempts_repair_when_workspace_bundle_is_missing() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert len(client.submitted_bodies) == 2
    assert report.results[0].evidence["deliverable_repair_trace"] is True
    assert "workspace_bundle: workspace bundle unavailable" in report.results[0].errors


def test_execute_project_scale_plan_drops_stale_workspace_bundle_error_after_repair() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        fail_bundle_once=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert report.ok is True
    assert result.run_id == "run-small-direct-repair"
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_repair_trace"] is True
    assert result.errors == ()


def test_drop_recovered_workspace_bundle_errors_keeps_quality_failures() -> None:
    errors = [
        "workspace_bundle: GET /api/v1/workspaces/projects/p/sessions/s/bundle/download failed status=404",
        "workspace_bundle: workspace bundle unavailable",
        "workspace_bundle: missing source files",
        "generated_project_validation: command failed exit=1 command=npm test",
    ]

    _drop_recovered_workspace_bundle_errors(errors)

    assert errors == [
        "workspace_bundle: missing source files",
        "generated_project_validation: command failed exit=1 command=npm test",
    ]


def test_direct_deliverable_repair_prompt_requires_embedded_bundle() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
    )

    execute_project_scale_plan(plan, client)

    repair_message = str(client.submitted_bodies[1]["message"])
    assert "do not call tools" in repair_message
    assert "workspace_bundle.files" in repair_message
    assert "Markdown file blocks" in repair_message
    assert "credential-like terms" in repair_message
    assert "sk-" in repair_message


def test_deliverable_repair_prompt_includes_relevant_workspace_context() -> None:
    bundle = _project_bundle(
        {
            "package.json": json.dumps({"scripts": {"build": "tsc -p tsconfig.json"}}),
            "tsconfig.json": json.dumps({"compilerOptions": {"strict": True}}),
            "src/server.ts": "import type { CreateServerOptions } from './types';\nexport function start(options: CreateServerOptions) { return options.port; }\n",
            "src/types.ts": "export type Task = { id: string; title: string };\n",
            "README.md": "# Small task API\n",
        }
    )

    repair_body = _deliverable_repair_body(
        {
            "message": "Build a small task API.",
            "mode": "direct",
            "project_id": "project-1",
            "workspace_session_id": "session-1",
        },
        "small:self_repair",
        benchmark_kind="capability",
        source_workspace_bundle=bundle,
        failed_reasons=(
            (
                "generated_project_validation: command failed exit=2 command=npm run build "
                "output_tail=\"src/server.ts(8,15): error TS2305: Module './types' "
                "has no exported member 'CreateServerOptions'.\""
            ),
        ),
    )

    repair_message = str(repair_body["message"])
    assert repair_body["replace_workspace_files"] is False
    assert "Return only complete changed files" in repair_message
    assert "Current workspace context for precise repair" in repair_message
    assert "src/server.ts" in repair_message
    assert "src/types.ts" in repair_message
    assert "CreateServerOptions" in repair_message
    assert "TS2305 means the imported symbol must be exported" in repair_message
    assert "JSON files must use strict JSON syntax with double-quoted keys and strings" in repair_message


def test_deliverable_repair_prompt_preserves_runtime_constructor_exports() -> None:
    bundle = _project_bundle(
        {
            "package.json": json.dumps({"scripts": {"test": "vitest run"}}),
            "tests/helper.ts": (
                "import { Storage } from '../src/infrastructure/storage';\n"
                "export function makeStorage(path: string) { return new Storage(path); }\n"
            ),
            "src/infrastructure/storage.ts": (
                "export class PortfolioStorage {\n"
                "  constructor(readonly dataDir: string) {}\n"
                "}\n"
            ),
        }
    )

    repair_body = _deliverable_repair_body(
        {
            "message": "Build an enterprise portfolio API.",
            "mode": "direct",
            "project_id": "project-1",
            "workspace_session_id": "session-1",
        },
        "ultra:auto",
        benchmark_kind="capability",
        source_workspace_bundle=bundle,
        failed_reasons=(
            (
                "generated_project_validation: command failed exit=1 command=npm test "
                "output_tail=\"tests/helper.ts:18:17 TypeError: Storage is not a constructor\""
            ),
        ),
    )

    repair_message = str(repair_body["message"])
    assert "tests/helper.ts" in repair_message
    assert "src/infrastructure/storage.ts" in repair_message
    assert "PortfolioStorage" in repair_message
    assert "Runtime import/export repair hint" in repair_message
    assert "compatible named export" in repair_message


def test_execute_project_scale_plan_uses_embedded_workspace_bundle_artifact() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    embedded_bundle = {
        "workspace_bundle": {
            "files": {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0; vite build completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps(
                    {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                    sort_keys=True,
                ),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
            }
        }
    }
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "run_id": "run-small-direct",
                "artifact": {"content": {"text": json.dumps(embedded_bundle)}},
            }
        ],
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.evidence["agent_standard_verification"] is True
    assert result.errors == ()


def test_execute_project_scale_plan_reads_quality_flags_from_json_artifact() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    embedded_bundle = {
        "deliverable_quality": {
            "requirements_satisfied": True,
            "build_passed": True,
            "tests_passed": True,
            "interactive_checks_passed": True,
            "no_placeholders": True,
            "artifact_integrity": True,
        },
        "agent_standard_verification": {
            "constraints_read": True,
            "plan_before_implementation": True,
            "reproducible_verification": True,
            "root_cause_repair": True,
        },
        "workspace_bundle": {
            "files": {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION_REPORT.md": (
                    "- npm run build: passed exit 0; vite build completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps(
                    {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                    sort_keys=True,
                ),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
            }
        },
    }
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        deliverable_quality=False,
        agent_standard=False,
        events=[
            {
                "kind": "artifact.created",
                "run_id": "run-small-direct",
                "artifact": {"content": {"text": json.dumps(embedded_bundle)}},
            }
        ],
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.evidence["agent_standard_verification"] is True
    assert result.errors == ()


def test_execute_project_scale_plan_uses_markdown_file_bundle_artifact() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    markdown_bundle = """
# Direct Deliverable

## `README.md`

```markdown
# Acceptance Fixture

Implements the requested project scope.
```

## `PROJECT_REQUIREMENTS.md`

```markdown
- Requirement satisfied
- Interaction verified
```

### `IMPLEMENTATION_PLAN.md`

```markdown
__IMPLEMENTATION_PLAN__
```

### `VERIFICATION.md`

```markdown
- npm run build: passed exit 0; vite build completed
- npm test: passed exit 0; 1 test passed
- interaction smoke: passed
```

### `package.json`

```json
{"scripts":{"build":"node --check src/main.js","test":"node --test"}}
```

### `src/main.js`

```js
export function formatGreeting(name) {
  const value = String(name || '').trim();
  if (!value) return 'Hello, guest';
  return `Hello, ${value}`;
}
```

### `tests/main.test.js`

```js
import assert from 'node:assert/strict';
import { formatGreeting } from '../src/main.js';

assert.equal(formatGreeting(' Ada '), 'Hello, Ada');
assert.equal(formatGreeting(''), 'Hello, guest');
```
""".replace("__IMPLEMENTATION_PLAN__", _AGENT_STANDARD_IMPLEMENTATION_PLAN).strip()
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "run_id": "run-small-direct",
                "artifact": {"content": {"text": markdown_bundle}},
            }
        ],
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.evidence["agent_standard_verification"] is True
    assert result.errors == ()


def test_execute_project_scale_plan_recovers_workspace_bundle_from_downloaded_artifact() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    downloaded_bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "run_id": "run-small-direct",
                "payload": {
                    "artifact_id": "artifact-1",
                    "output": "### `README.md`\n\n```text\n# Acceptance Fixture\n...",
                },
            }
        ],
        artifact_downloads={"artifact-1": downloaded_bundle},
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.evidence["agent_standard_verification"] is True
    assert result.errors == ()
    assert (
        "GET",
        "/api/v1/runs/run-small-direct/artifacts/artifact-1/download",
        None,
    ) in client.calls
    assert [call for call in client.calls if call[0] == "POST" and call[1] == "/api/v1/runs"] == [
        ("POST", "/api/v1/runs", "project-scale-small-direct-0")
    ]


def test_execute_project_scale_plan_prefers_downloaded_bundle_over_redacted_event_bundle() -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True
    )
    downloaded_bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )
    redacted_event_bundle = {
        "files": {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": "[redacted]",
        }
    }
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "run_id": "run-small-direct",
                "payload": {
                    "artifact_id": "artifact-1",
                    "workspace_bundle": redacted_event_bundle,
                },
            }
        ],
        artifact_downloads={"artifact-1": downloaded_bundle},
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.evidence["agent_standard_verification"] is True
    assert result.errors == ()


def test_embedded_workspace_bundle_accepts_inline_fence_file_blocks() -> None:
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

    bundle = _embedded_workspace_bundle_from_text(text)

    assert bundle is not None
    with zipfile.ZipFile(BytesIO(bundle)) as archive:
        assert archive.read("package.json").decode("utf-8") == (
            '{"scripts":{"build":"node --check src/main.js","test":"node --test"}}\n'
        )
        assert "module.exports = { add };" in archive.read("src/main.js").decode("utf-8")


def test_embedded_workspace_bundle_accepts_plain_file_headings() -> None:
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

    bundle = _embedded_workspace_bundle_from_text(text)

    assert bundle is not None
    with zipfile.ZipFile(BytesIO(bundle)) as archive:
        assert archive.read("package.json").decode("utf-8") == (
            '{"scripts":{"build":"node --check src/main.js","test":"node --test"}}\n'
        )
        assert "module.exports = { add };" in archive.read("src/main.js").decode("utf-8")


def test_embedded_workspace_bundle_accepts_fenced_blocks_with_path_comments() -> None:
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

    bundle = _embedded_workspace_bundle_from_text(text)

    assert bundle is not None
    with zipfile.ZipFile(BytesIO(bundle)) as archive:
        assert archive.read("package.json").decode("utf-8") == (
            '{"scripts":{"build":"node --check src/main.js","test":"node --test"}}\n'
        )
        assert "module.exports = { add };" in archive.read("src/main.js").decode("utf-8")


def test_execute_project_scale_plan_recovers_workspace_bundle_from_admin_artifacts() -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture",
        scales=("small",),
        flows=("direct",),
        execute=True,
    )
    bundle_payload = {
        "workspace_bundle": {
            "files": {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0; vite build completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps(
                    {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                    sort_keys=True,
                ),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
            }
        }
    }
    artifact_text = json.dumps(bundle_payload, ensure_ascii=False)
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        admin_artifacts=[{"id": "artifact-1", "kind": "text", "text": artifact_text}],
        events=[
            {
                "kind": "artifact.created",
                "run_id": "run-small-direct",
                "payload": {
                    "artifact_id": "artifact-1",
                    "output": artifact_text[:240],
                },
            }
        ],
        artifact_downloads={},
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.evidence["agent_standard_verification"] is True
    assert result.errors == ()
    assert ("GET", "/api/v1/admin/runs/run-small-direct", None) in client.calls


def test_execute_project_scale_plan_reads_quality_from_markdown_metadata_file() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    metadata = {
        "deliverable_quality": {
            "requirements_satisfied": True,
            "build_passed": True,
            "tests_passed": True,
            "interactive_checks_passed": True,
            "no_placeholders": True,
            "artifact_integrity": True,
        },
        "agent_standard_verification": {
            "constraints_read": True,
            "plan_before_implementation": True,
            "reproducible_verification": True,
            "root_cause_repair": True,
        },
    }
    markdown_bundle = f"""
### `deliverable_metadata.json`

```json
{json.dumps(metadata, sort_keys=True)}
```

### `README.md`

```markdown
# Acceptance Fixture

Implements the requested project scope.
```

### `docs/implementation-plan.md`

```markdown
{_AGENT_STANDARD_IMPLEMENTATION_PLAN}
```

### `docs/verification-report.md`

```markdown
## Reproducible build evidence
npm run build
{{"build": "ok", "exit_code": 0, "tool": "compileall"}}

## Reproducible test evidence
npm test
Ran 1 test
OK
exit 0

npm run test:interaction
- build_passed: true
- tests_passed: true
- interactive_checks_passed: true
```

### `requirements.txt`

```text
# Standard library only.
```

### `direct_ledger/core.py`

```python
def format_greeting(name):
    value = str(name or "").strip()
    if not value:
        return "Hello, guest"
    return f"Hello, {{value}}"
```

### `tests/test_core.py`

```python
from direct_ledger.core import format_greeting

def test_format_greeting():
    assert format_greeting(" Ada ") == "Hello, Ada"
    assert format_greeting("") == "Hello, guest"
```

### `scripts/build.sh`

```bash
python -m compileall direct_ledger tests
```
""".strip()
    client = FakeAcceptanceClient(
        fail_bundle=True,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        deliverable_quality=False,
        agent_standard=False,
        events=[
            {
                "kind": "artifact.created",
                "run_id": "run-small-direct",
                "artifact": {"content": {"text": markdown_bundle}},
            }
        ],
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.evidence["agent_standard_verification"] is True
    assert result.errors == ()


def test_execute_project_scale_plan_requires_interaction_evidence_when_claimed() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    metadata = {
        "deliverable_quality": {
            "requirements_satisfied": True,
            "build_passed": True,
            "tests_passed": True,
            "interactive_checks_passed": True,
            "no_placeholders": True,
            "artifact_integrity": True,
        },
        "agent_standard_verification": {
            "constraints_read": True,
            "plan_before_implementation": True,
            "reproducible_verification": True,
            "root_cause_repair": True,
        },
    }
    shell_bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
            ),
            "deliverable_metadata.json": json.dumps(metadata, sort_keys=True),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        deliverable_quality=False,
        agent_standard=False,
        workspace_bundle=shell_bundle,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is False
    assert "workspace_bundle: missing interaction execution evidence" in result.errors


def test_execute_project_scale_plan_rejects_todo_dummy_project_markers() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    shell_bundle = _project_bundle(
        {
            "README.md": (
                "# Acceptance Fixture\n\n"
                "TODO: replace with real implementation after the demo.\n"
            ),
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": "export function runApp() { return 'dummy implementation'; }\n",
            "tests/main.test.js": (
                "import assert from 'node:assert/strict';\n"
                "import { runApp } from '../src/main.js';\n"
                "assert.equal(runApp(), 'ready');\n"
            ),
        }
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=shell_bundle,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is False
    assert "workspace_bundle: contains placeholder or stub markers" in result.errors


def test_execute_project_scale_plan_rejects_constant_only_source_bundle() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    shell_bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": "export const status = 'ready';\n",
            "tests/main.test.js": (
                "import assert from 'node:assert/strict';\n"
                "import { status } from '../src/main.js';\n"
                "assert.equal(status, 'ready');\n"
            ),
        }
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=shell_bundle,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is False
    assert "workspace_bundle: missing meaningful source implementation" in result.errors


def test_execute_project_scale_plan_accepts_small_functional_source_bundle() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    functional_bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 2 tests passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": (
                "export function formatGreeting(name) {\n"
                "  const value = String(name || '').trim();\n"
                "  if (!value) return 'Hello, guest';\n"
                "  return `Hello, ${value}`;\n"
                "}\n"
            ),
            "tests/main.test.js": (
                "import assert from 'node:assert/strict';\n"
                "import { formatGreeting } from '../src/main.js';\n"
                "assert.equal(formatGreeting(' Ada '), 'Hello, Ada');\n"
                "assert.equal(formatGreeting(''), 'Hello, guest');\n"
            ),
        }
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=functional_bundle,
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True


@pytest.mark.usefixtures("trusted_python_fixture_commands")
def test_execute_project_scale_plan_can_validate_generated_project_bundle() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0; node --check completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps({"scripts": {"build": "node --check src/main.js"}}),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
            }
        ),
    )

    report = execute_project_scale_plan(
        plan,
        client,
        validate_generated_project=True,
        generated_project_commands=(
            (
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; assert Path('package.json').exists(); "
                    "assert Path('src/main.js').exists()"
                ),
            ),
        ),
    )

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["generated_project_validation"] is True
    assert result.errors == ()


@pytest.mark.usefixtures("trusted_python_fixture_commands")
def test_execute_project_scale_plan_fails_when_generated_project_validation_fails() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0; node --check completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps({"scripts": {"build": "node --check src/main.js"}}),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
            }
        ),
    )

    report = execute_project_scale_plan(
        plan,
        client,
        validate_generated_project=True,
        generated_project_commands=(
            (
                sys.executable,
                "-c",
                "print('src/app.ts(1,1): error TS2322: type mismatch'); raise SystemExit(7)",
            ),
        ),
    )

    assert report.ok is False
    result = report.results[0]
    assert result.evidence["generated_project_validation"] is False
    assert result.missing_evidence == ("generated_project_validation",)
    assert result.errors == (
        (
            "generated_project_validation: command failed exit=7 command="
            f"{sys.executable} -c print('src/app.ts(1,1): error TS2322: type mismatch'); "
            "raise SystemExit(7) output_tail="
            '"src/app.ts(1,1): error TS2322: type mismatch"'
        ),
    )


@pytest.mark.usefixtures("trusted_python_fixture_commands")
def test_execute_project_scale_plan_repairs_generated_project_validation_failure(
    tmp_path: Path,
) -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("medium",), flows=("artifact_production",), execute=True)
    marker = tmp_path / "validation-repaired"
    client = FakeAcceptanceClient(
        run_id="run-medium-artifact-validation",
        session_id="project-scale-medium-artifact_production",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0; node --check completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps({"scripts": {"build": "node --check src/main.js"}}),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
            }
        ),
    )

    report = execute_project_scale_plan(
        plan,
        client,
        validate_generated_project=True,
        generated_project_commands=(
            (
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    f"p=Path({str(marker)!r}); "
                    "sys.exit(0) if p.exists() else (p.write_text('seen'), sys.exit(7))"
                ),
            ),
        ),
    )

    assert report.ok is True
    result = report.results[0]
    assert result.run_id == "run-medium-artifact-validation-repair"
    assert result.evidence["generated_project_validation"] is True
    assert result.evidence["deliverable_repair_trace"] is True
    assert len(client.submitted_bodies) == 2
    repair_message = str(client.submitted_bodies[1]["message"])
    assert "generated_project_validation: command failed exit=7" in repair_message
    assert "rerun build/test/interaction checks" in repair_message


def test_execute_project_scale_plan_merges_partial_repair_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=("large",),
        flows=("direct",),
        execute=True,
    )
    base_bundle = _project_bundle(
        {
            "README.md": "# Order Ops\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Large order operations contract\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; node --check completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps({"scripts": {"build": "node --check src/main.js"}}),
            "src/main.js": _functional_js_source(),
            "tests/audit.test.js": "throw new Error('syntax stays broken');\n",
        }
    )
    patch_files = {
        "tests/audit.test.js": "import assert from 'node:assert/strict';\nassert.equal(42, 42);\n"
    }
    seen: list[bytes | None] = []

    def validate_generated_project_bundle(
        bundle: bytes | None, **kwargs: object
    ) -> project_scale_runner_module._EvidenceCheck:
        seen.append(bundle)
        assert bundle is not None
        with zipfile.ZipFile(BytesIO(bundle)) as archive:
            names = set(archive.namelist())
            repaired_test = archive.read("tests/audit.test.js").decode("utf-8")
        if len(seen) == 1:
            return project_scale_runner_module._EvidenceCheck(
                passed=False,
                reasons=("generated_project_validation: command failed exit=2 output_tail=\"tests/audit.test.js(1,1): error\"",),
            )
        assert "src/main.js" in names
        assert "assert.equal(42, 42)" in repaired_test
        return project_scale_runner_module._EvidenceCheck(passed=True, reasons=())

    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        validate_generated_project_bundle,
    )
    class EmbeddedRepairClient(FakeAcceptanceClient):
        def request_bytes(self, method: str, path: str) -> bytes:
            if self._collecting_repair_run:
                raise RuntimeError("public workspace unavailable for embedded patch")
            return super().request_bytes(method, path)

    client = EmbeddedRepairClient(
        run_id="run-large-direct-merge",
        session_id="project-scale-large-direct",
        create_status="waiting_approval",
        decision_token="approve-large",
        decision_version=4,
        statuses=("queued", "completed"),
        repair_create_status="waiting_approval",
        repair_decision_token="approve-large-repair",
        repair_decision_version=7,
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "payload": {
                    "agent_standard_verification": {
                        "constraints_read": True,
                        "plan_before_implementation": True,
                        "reproducible_verification": True,
                        "root_cause_repair": True,
                    }
                },
            }
        ],
        workspace_bundle=base_bundle,
        repair_events=[
            _trusted_agent_standard_event(),
            {"kind": "artifact.created", "payload": {"workspace_bundle": {"files": patch_files}}},
        ],
    )

    report = execute_project_scale_plan(plan, client, wait_seconds=5, poll_interval_seconds=0)

    result = report.results[0]
    assert report.ok is True
    assert result.run_id == "run-large-direct-merge-repair"
    assert result.evidence["generated_project_validation"] is True
    assert result.evidence["deliverable_repair_trace"] is True
    assert result.workspace_bundle_source == "embedded_bundle"
    assert len(seen) == 2


def _validation_manifest_files() -> dict[str, str]:
    return {
        "README.md": "# Task API\nImplements the requested scope.\n",
        "PROJECT_REQUIREMENTS.md": "- Task API with persistence\n",
        "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
        "VERIFICATION.md": "Independent build and tests are required.\n",
        "package.json": '{"scripts":{"build":"node --check src/main.js","test":"node --test"}}',
        "src/main.js": _functional_js_source(),
        "tests/main.test.js": _functional_js_test(),
        "tests/retired.test.js": "// retained only in the initial workspace\n",
        "tests/expected.json": '{"expected":42}',
    }


@pytest.mark.parametrize("replace_files", (False, True))
@pytest.mark.parametrize("authoritative", (False, True))
@pytest.mark.parametrize("defect", ("missing_file", "invalid_json"))
def test_public_repair_validation_never_merges_previous_workspace(
    monkeypatch: pytest.MonkeyPatch,
    replace_files: bool,
    authoritative: bool,
    defect: str,
) -> None:
    base_files = _validation_manifest_files()
    public_files = dict(base_files)
    if defect == "missing_file":
        del public_files["tests/retired.test.js"]
    else:
        public_files["tests/expected.json"] = "{"
    if not authoritative:
        del public_files["IMPLEMENTATION_PLAN.md"]
    seen: list[bytes | None] = []

    def validate(bundle: bytes | None, **kwargs: object) -> object:
        seen.append(bundle)
        reason = (
            "generated_project_validation: command failed exit=1"
            if len(seen) == 1
            else "generated_project_validation: isolated systemd validator is required"
        )
        return project_scale_runner_module._EvidenceCheck(passed=False, reasons=(reason,))

    original_repair_body = project_scale_runner_module._deliverable_repair_body

    def repair_body(*args: Any, **kwargs: Any) -> dict[str, object]:
        body = original_repair_body(*args, **kwargs)
        body["replace_workspace_files"] = replace_files
        return body

    monkeypatch.setattr(project_scale_runner_module, "_validate_generated_project_bundle", validate)
    monkeypatch.setattr(project_scale_runner_module, "_deliverable_repair_body", repair_body)
    plan = build_project_scale_run_plan(
        benchmark_kind="capability", scales=("small",), flows=("direct",), execute=True
    )
    client = FakeAcceptanceClient(
        status="completed", artifacts=[{"id": "artifact-1"}],
        events=[_trusted_agent_standard_event()],
        workspace_bundle=_project_bundle(base_files),
        repair_workspace_bundle=_project_bundle(public_files),
    )

    result = execute_project_scale_plan(plan, client).results[0]

    assert len(seen) == 2
    assert client.submitted_bodies[1]["replace_workspace_files"] is replace_files
    assert seen[1] is not None
    with zipfile.ZipFile(BytesIO(seen[1])) as archive:
        actual = {name: archive.read(name) for name in archive.namelist()}
    assert actual == {path: content.encode() for path, content in public_files.items()}
    assert result.evidence["generated_project_validation"] is False
    assert result.validated_workspace_manifest is None


@pytest.mark.parametrize("delivery", ("initial", "replacement", "materialized_patch"))
@pytest.mark.parametrize("scale", ("small", "ultra"))
def test_validated_workspace_manifest_records_last_successful_input(
    monkeypatch: pytest.MonkeyPatch, delivery: str, scale: str,
) -> None:
    files = _validation_manifest_files()
    final_files = dict(files)
    final_files["tests/expected.json"] = '{"expected":43}'
    seen: list[bytes | None] = []

    def validate(bundle: bytes | None, **kwargs: object) -> object:
        seen.append(bundle)
        passed = delivery == "initial" or len(seen) > 1
        return project_scale_runner_module._EvidenceCheck(
            passed=passed,
            reasons=() if passed else ("generated_project_validation: command failed exit=1",),
            scale_validation=_ultra_load_result() if passed and scale == "ultra" else None,
        )

    monkeypatch.setattr(project_scale_runner_module, "_validate_generated_project_bundle", validate)
    if delivery == "replacement":
        original_repair_body = project_scale_runner_module._deliverable_repair_body

        def replacement_body(*args: Any, **kwargs: Any) -> dict[str, object]:
            return {**original_repair_body(*args, **kwargs), "replace_workspace_files": True}

        monkeypatch.setattr(project_scale_runner_module, "_deliverable_repair_body", replacement_body)
    plan = build_project_scale_run_plan(
        benchmark_kind="capability", scales=(scale,), flows=("direct",), execute=True
    )
    client = FakeAcceptanceClient(
        run_id=f"run-{scale}-direct", session_id=f"project-scale-{scale}-direct",
        create_status="waiting_approval" if scale == "ultra" else "completed",
        decision_token="approve-scale", decision_version=2,
        repair_create_status="waiting_approval" if scale == "ultra" else "completed",
        repair_decision_token="approve-scale-repair", repair_decision_version=3,
        status="completed", artifacts=[{"id": "artifact-1"}],
        events=[_trusted_agent_standard_event()],
        workspace_bundle=_project_bundle(files),
        repair_workspace_bundle=_project_bundle(final_files),
    )

    result = execute_project_scale_plan(plan, client).results[0]

    assert result.ok, result.errors
    assert len(seen) == (1 if delivery == "initial" else 2)
    expected_files = files if delivery == "initial" else final_files
    expected = {
        path: (len(content.encode()), hashlib.sha256(content.encode()).hexdigest())
        for path, content in expected_files.items()
    }
    assert result.validated_workspace_manifest == expected
    assert result.scale_specific_evidence_ok
    if scale == "ultra":
        assert result.scale_validation == project_scale_runner_module._bind_scale_validation(
            _ultra_load_result(), f"{scale}:direct", result.run_id, expected,
        )
        assert result.run_id == f"run-{scale}-direct" + (
            "" if delivery == "initial" else "-repair"
        )
    else:
        assert result.scale_validation is None
    payload = result.to_payload()
    assert payload["validated_workspace_manifest"] == {
        path: [size, digest] for path, (size, digest) in expected.items()
    }
    assert json.loads(json.dumps(payload))["validated_workspace_manifest"] == (
        payload["validated_workspace_manifest"]
    )


@pytest.mark.parametrize("failure", ("submission", "observation", "build", "requirements", "preview"))
@pytest.mark.parametrize("scale", ("small", "ultra"))
def test_validated_workspace_manifest_clears_after_success_before_failed_repair(
    monkeypatch: pytest.MonkeyPatch, failure: str, scale: str,
) -> None:
    files = _validation_manifest_files()
    repair_files = dict(files)
    del repair_files["tests/retired.test.js"]
    validations: list[bytes | None] = []

    def validate(bundle: bytes | None, **kwargs: object) -> object:
        validations.append(bundle)
        if len(validations) == 1:
            return project_scale_runner_module._EvidenceCheck(
                passed=True, reasons=(),
                scale_validation=_ultra_load_result() if scale == "ultra" else None,
            )
        if failure == "build":
            raise RuntimeError("build validator failed after repair")
        if failure == "requirements":
            return project_scale_runner_module._EvidenceCheck(
                passed=False, reasons=("requirements: persistence lost",),
            )
        return project_scale_runner_module._EvidenceCheck(
            passed=True, reasons=(),
            scale_validation=_ultra_load_result() if scale == "ultra" else None,
        )

    original_observe = project_scale_runner_module._collect_run_observation
    observations = 0

    def observe(*args: Any, **kwargs: Any) -> object:
        nonlocal observations
        observations += 1
        if observations > 1 and failure == "observation":
            raise RuntimeError("repair observation failed")
        return original_observe(*args, **kwargs)

    original_preview = project_scale_runner_module._validate_requested_web_preview

    def preview(bundle: bytes | None, body: dict[str, object]) -> object:
        if len(validations) > 1 and failure == "preview":
            return project_scale_runner_module._EvidenceCheck(
                passed=False, reasons=("requirements: preview entrypoint missing",),
            )
        return original_preview(bundle, body)

    class FailingRepairClient(FakeAcceptanceClient):
        def request_json(
            self, method: str, path: str, **kwargs: Any,
        ) -> dict[str, object] | list[object]:
            if (
                method == "POST" and path == "/api/v1/runs"
                and self.submitted_bodies and failure == "submission"
            ):
                raise RuntimeError("repair submission failed")
            return super().request_json(method, path, **kwargs)

    monkeypatch.setattr(project_scale_runner_module, "_validate_generated_project_bundle", validate)
    monkeypatch.setattr(project_scale_runner_module, "_collect_run_observation", observe)
    monkeypatch.setattr(project_scale_runner_module, "_validate_requested_web_preview", preview)
    monkeypatch.setattr(project_scale_runner_module, "_deliverable_repair_attempt_limit", lambda *a, **k: 1)
    monkeypatch.setattr(project_scale_runner_module, "_deliverable_repair_safety_limit", lambda *a, **k: 1)
    plan = build_project_scale_run_plan(
        benchmark_kind="capability", scales=(scale,), flows=("direct",), execute=True
    )
    # Missing trusted process evidence triggers repair after a successful first validation.
    client = FailingRepairClient(
        run_id=f"run-{scale}-direct", session_id=f"project-scale-{scale}-direct",
        create_status="waiting_approval" if scale == "ultra" else "completed",
        decision_token="approve-scale", decision_version=2,
        repair_create_status="waiting_approval" if scale == "ultra" else "completed",
        repair_decision_token="approve-scale-repair", repair_decision_version=3,
        status="completed", artifacts=[{"id": "artifact-1"}],
        workspace_bundle=_project_bundle(files), repair_workspace_bundle=_project_bundle(repair_files),
        repair_events=[_trusted_agent_standard_event()],
    )

    result = execute_project_scale_plan(plan, client).results[0]

    assert validations
    assert len(validations) == (1 if failure in {"submission", "observation"} else 2)
    assert result.ok is False
    assert result.validated_workspace_manifest is None
    assert result.to_payload()["validated_workspace_manifest"] is None
    assert result.scale_validation is None
    assert result.to_payload()["scale_validation"] is None
    assert result.scale_specific_evidence_ok is (scale != "ultra")


def test_validated_workspace_manifest_defaults_to_none_without_requirements() -> None:
    result = ProjectScaleCaseResult(
        case_id="small:direct", run_id="run-small-direct", status="completed", evidence={},
    )
    assert result.validated_workspace_manifest is None
    assert result.to_payload()["validated_workspace_manifest"] is None


@pytest.mark.usefixtures("trusted_python_fixture_commands")
def test_execute_project_scale_plan_repairs_running_run_with_invalid_generated_project(
    tmp_path: Path,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="fixture",
        scales=("medium",),
        flows=("artifact_production",),
        execute=True,
    )
    marker = tmp_path / "running-validation-repaired"
    client = FakeAcceptanceClient(
        run_id="run-medium-artifact-running-validation",
        session_id="project-scale-medium-artifact_production",
        statuses=("running", "completed"),
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0; node --check completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps({"scripts": {"build": "node --check src/main.js"}}),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
            }
        ),
    )

    report = execute_project_scale_plan(
        plan,
        client,
        wait_seconds=30,
        poll_interval_seconds=0,
        validate_generated_project=True,
        generated_project_commands=(
            (
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    f"p=Path({str(marker)!r}); "
                    "sys.exit(0) if p.exists() else (p.write_text('seen'), sys.exit(7))"
                ),
            ),
        ),
    )

    result = report.results[0]
    assert report.ok is True
    assert result.run_id == "run-medium-artifact-running-validation-repair"
    assert result.evidence["deliverable_repair_trace"] is True
    assert result.evidence["generated_project_validation"] is True
    repair_message = str(client.submitted_bodies[1]["message"])
    assert "generated_project_validation: command failed exit=7" in repair_message


def test_capability_repair_retries_when_repair_run_fails_without_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=("medium",),
        flows=("direct",),
        execute=True,
    )
    validation_results = [
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=(
                'generated_project_validation: command failed exit=2 command=npm run build output_tail="type error"',
            ),
        ),
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=("generated_project_validation: missing workspace bundle",),
        ),
        project_scale_runner_module._EvidenceCheck(passed=True, reasons=()),
    ]

    def validate_generated_project_bundle(
        bundle: bytes | None, **kwargs: object
    ) -> project_scale_runner_module._EvidenceCheck:
        return validation_results.pop(0)

    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        validate_generated_project_bundle,
    )

    class RepairFailureClient:
        def __init__(self) -> None:
            self.submitted_bodies: list[dict[str, object]] = []
            self.calls: list[tuple[str, str, str | None]] = []
            self.bundle_requests = 0
            self.run_ids = (
                "run-medium-direct",
                "run-medium-direct-repair-1",
                "run-medium-direct-repair-2",
            )

        def request_json(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object] | list[object]:
            self.calls.append((method, path, idempotency_key))
            if method == "POST" and path == "/api/v1/runs":
                assert body is not None
                index = len(self.submitted_bodies)
                self.submitted_bodies.append(dict(body))
                return {
                    "id": self.run_ids[index],
                    "status": "completed" if index != 1 else "failed",
                    "project_id": body["project_id"],
                    "workspace_session_id": body["workspace_session_id"],
                    "mode": body["mode"],
                }
            for index, run_id in enumerate(self.run_ids):
                if path == f"/api/v1/runs/{run_id}/details":
                    return {
                        "id": run_id,
                        "status": "failed" if index == 1 else "completed",
                        "artifacts": [] if index == 1 else [{"id": f"artifact-{index}"}],
                        "mode": self.submitted_bodies[-1]["mode"],
                    }
                if path == f"/api/v1/runs/{run_id}/events":
                    return [
                        {
                            "kind": "artifact.created",
                            "run_id": run_id,
                            "tool_name": "project.generate_zip",
                            "payload": {
                                "agent_standard_verification": {
                                    "constraints_read": True,
                                    "plan_before_implementation": True,
                                    "reproducible_verification": True,
                                    "root_cause_repair": True,
                                }
                            },
                        }
                    ]
                if path == f"/api/v1/admin/runs/{run_id}":
                    return {
                        "id": run_id,
                        "status": "failed" if index == 1 else "completed",
                        "artifacts": [] if index == 1 else [{"id": f"artifact-{index}"}],
                    }
            raise AssertionError(f"unexpected JSON request {method} {path}")

        def request_bytes(self, method: str, path: str) -> bytes:
            self.calls.append((method, path, None))
            self.bundle_requests += 1
            if self.bundle_requests == 2:
                raise RuntimeError("workspace bundle unavailable")
            return _project_bundle(
                {
                    "README.md": "# CRM Lite\n\nImplements the requested project scope.\n",
                    "PROJECT_REQUIREMENTS.md": "- CRM requirement satisfied\n- Interaction verified\n",
                    "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                    "VERIFICATION.md": (
                        "- npm run build: passed exit 0\n"
                        "- npm test: passed exit 0; 3 tests passed\n"
                        "- interaction smoke: passed\n"
                    ),
                    "package.json": json.dumps(
                        {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                        sort_keys=True,
                    ),
                    "src/main.js": _functional_js_source(),
                    "tests/main.test.js": _functional_js_test(),
                }
            )

    client = RepairFailureClient()

    report = execute_project_scale_plan(
        plan,
        client,
        validate_generated_project=True,
    )

    result = report.results[0]
    assert report.ok is True
    assert result.run_id == "run-medium-direct-repair-2"
    assert result.evidence["deliverable_repair_trace"] is True
    assert result.evidence["generated_project_validation"] is True
    assert len(client.submitted_bodies) == 3
    assert "generated_project_validation: missing workspace bundle" in str(
        client.submitted_bodies[2]["message"]
    )


def test_capability_repair_stops_when_validator_isolation_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=("small",),
        flows=("direct",),
        execute=True,
    )
    validation_results = [
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=("generated_project_validation: command failed exit=2",),
        ),
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=(
                (
                    "generated_project_validation: isolated systemd validator is required "
                    "for generated npm/node commands"
                ),
            ),
        ),
    ]

    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        lambda bundle, **kwargs: validation_results.pop(0),
    )
    client = FakeAcceptanceClient(
        run_id="run-small-direct-isolation",
        session_id="project-scale-small-direct",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=_project_bundle(
            dict(
                project_scale_artifact_zip_files(
                    "Build a real small business project for flow=direct."
                )
            )
        ),
    )

    report = execute_project_scale_plan(
        plan,
        client,
        validate_generated_project=True,
    )

    assert report.ok is False
    assert len(client.submitted_bodies) == 2
    assert any(
        "isolated systemd validator is required" in error
        for error in report.results[0].errors
    )


def test_execute_project_scale_plan_extends_wait_budget_for_observable_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("medium",), flows=("artifact_production",), execute=True)
    validation_results = [
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=(
                'generated_project_validation: command failed exit=7 command=npm test output_tail="boom"',
            ),
        ),
        project_scale_runner_module._EvidenceCheck(passed=True, reasons=()),
    ]

    def validate_generated_project_bundle(
        bundle: bytes | None, **kwargs: object
    ) -> project_scale_runner_module._EvidenceCheck:
        assert bundle is not None
        return validation_results.pop(0)

    ticks = iter((0.0, 0.0, 0.0, 2.0, 2.0))

    def monotonic() -> float:
        return next(ticks, 2.0)

    monkeypatch.setattr(project_scale_runner_module, "_validate_generated_project_bundle", validate_generated_project_bundle)
    monkeypatch.setattr("agent_hub.harness.project_scale_runner.time.monotonic", monotonic)
    client = FakeAcceptanceClient(
        run_id="run-medium-artifact-validation",
        session_id="project-scale-medium-artifact_production",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0; node --check completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps({"scripts": {"build": "node --check src/main.js"}}),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
            }
        ),
    )

    report = execute_project_scale_plan(
        plan,
        client,
        wait_seconds=1,
        poll_interval_seconds=0,
        validate_generated_project=True,
    )

    result = report.results[0]
    assert report.ok is True
    assert result.run_id == "run-medium-artifact-validation-repair"
    assert result.evidence["deliverable_repair_trace"] is True
    assert result.evidence["generated_project_validation"] is True
    assert len(client.submitted_bodies) == 2
    assert any(
        call[0] == "POST" and call[1] == "/api/v1/runs" and "deliverable-repair" in (call[2] or "")
        for call in client.calls
    )
    assert result.errors == ()


@pytest.mark.parametrize(
    "second_regression",
    (False, True),
    ids=("single_regression", "repeated_regression"),
)
def test_capability_generated_project_repair_allows_repeated_validation_regression_followup(
    monkeypatch: pytest.MonkeyPatch,
    second_regression: bool,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=("medium",),
        flows=("direct",),
        execute=True,
    )
    validation_results = [
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=(
                (
                    "generated_project_validation: command failed exit=7 "
                    'command=npm run build output_tail="src/app.ts(1,1): error TS2322"'
                ),
            ),
        ),
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=(
                (
                    "generated_project_validation: command failed exit=2 "
                    'command=npm run build output_tail="tests/unit.test.ts(41,16): '
                    'error TS2554: Expected 2 arguments, but got 1."'
                ),
            ),
        ),
        project_scale_runner_module._EvidenceCheck(passed=True, reasons=()),
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=(
                "requirements: CRM workflow: startup: npm start exited before CRM API became ready",
            ),
        ),
        project_scale_runner_module._EvidenceCheck(passed=True, reasons=()),
    ]
    if second_regression:
        validation_results.append(
            project_scale_runner_module._EvidenceCheck(
                passed=False,
                reasons=(
                    "requirements: CRM workflow: startup: npm start exited before CRM API became ready",
                ),
            )
        )
        validation_results.append(
            project_scale_runner_module._EvidenceCheck(passed=True, reasons=())
        )
    failed_verification = project_scale_runner_module._EvidenceCheck(
        passed=False,
        reasons=(
            (
                "agent_standard_verification: trusted runtime context/plan "
                "evidence unavailable"
            ),
        ),
    )
    if second_regression:
        alternate_failed_verification = project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=("agent_standard_verification: root cause repair evidence unavailable",),
        )
        verification_results = [failed_verification for _ in range(4)] + [
            alternate_failed_verification,
            alternate_failed_verification,
            project_scale_runner_module._EvidenceCheck(passed=True, reasons=()),
        ]
    else:
        verification_results = [failed_verification for _ in range(4)]
        verification_results.append(
            project_scale_runner_module._EvidenceCheck(passed=True, reasons=())
        )

    def validate_generated_project_bundle(
        bundle: bytes | None, **kwargs: object
    ) -> project_scale_runner_module._EvidenceCheck:
        assert bundle is not None
        return validation_results.pop(0)

    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        validate_generated_project_bundle,
    )

    def evaluate_agent_standard_verification(
        *args: object, **kwargs: object
    ) -> project_scale_runner_module._EvidenceCheck:
        return verification_results.pop(0)

    monkeypatch.setattr(
        project_scale_runner_module,
        "_evaluate_agent_standard_verification",
        evaluate_agent_standard_verification,
    )
    client = FakeAcceptanceClient(
        run_id="run-medium-direct-validation",
        session_id="project-scale-medium-direct",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "payload": {
                    "agent_standard_verification": {
                        "constraints_read": True,
                        "plan_before_implementation": True,
                        "reproducible_verification": True,
                        "root_cause_repair": True,
                    }
                },
            }
        ],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# CRM Lite\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- CRM requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0\n"
                    "- npm test: passed exit 0\n"
                    "- interaction smoke: passed by real HTTP checks\n"
                ),
                "constraints_reading_evidence.json": json.dumps(
                    {
                        "read_before_implementation": True,
                        "constraints": [
                            "AGENTS.md workspace rules",
                            "HANDOFF",
                            "PROJECT_REQUIREMENTS.md",
                        ],
                        "skills": ["applicable SKILL.md or agent-standard rules"],
                    }
                ),
                "package.json": json.dumps({"scripts": {"build": "tsc", "test": "node --test"}}),
                "src/app.ts": _functional_ts_source(),
                "tests/unit.test.ts": _functional_ts_test(),
            }
        ),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    result = report.results[0]
    assert result.run_id == "run-medium-direct-validation-repair"
    assert result.evidence["generated_project_validation"] is True
    assert result.evidence["deliverable_repair_trace"] is True
    assert len(client.submitted_bodies) == (7 if second_regression else 5)
    repair_messages = [str(body["message"]) for body in client.submitted_bodies[1:]]
    assert "src/app.ts(1,1): error TS2322" in repair_messages[0]
    assert "Current workspace context for precise repair" in repair_messages[0]
    assert "src/app.ts" in repair_messages[0]
    assert "Expected 2 arguments, but got 1" in repair_messages[1]
    assert "validator helpers that require a field argument" in repair_messages[1]
    assert "trusted runtime context/plan evidence unavailable" in repair_messages[2]
    assert "npm start exited before CRM API became ready" in repair_messages[3]
    assert client.submitted_bodies[4]["replace_workspace_files"] is True
    assert "Replace the entire workspace" in repair_messages[3]
    if second_regression:
        assert "root cause repair evidence unavailable" in repair_messages[4]
    assert validation_results == []
    assert verification_results == []
    repair_keys = [
        call[2]
        for call in client.calls
        if call[0] == "POST"
        and call[1] == "/api/v1/runs"
        and call[2] is not None
        and "deliverable-repair" in call[2]
    ]
    expected_repair_keys = [
        "project-scale-medium-direct-0-deliverable-repair",
        "project-scale-medium-direct-0-deliverable-repair-2",
        "project-scale-medium-direct-0-deliverable-repair-3",
        "project-scale-medium-direct-0-deliverable-repair-4",
    ]
    if second_regression:
        expected_repair_keys.append("project-scale-medium-direct-0-deliverable-repair-5")
        expected_repair_keys.append("project-scale-medium-direct-0-deliverable-repair-6")
    assert repair_keys == expected_repair_keys


def test_build_regression_requires_one_authoritative_recovery() -> None:
    previous = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("npm test failed",),
        validation_stage=2,
        progress_metrics=(),
    )
    current = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("npm run build failed",),
        validation_stage=1,
        progress_metrics=(),
    )

    assert project_scale_runner_module._deliverable_repair_requires_authoritative_recovery(
        previous,
        current,
    )


def test_repeated_same_stage_failure_does_not_force_authoritative_recovery() -> None:
    previous = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("npm run build failed once",),
        validation_stage=1,
        progress_metrics=(),
    )
    current = project_scale_runner_module._DeliverableRepairProgress(
        deficits=("generated_project_validation",),
        failure_fingerprints=("npm run build failed again",),
        validation_stage=1,
        progress_metrics=(),
    )

    assert not project_scale_runner_module._deliverable_repair_requires_authoritative_recovery(
        previous,
        current,
    )


def test_capability_repair_stops_at_soft_limit_for_non_typescript_error_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=("small",),
        flows=("direct",),
        execute=True,
    )
    validation_results = [
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=(
                (
                    "generated_project_validation: command failed exit=2 "
                    f'command=npm run build output_tail="unclassified build failure {attempt}"'
                ),
            ),
        )
        for attempt in range(6)
    ] + [project_scale_runner_module._EvidenceCheck(passed=True, reasons=())]

    def validate_generated_project_bundle(
        bundle: bytes | None, **kwargs: object
    ) -> project_scale_runner_module._EvidenceCheck:
        assert bundle is not None
        return validation_results.pop(0)

    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        validate_generated_project_bundle,
    )
    client = FakeAcceptanceClient(
        run_id="run-small-direct-dynamic-budget",
        session_id="project-scale-small-direct",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "payload": {
                    "agent_standard_verification": {
                        "constraints_read": True,
                        "plan_before_implementation": True,
                        "reproducible_verification": True,
                        "root_cause_repair": True,
                    }
                },
            }
        ],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Task API\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Task API requirement satisfied\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0\n"
                    "- npm test: passed exit 0\n"
                    "- interaction smoke: passed by real HTTP checks\n"
                ),
                "constraints_reading_evidence.json": json.dumps(
                    {
                        "read_before_implementation": True,
                        "constraints": [
                            "AGENTS.md workspace rules",
                            "HANDOFF",
                            "PROJECT_REQUIREMENTS.md",
                        ],
                        "skills": ["applicable SKILL.md or agent-standard rules"],
                    }
                ),
                "package.json": json.dumps(
                    {"scripts": {"build": "tsc", "test": "node --test"}}
                ),
                "src/app.ts": _functional_ts_source(),
                "tests/unit.test.ts": _functional_ts_test(),
            }
        ),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert project_scale_runner_module._deliverable_repair_attempt_limit(
        "small:direct", benchmark_kind="capability"
    ) == 5
    assert len(client.submitted_bodies) == 5
    assert len(validation_results) == 2
    result = report.results[0]
    assert result.evidence["generated_project_validation"] is False
    assert result.evidence["deliverable_repair_trace"] is True
    assert any("unclassified build failure 4" in error for error in result.errors)
    repair_keys = [
        call[2]
        for call in client.calls
        if call[0] == "POST"
        and call[1] == "/api/v1/runs"
        and call[2] is not None
        and "deliverable-repair" in call[2]
    ]
    assert repair_keys == [
        "project-scale-small-direct-0-deliverable-repair",
        "project-scale-small-direct-0-deliverable-repair-2",
        "project-scale-small-direct-0-deliverable-repair-3",
        "project-scale-small-direct-0-deliverable-repair-4",
    ]


def test_capability_repair_extends_past_scale_budget_for_repeated_actionable_regressions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=("small",),
        flows=("direct",),
        execute=True,
    )
    validation_results = [
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=('generated_project_validation: command failed command=npm run build output_tail="a"',),
        ),
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=('generated_project_validation: command failed command=npm run build output_tail="b"',),
        ),
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=('generated_project_validation: command failed command=npm run build output_tail="c"',),
        ),
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=('generated_project_validation: command failed command=npm test output_tail="d"',),
        ),
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=("requirements: task workflow expected 201, got 400",),
        ),
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=('generated_project_validation: command failed command=npm test output_tail="new regression"',),
        ),
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=("requirements: task workflow expected persisted item, got empty list",),
        ),
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=(
                (
                    'generated_project_validation: command failed command=npm test '
                    'output_tail="second new regression"'
                ),
            ),
        ),
        project_scale_runner_module._EvidenceCheck(passed=True, reasons=()),
    ]

    def validate_generated_project_bundle(
        bundle: bytes | None, **kwargs: object
    ) -> project_scale_runner_module._EvidenceCheck:
        assert bundle is not None
        return validation_results.pop(0)

    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        validate_generated_project_bundle,
    )
    client = FakeAcceptanceClient(
        run_id="run-small-direct-regression-budget",
        session_id="project-scale-small-direct",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "payload": {
                    "agent_standard_verification": {
                        "constraints_read": True,
                        "plan_before_implementation": True,
                        "reproducible_verification": True,
                        "root_cause_repair": True,
                    }
                },
            }
        ],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Task API\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Task API requirement satisfied\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0\n"
                    "- npm test: passed exit 0\n"
                    "- interaction smoke: passed by real HTTP checks\n"
                ),
                "constraints_reading_evidence.json": json.dumps(
                    {
                        "read_before_implementation": True,
                        "constraints": [
                            "AGENTS.md workspace rules",
                            "HANDOFF",
                            "PROJECT_REQUIREMENTS.md",
                        ],
                        "skills": ["applicable SKILL.md or agent-standard rules"],
                    }
                ),
                "package.json": json.dumps(
                    {"scripts": {"build": "tsc", "test": "node --test"}}
                ),
                "src/app.ts": _functional_ts_source(),
                "tests/unit.test.ts": _functional_ts_test(),
            }
        ),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    assert len(client.submitted_bodies) == 9
    assert validation_results == []


def test_medium_capability_repair_extends_after_six_distinct_typescript_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=("medium",),
        flows=("direct",),
        execute=True,
    )
    typescript_failures = (
        "src/scripts.ts(4,7): error TS2305: Module has no exported member 'startServer'",
        "src/router.ts:18:9 - error TS2322: Type 'string | undefined' is not assignable to type 'string'",
        "src/store.ts(31,14): error TS2345: Argument of type 'Store' is not assignable to parameter of type 'Repository'",
        "src/handlers.ts:22:5 - error TS2739: Type 'Handler' is missing properties from type 'Router'",
        "src/router.ts(47,3): error TS2769: No overload matches this call",
        "src/seed.ts:11:17 - error TS7053: Element implicitly has an 'any' type",
        (
            "src/handlers.ts(63,7): error TS2322: Type "
            "'Request<{tenant_id:string}>' is not assignable to type "
            "'Handler<Record<string,string>>'"
        ),
        "error TS18003: No inputs were found in config file 'tsconfig.json'",
    )
    validation_results = [
        _typescript_build_validation(
            "\n".join(
                (
                    typescript_failures[index],
                    typescript_failures[index + 1],
                    "Found 2 errors in 2 files.",
                    "npm error Lifecycle script `build` failed with error",
                )
            )
        )
        for index in range(len(typescript_failures) - 1)
    ]
    validation_results.append(
        project_scale_runner_module._EvidenceCheck(passed=True, reasons=())
    )

    def validate_generated_project_bundle(
        bundle: bytes | None, **kwargs: object
    ) -> project_scale_runner_module._EvidenceCheck:
        assert bundle is not None
        return validation_results.pop(0)

    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        validate_generated_project_bundle,
    )
    client = FakeAcceptanceClient(
        run_id="run-medium-direct-progressive-typescript-repair",
        session_id="project-scale-medium-direct",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "payload": {
                    "agent_standard_verification": {
                        "constraints_read": True,
                        "plan_before_implementation": True,
                        "reproducible_verification": True,
                        "root_cause_repair": True,
                    }
                },
            }
        ],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# CRM API\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Multi-tenant CRM requirements satisfied\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0\n"
                    "- npm test: passed exit 0\n"
                    "- interaction smoke: passed by real HTTP checks\n"
                ),
                "constraints_reading_evidence.json": json.dumps(
                    {
                        "read_before_implementation": True,
                        "constraints": [
                            "AGENTS.md workspace rules",
                            "HANDOFF",
                            "PROJECT_REQUIREMENTS.md",
                        ],
                        "skills": ["applicable SKILL.md or agent-standard rules"],
                    }
                ),
                "package.json": json.dumps(
                    {
                        "scripts": {
                            "build": "tsc",
                            "test": "node --test",
                            "start": "node dist/app.js",
                        }
                    }
                ),
                "src/app.ts": _functional_ts_source(),
                "tests/unit.test.ts": _functional_ts_test(),
            }
        ),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    assert len(client.submitted_bodies) == 8
    assert client.submitted_bodies[-1]["replace_workspace_files"] is True
    assert "Replace the entire workspace with one coherent implementation" in str(
        client.submitted_bodies[-1]["message"]
    )
    assert validation_results == []


@pytest.mark.parametrize(
    ("scale", "seconds_per_validation", "failed_validations"),
    (
        ("large", 22.0, 10),
        ("ultra", 20.0, 12),
    ),
)
def test_large_capability_progress_extends_absolute_wall_clock_budget(
    monkeypatch: pytest.MonkeyPatch,
    scale: str,
    seconds_per_validation: float,
    failed_validations: int,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=(scale,),
        flows=("direct",),
        execute=True,
    )
    clock = _ControlledMonotonicClock()
    initial_absolute_deadline = project_scale_runner_module._case_absolute_deadline(
        0.0,
        configured_wait_seconds=100.0,
        case_id=f"{scale}:direct",
        benchmark_kind="capability",
    )
    observed_absolute_deadlines: list[float] = []
    diagnostics = tuple(
        f"src/repair-{index}.ts({index + 1},1): error TS2322: repair failure {index}"
        for index in range(failed_validations + 1)
    )
    validation_results = [
        _typescript_build_validation(
            "\n".join(
                (
                    diagnostics[index],
                    diagnostics[index + 1],
                    "Found 2 errors in 2 files.",
                )
            )
        )
        for index in range(failed_validations)
    ]
    validation_results.append(
        project_scale_runner_module._EvidenceCheck(passed=True, reasons=())
    )
    validation_index = 0

    def validate_generated_project_bundle(
        bundle: bytes | None, **kwargs: object
    ) -> project_scale_runner_module._EvidenceCheck:
        nonlocal validation_index
        assert bundle is not None
        absolute_deadline = kwargs["absolute_deadline"]
        assert isinstance(absolute_deadline, int | float)
        observed_absolute_deadlines.append(float(absolute_deadline))
        clock.advance(seconds_per_validation)
        result = validation_results[min(validation_index, len(validation_results) - 1)]
        validation_index += 1
        return result

    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        validate_generated_project_bundle,
    )
    monkeypatch.setattr(
        "agent_hub.harness.project_scale_runner.time.monotonic",
        clock,
    )
    client = FakeAcceptanceClient(
        run_id=f"run-{scale}-direct-wall-clock-budget",
        session_id=f"project-scale-{scale}-direct",
        create_status="waiting_approval",
        decision_token=f"approve-{scale}",
        decision_version=2,
        repair_create_status="waiting_approval",
        repair_decision_token=f"approve-{scale}-repair",
        repair_decision_version=3,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "payload": {
                    "agent_standard_verification": {
                        "constraints_read": True,
                        "plan_before_implementation": True,
                        "reproducible_verification": True,
                        "root_cause_repair": True,
                    }
                },
            }
        ],
        workspace_bundle=_project_bundle(
            dict(
                project_scale_artifact_zip_files(
                    str(plan.requests[0].body["message"])
                )
            )
        ),
    )

    report = execute_project_scale_plan(
        plan,
        client,
        wait_seconds=100.0,
        poll_interval_seconds=0,
    )

    assert report.ok is True, report.results[0].errors
    assert validation_index == failed_validations + 1
    assert len(client.submitted_bodies) == failed_validations + 1
    assert max(observed_absolute_deadlines) > initial_absolute_deadline
    assert observed_absolute_deadlines == sorted(observed_absolute_deadlines)


def test_capability_repair_retries_transient_failed_run_at_soft_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=("small",),
        flows=("direct",),
        execute=True,
    )
    validation_results = [
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=(
                f'generated_project_validation: command failed command=npm run build output_tail="failure-{attempt}"',
            ),
        )
        for attempt in range(3)
    ]
    validation_results.extend(
        (
            project_scale_runner_module._EvidenceCheck(
                passed=False,
                reasons=(
                    'generated_project_validation: command failed command=npm run build output_tail="failure-2"',
                ),
            ),
            project_scale_runner_module._EvidenceCheck(passed=True, reasons=()),
        )
    )

    def validate_generated_project_bundle(
        bundle: bytes | None, **kwargs: object
    ) -> project_scale_runner_module._EvidenceCheck:
        assert bundle is not None
        return validation_results.pop(0)

    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        validate_generated_project_bundle,
    )
    client = FakeAcceptanceClient(
        run_id="run-small-direct-transient-repair-failure",
        session_id="project-scale-small-direct",
        statuses=("completed", "completed", "completed", "failed", "completed"),
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "payload": {
                    "agent_standard_verification": {
                        "constraints_read": True,
                        "plan_before_implementation": True,
                        "reproducible_verification": True,
                        "root_cause_repair": True,
                    }
                },
            }
        ],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Task API\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Task API requirement satisfied\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": "- npm run build: passed\n- npm test: passed\n",
                "package.json": json.dumps(
                    {"scripts": {"build": "tsc", "test": "node --test"}}
                ),
                "src/app.ts": _functional_ts_source(),
                "tests/unit.test.ts": _functional_ts_test(),
            }
        ),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    assert len(client.submitted_bodies) == 5
    assert validation_results == []


def test_capability_repair_escalates_stalled_failure_to_authoritative_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=("small",),
        flows=("direct",),
        execute=True,
    )
    validation_results = [
        project_scale_runner_module._EvidenceCheck(
            passed=False,
            reasons=(
                f'generated_project_validation: command failed command=npm run build output_tail="failure-{attempt}"',
            ),
        )
        for attempt in range(3)
    ]
    validation_results.extend(
        (
            project_scale_runner_module._EvidenceCheck(
                passed=False,
                reasons=(
                    'generated_project_validation: command failed command=npm run build output_tail="failure-2"',
                ),
            ),
            project_scale_runner_module._EvidenceCheck(passed=True, reasons=()),
        )
    )

    def validate_generated_project_bundle(
        bundle: bytes | None, **kwargs: object
    ) -> project_scale_runner_module._EvidenceCheck:
        assert bundle is not None
        return validation_results.pop(0)

    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        validate_generated_project_bundle,
    )
    client = FakeAcceptanceClient(
        run_id="run-small-direct-stalled-repair",
        session_id="project-scale-small-direct",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[
            {
                "kind": "artifact.created",
                "payload": {
                    "agent_standard_verification": {
                        "constraints_read": True,
                        "plan_before_implementation": True,
                        "reproducible_verification": True,
                        "root_cause_repair": True,
                    }
                },
            }
        ],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Task API\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Task API requirement satisfied\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": "- npm run build: passed\n- npm test: passed\n",
                "package.json": json.dumps(
                    {"scripts": {"build": "tsc", "test": "node --test"}}
                ),
                "src/app.ts": _functional_ts_source(),
                "tests/unit.test.ts": _functional_ts_test(),
            }
        ),
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is True
    assert len(client.submitted_bodies) == 5
    assert client.submitted_bodies[-1]["replace_workspace_files"] is True
    assert validation_results == []


def test_typescript_overlap_repair_stops_at_dynamic_safety_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=("small",),
        flows=("direct",),
        execute=True,
    )
    typescript_failures = tuple(
        (
            f"src/repair-{index}.ts({index + 1},1): error TS2322: repair failure {index}"
            if index % 2 == 0
            else f"src/repair-{index}.ts:{index + 1}:1 - error TS2345: repair failure {index}"
        )
        for index in range(16)
    )
    validation_results = [
        _typescript_build_validation(
            "\n".join(
                (
                    typescript_failures[index],
                    typescript_failures[index + 1],
                    "Found 2 errors in 2 files.",
                )
            )
        )
        for index in range(15)
    ] + [project_scale_runner_module._EvidenceCheck(passed=True, reasons=())]

    def validate_generated_project_bundle(
        bundle: bytes | None, **kwargs: object
    ) -> project_scale_runner_module._EvidenceCheck:
        assert bundle is not None
        return validation_results.pop(0)

    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        validate_generated_project_bundle,
    )
    client = FakeAcceptanceClient(
        run_id="run-small-direct-safety-limit",
        session_id="project-scale-small-direct",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
    )

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    assert len(client.submitted_bodies) == 14
    assert len(validation_results) == 2
    assert (
        "deliverable_repair: dynamic safety limit exhausted after 13 attempts"
        in report.results[0].errors
    )


def test_deliverable_repair_idempotency_keys_reserve_attempt_suffix() -> None:
    evidence = {
        "workspace_bundle": False,
        "deliverable_quality": False,
        "agent_standard_verification": False,
        "discussion_trace": False,
        "plugin_contract": False,
        "multi_agent_participation": False,
        "self_repair_trace": False,
        "generated_project_validation": False,
        "requirements_validation": False,
    }
    safety_limit = project_scale_runner_module._deliverable_repair_safety_limit(
        "ultra:multi_agent",
        evidence=evidence,
    )
    keys = {
        project_scale_runner_module._deliverable_repair_idempotency_key(
            "ultra:multi_agent",
            99,
            execution_id="acceptance-" + "x" * 120,
            repair_attempt=attempt,
        )
        for attempt in range(1, safety_limit + 1)
    }

    assert len(keys) == safety_limit
    assert all(len(key) <= 90 for key in keys)
    assert any(key.endswith(f"-deliverable-repair-{safety_limit}") for key in keys)


def test_execute_project_scale_plan_rejects_unsafe_generated_project_zip_paths() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=_project_bundle(
            {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": (
                    "- npm run build: passed exit 0; node --check completed\n"
                    "- npm test: passed exit 0; 1 test passed\n"
                    "- interaction smoke: passed\n"
                ),
                "package.json": json.dumps({"scripts": {"build": "node --check src/main.js"}}),
                "src/main.js": _functional_js_source(),
                "tests/main.test.js": _functional_js_test(),
                "nested/../evil.js": "throw new Error('unsafe');\n",
            }
        ),
    )

    report = execute_project_scale_plan(
        plan,
        client,
        validate_generated_project=True,
        generated_project_commands=((sys.executable, "-c", "raise SystemExit(0)"),),
    )

    assert report.ok is False
    result = report.results[0]
    assert result.evidence["generated_project_validation"] is False
    assert result.errors == (
        "generated_project_validation: workspace bundle has unsafe path: nested/../evil.js",
    )


def test_generated_project_zip_path_validation_rejects_backslashes() -> None:
    with pytest.raises(RuntimeError, match=r"unsafe path"):
        _safe_zip_member_path(r"nested\evil.js")


@pytest.mark.parametrize("path", ("POST /orders/:id", "PATCH /fulfillment/jobs/:id"))
def test_generated_project_zip_path_validation_rejects_endpoint_titles(path: str) -> None:
    with pytest.raises(RuntimeError, match=r"unsafe path"):
        _safe_zip_member_path(path)


def test_workspace_bundle_agent_standard_requires_constraints_and_skill_rule_evidence() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": "- Read constraints\n- Build project\n",
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )

    assert "workspace_bundle: missing constraints and skill/rule reading evidence in implementation plan" in (
        _workspace_bundle_agent_standard_reasons(bundle)
    )


def test_workspace_bundle_agent_standard_rejects_generic_reading_claims() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": (
                "- Read before implementation: requirements and rules were reviewed.\n"
                "- Skills checked before implementation: applicable rules reviewed.\n"
                "- Build project\n"
            ),
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )

    assert "workspace_bundle: missing constraints and skill/rule reading evidence in implementation plan" in (
        _workspace_bundle_agent_standard_reasons(bundle)
    )


def test_workspace_bundle_agent_standard_rejects_partial_plan_reading_evidence() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": (
                "- Read before implementation: HANDOFF current-state index and "
                "PROJECT_REQUIREMENTS.md.\n"
                "- Rules checked before implementation: applicable runtime rules.\n"
                "- Build project\n"
            ),
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )

    assert "workspace_bundle: missing constraints and skill/rule reading evidence in implementation plan" in (
        _workspace_bundle_agent_standard_reasons(bundle)
    )


def test_workspace_bundle_agent_standard_rejects_generic_json_reading_evidence() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": "- Build project\n",
            "constraints_reading_evidence.json": json.dumps(
                {
                    "read_before_implementation": True,
                    "sources": ["requirements"],
                    "rules": ["general rules"],
                },
                sort_keys=True,
            ),
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )

    assert "workspace_bundle: missing constraints and skill/rule reading evidence in implementation plan" in (
        _workspace_bundle_agent_standard_reasons(bundle)
    )


def test_workspace_bundle_agent_standard_accepts_json_source_names_as_keys() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": "- Build project\n",
            "constraints_reading_evidence.json": json.dumps(
                {
                    "read_before_implementation": True,
                    "constraints": {
                        "AGENTS.md": {"kind": "workspace rules", "status": "read"},
                        "HANDOFF": {"kind": "handoff", "status": "read"},
                        "PROJECT_REQUIREMENTS.md": {"kind": "requirements", "status": "read"},
                    },
                    "skills": {
                        "SKILL.md": {
                            "kind": "agent-standard rules",
                            "status": "applied",
                        }
                    },
                },
                sort_keys=True,
            ),
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )

    assert _workspace_bundle_agent_standard_reasons(bundle) == ()


def test_workspace_bundle_agent_standard_accepts_combined_skill_rule_field() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": "- Build project\n",
            "constraints_reading_evidence.json": json.dumps(
                {
                    "read_before_implementation": True,
                    "constraints": [
                        "AGENTS.md workspace rules",
                        "HANDOFF.md current-state index",
                        "PROJECT_REQUIREMENTS.md requirements",
                    ],
                    "skills_or_agent_standard_rules": [
                        "SKILL.md applicable skill guidance",
                        "agent-standard rules",
                    ],
                },
                sort_keys=True,
            ),
            "VERIFICATION.md": "- No checks were executed in this environment.\n",
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )

    assert _workspace_bundle_agent_standard_reasons(bundle) == ()


def test_workspace_bundle_agent_standard_accepts_generated_skills_and_rules_field() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": "- Build project\n",
            "constraints_reading_evidence.json": json.dumps(
                {
                    "read_before_implementation": True,
                    "constraints": [
                        {"source": "AGENTS.md", "kind": "workspace_rules"},
                        {"source": "HANDOFF.md", "kind": "handoff_state"},
                        {
                            "source": "PROJECT_REQUIREMENTS.md",
                            "kind": "requirements",
                        },
                    ],
                    "skills_and_rules": [
                        {"source": "SKILL.md", "skill": "artifact-production"},
                        {"source": "agent-standard", "kind": "rules"},
                    ],
                },
                sort_keys=True,
            ),
            "VERIFICATION.md": "- No checks were executed in this environment.\n",
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )

    assert _workspace_bundle_agent_standard_reasons(bundle) == ()


def test_workspace_bundle_agent_standard_accepts_generated_skills_rules_field() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": "- Build project\n",
            "constraints_reading_evidence.json": json.dumps(
                {
                    "read_before_implementation": True,
                    "constraints": [
                        "AGENTS.md workspace rules",
                        "HANDOFF.md current-state index",
                        "PROJECT_REQUIREMENTS.md requirements",
                    ],
                    "skills_rules": [
                        "SKILL.md agent-standard TypeScript/Node API rules",
                        "workspace rules for artifact production",
                    ],
                },
                sort_keys=True,
            ),
            "VERIFICATION.md": "- No checks were executed in this environment.\n",
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )

    assert _workspace_bundle_agent_standard_reasons(bundle) == ()


def test_workspace_bundle_agent_standard_rejects_generic_skills_and_rules_alias() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": "- Build project\n",
            "constraints_reading_evidence.json": json.dumps(
                {
                    "read_before_implementation": True,
                    "constraints": [
                        "AGENTS.md workspace rules",
                        "HANDOFF.md current-state index",
                        "PROJECT_REQUIREMENTS.md requirements",
                    ],
                    "skills_and_rules": ["general guidance"],
                },
                sort_keys=True,
            ),
            "VERIFICATION.md": "- No checks were executed in this environment.\n",
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )

    assert "workspace_bundle: missing constraints and skill/rule reading evidence in implementation plan" in (
        _workspace_bundle_agent_standard_reasons(bundle)
    )


def test_agent_standard_verification_accepts_public_tool_event_evidence() -> None:
    bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": "- Read constraints\n- Build project\n",
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": _functional_js_source(),
            "tests/main.test.js": _functional_js_test(),
        }
    )
    event = {
        "kind": "tool.completed",
        "tool_name": "project.generate_zip",
        "payload": {
            "agent_standard_verification": {
                "constraints_read": True,
                "plan_before_implementation": True,
                "reproducible_verification": True,
                "root_cause_repair": True,
            },
        },
    }

    check = _evaluate_agent_standard_verification(None, [event], bundle, benchmark_kind="fixture")

    assert check.passed is True
    assert check.reasons == ()


def test_execute_project_scale_plan_rejects_package_only_shell_bundle() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    shell_bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "echo build passed", "test": "echo tests passed"}},
                sort_keys=True,
            ),
        }
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=shell_bundle,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is False
    assert "workspace_bundle: missing source files" in result.errors


def test_execute_project_scale_plan_rejects_source_bundle_without_test_files() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    shell_bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "echo build passed", "test": "echo tests passed"}},
                sort_keys=True,
            ),
            "src/main.js": "export const status = 'ready';\n",
        }
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=shell_bundle,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is False
    assert "workspace_bundle: missing test or verification file path" in result.errors


def test_execute_project_scale_plan_rejects_import_only_test_files() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    shell_bundle = _project_bundle(
        {
            "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
            "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
            "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
            "VERIFICATION.md": (
                "- npm run build: passed exit 0; vite build completed\n"
                "- npm test: passed exit 0; 1 test passed\n"
                "- interaction smoke: passed\n"
            ),
            "package.json": json.dumps(
                {"scripts": {"build": "node --check src/main.js", "test": "node --test"}},
                sort_keys=True,
            ),
            "src/main.js": "export const status = 'ready';\n",
            "tests/main.test.js": "import { status } from '../src/main.js';\n",
        }
    )
    client = FakeAcceptanceClient(
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        workspace_bundle=shell_bundle,
    )

    report = execute_project_scale_plan(plan, client)

    result = report.results[0]
    assert result.evidence["workspace_bundle"] is True
    assert result.evidence["deliverable_quality"] is False
    assert "workspace_bundle: missing meaningful test assertions" in result.errors


def test_execute_project_scale_plan_rejects_failed_terminal_status() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(status="failed", artifacts=[{"id": "artifact-1"}])

    report = execute_project_scale_plan(plan, client)

    assert report.ok is False
    result = report.results[0]
    assert result.status == "failed"
    assert result.evidence["terminal_status"] is True
    assert result.errors == ("terminal_status: failed",)


def test_execute_project_scale_plan_approves_large_project_preflight() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("large",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-large-direct",
        session_id="project-scale-large-direct",
        create_status="waiting_approval",
        decision_token="approve-large",
        decision_version=4,
        statuses=("queued", "completed"),
        artifacts=[{"id": "artifact-1"}],
    )

    report = execute_project_scale_plan(plan, client, wait_seconds=5, poll_interval_seconds=0)

    assert report.ok is True
    result = report.results[0]
    assert result.evidence["project_preflight_approval"] is True
    assert ("POST", "/api/v1/runs/run-large-direct/approve-project-preflight", None) in client.calls


def test_execute_project_scale_plan_approves_large_repair_project_preflight() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("large",), flows=("direct",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-large-direct",
        session_id="project-scale-large-direct",
        create_status="waiting_approval",
        decision_token="approve-large",
        decision_version=4,
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        deliverable_quality_sequence=(False, True),
        repair_create_status="waiting_approval",
        repair_decision_token="approve-large-repair",
        repair_decision_version=7,
    )

    report = execute_project_scale_plan(plan, client, wait_seconds=5, poll_interval_seconds=0)

    assert report.ok is True
    result = report.results[0]
    assert result.run_id == "run-large-direct-repair"
    assert result.evidence["project_preflight_approval"] is True
    assert (
        "POST",
        "/api/v1/runs/run-large-direct-repair/approve-project-preflight",
        None,
    ) in client.calls


def test_execute_project_scale_plan_approves_waiting_capability_tool() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("artifact_production",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-small-artifact",
        session_id="project-scale-small-artifact_production",
        statuses=("waiting_approval", "completed"),
        artifacts=[{"id": "artifact-1"}],
        capability_approval_id="approval_project_zip",
        capability_approval_version=3,
    )

    report = execute_project_scale_plan(
        plan,
        client,
        wait_seconds=5,
        poll_interval_seconds=0,
        auto_approve_capability_requests=True,
    )

    assert report.ok is True
    assert report.results[0].status == "completed"
    assert (
        "POST",
        "/api/v1/runs/run-small-artifact/approve-capability",
        None,
    ) in client.calls


def test_execute_project_scale_plan_can_wait_for_terminal_status() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("self_repair",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-small-self-repair",
        session_id="project-scale-small-self_repair",
        statuses=("queued", "completed"),
        artifacts=[{"id": "artifact-1"}],
        events=[{"kind": "runtime.self_repair.completed"}],
    )

    report = execute_project_scale_plan(plan, client, wait_seconds=5, poll_interval_seconds=0)

    assert report.ok is True
    assert report.results[0].status == "completed"
    assert report.results[0].evidence["terminal_status"] is True
    assert report.results[0].evidence["final_artifacts"] is True
    assert report.results[0].evidence["self_repair_trace"] is True
    assert client.calls.count(("GET", "/api/v1/runs/run-small-self-repair/details", None)) == 2


def test_execute_project_scale_plan_uses_terminal_event_when_details_stay_running() -> None:
    class EventTerminalClient(FakeAcceptanceClient):
        details_calls = 0

        def request_json(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, object] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object] | list[object]:
            if path == f"/api/v1/runs/{self.run_id}/details":
                self.details_calls += 1
                if self.details_calls > 1:
                    raise AssertionError("terminal event should stop details polling")
                return {
                    "id": self.run_id,
                    "status": "running",
                    "mode": "direct",
                    "artifacts": [{"id": "artifact-1"}],
                }
            if path == f"/api/v1/runs/{self.run_id}/events":
                return [
                    {
                        "kind": "terminal.notified",
                        "run_id": self.run_id,
                        "payload": {"status": "failed"},
                    }
                ]
            return super().request_json(
                method,
                path,
                body=body,
                idempotency_key=idempotency_key,
            )

    plan = build_project_scale_run_plan(
        benchmark_kind="fixture",
        scales=("small",),
        flows=("direct",),
        execute=True,
    )
    client = EventTerminalClient(
        run_id="run-small-direct-terminal-event",
        session_id="project-scale-small-direct",
        status="running",
        artifacts=[{"id": "artifact-1"}],
    )

    report = execute_project_scale_plan(
        plan,
        client,
        wait_seconds=5,
        poll_interval_seconds=0,
    )

    result = report.results[0]
    assert result.status == "failed"
    assert result.evidence["terminal_status"] is True
    assert client.details_calls == 1


def test_execute_project_scale_plan_repairs_missing_self_repair_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        project_scale_runner_module,
        "_validate_generated_project_bundle",
        lambda *args, **kwargs: project_scale_runner_module._EvidenceCheck(
            passed=True,
            reasons=(),
        ),
    )
    monkeypatch.setattr(
        project_scale_runner_module,
        "_evaluate_deliverable_quality",
        lambda *args, **kwargs: project_scale_runner_module._EvidenceCheck(
            passed=True,
            reasons=(),
        ),
    )
    monkeypatch.setattr(
        project_scale_runner_module,
        "_evaluate_agent_standard_verification",
        lambda *args, **kwargs: project_scale_runner_module._EvidenceCheck(
            passed=True,
            reasons=(),
        ),
    )
    monkeypatch.setattr(
        project_scale_runner_module,
        "_evaluate_discussion_trace",
        lambda *args, **kwargs: project_scale_runner_module._EvidenceCheck(
            passed=True,
            reasons=(),
        ),
    )
    plan = build_project_scale_run_plan(
        benchmark_kind="capability",
        scales=("small",),
        flows=("self_repair",),
        execute=True,
    )
    client = FakeAcceptanceClient(
        run_id="run-small-self-repair",
        session_id="project-scale-small-self_repair",
        status="completed",
        artifacts=[{"id": "artifact-1"}],
        events=[{"kind": "discussion.completed", "run_id": "run-small-self-repair"}],
    )

    report = execute_project_scale_plan(
        plan,
        client,
        wait_seconds=5,
        poll_interval_seconds=0,
    )

    result = report.results[0]
    assert report.ok is True
    assert result.run_id == "run-small-self-repair-repair"
    assert result.evidence["deliverable_repair_trace"] is True
    assert result.evidence["self_repair_trace"] is True
    assert result.missing_evidence == ()
    assert (
        "POST",
        "/api/v1/runs",
        "project-scale-small-self-repair-0-deliverable-repair",
    ) in client.calls
    repair_message = str(client.submitted_bodies[-1]["message"])
    assert "self_repair_trace: expected explicit fault-injection or repair evidence" in repair_message


def test_execute_project_scale_plan_cancels_running_capability_timeout_without_bundle_fetch() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="capability", scales=("medium",), flows=("hybrid",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-medium-hybrid",
        session_id="project-scale-medium-hybrid",
        status="running",
        events=[{"kind": "discussion.completed", "run_id": "run-medium-hybrid"}],
    )

    report = execute_project_scale_plan(plan, client, wait_seconds=0, poll_interval_seconds=0)

    result = report.results[0]
    assert result.status == "cancelled"
    assert result.evidence["run_details"] is True
    assert result.evidence["run_events"] is True
    assert result.evidence["terminal_status"] is False
    assert result.evidence["workspace_bundle"] is False
    assert result.evidence["cleanup_cancel"] is True
    assert (
        "run_observation: status running before terminal artifact collection"
        in result.errors
    )
    assert (
        "GET",
        "/api/v1/workspaces/projects/project-scale-acceptance/sessions/project-scale-medium-hybrid/bundle/download",
        None,
    ) not in client.calls
    assert ("POST", "/api/v1/runs/run-medium-hybrid/cancel", None) in client.calls


def test_execute_project_scale_plan_accepts_self_repair_proposal() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("dispatch",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-small-dispatch",
        session_id="project-scale-small-dispatch",
        statuses=("failed", "completed"),
        artifacts=[{"id": "artifact-1"}],
        events=[{"kind": "repair.classified", "run_id": "run-small-dispatch"}],
        self_repair_decision_token="repair-token-12345678901234567890",
        self_repair_decision_version=7,
        public_self_repair_proposal=False,
    )

    report = execute_project_scale_plan(plan, client, wait_seconds=5, poll_interval_seconds=0)

    assert report.ok is True
    result = report.results[0]
    assert result.run_id == "run-small-dispatch-repair"
    assert result.evidence["self_repair_trace"] is True
    assert result.evidence["deliverable_repair_trace"] is True
    assert result.evidence["deliverable_quality"] is True
    assert result.evidence["agent_standard_verification"] is True
    assert result.evidence["discussion_trace"] is True
    assert (
        "POST",
        "/api/v1/runs/run-small-dispatch/accept-repair",
        None,
    ) in client.calls
    assert result.errors == ()


def test_execute_project_scale_plan_repairs_failed_run_with_artifacts() -> None:
    plan = build_project_scale_run_plan(benchmark_kind="fixture", scales=("small",), flows=("dispatch",), execute=True)
    client = FakeAcceptanceClient(
        run_id="run-small-dispatch",
        session_id="project-scale-small-dispatch",
        statuses=("failed", "completed"),
        artifacts=[{"id": "artifact-1"}],
        discussion_trace_sequence=(False, True),
    )

    report = execute_project_scale_plan(plan, client, wait_seconds=5, poll_interval_seconds=0)

    assert report.ok is True
    result = report.results[0]
    assert result.run_id == "run-small-dispatch-repair"
    assert result.evidence["deliverable_repair_trace"] is True
    assert result.evidence["discussion_trace"] is True
    assert not any(error.startswith("terminal_status: failed") for error in result.errors)


def test_project_scale_execution_payload_lists_missing_evidence() -> None:
    result = ProjectScaleCaseResult(
        case_id="ultra:self_repair",
        run_id="run-ultra-self-repair",
        status="completed",
        evidence={
            "run_details": True,
            "run_events": True,
            "terminal_status": True,
            "final_artifacts": False,
            "deliverable_quality": False,
            "agent_standard_verification": False,
            "self_repair_trace": False,
            "project_preflight_approval": True,
            "workspace_bundle": True,
            "cleanup_cancel": True,
        },
    )

    payload = result.to_payload()

    assert payload["ok"] is False
    assert payload["required_evidence"] == [
        "run_details",
        "run_events",
        "terminal_status",
        "final_artifacts",
        "deliverable_quality",
        "agent_standard_verification",
        "project_preflight_approval",
        "workspace_bundle",
        "cleanup_cancel",
        "discussion_trace",
        "self_repair_trace",
    ]
    assert payload["missing_evidence"] == [
        "final_artifacts",
        "deliverable_quality",
        "agent_standard_verification",
        "discussion_trace",
        "self_repair_trace",
    ]


def test_project_scale_execution_text_line_lists_failed_case_diagnostics() -> None:
    result = ProjectScaleCaseResult(
        case_id="ultra:self_repair",
        run_id="run-ultra-self-repair",
        status="failed",
        evidence={
            "run_details": True,
            "run_events": True,
            "terminal_status": True,
            "project_preflight_approval": True,
            "workspace_bundle": True,
            "deliverable_quality": True,
            "agent_standard_verification": True,
            "cleanup_cancel": True,
        },
        validation_focus=(
            "interaction_stability",
            "final_result",
            "self_repair",
            "project_preflight_approval",
        ),
        errors=("terminal_status: failed",),
    )

    line = format_project_scale_result_line(result)

    assert line == (
        "ultra:self_repair run_id=run-ultra-self-repair ok=false "
        "focus=interaction_stability,final_result,self_repair,project_preflight_approval "
        "missing=final_artifacts,discussion_trace,self_repair_trace errors=1"
    )


class FakeAcceptanceClient:
    def __init__(
        self,
        *,
        fail_bundle: bool = False,
        fail_bundle_once: bool = False,
        run_id: str = "run-small-direct",
        session_id: str = "project-scale-small-direct",
        create_status: str | None = None,
        decision_token: str | None = None,
        decision_version: int | None = None,
        repair_create_status: str | None = None,
        repair_decision_token: str | None = None,
        repair_decision_version: int | None = None,
        status: str = "queued",
        statuses: tuple[str, ...] | None = None,
        artifacts: list[dict[str, object]] | None = None,
        admin_artifacts: list[dict[str, object]] | None = None,
        events: list[dict[str, object]] | None = None,
        repair_events: list[dict[str, object]] | None = None,
        events_envelope: bool = False,
        artifact_ids: list[str] | None = None,
        response_project_id: str | None = None,
        response_session_id: str | None = None,
        details_run_id: str | None = None,
        deliverable_quality: bool = True,
        deliverable_quality_sequence: tuple[bool, ...] | None = None,
        agent_standard: bool = True,
        agent_standard_sequence: tuple[bool, ...] | None = None,
        discussion_trace: bool = True,
        discussion_trace_sequence: tuple[bool, ...] | None = None,
        plugin_contract: bool = True,
        plugin_contract_sequence: tuple[bool, ...] | None = None,
        execution_evidence: bool = True,
        execution_evidence_sequence: tuple[bool, ...] | None = None,
        actual_mode: str | None = None,
        repair_actual_mode: str | None = None,
        capability_approval_id: str | None = None,
        capability_approval_version: int | None = None,
        self_repair_decision_token: str | None = None,
        self_repair_decision_version: int | None = None,
        public_self_repair_proposal: bool = True,
        workspace_bundle: bytes | None = None,
        repair_workspace_bundle: bytes | None = None,
        workspace_bundle_sequence: tuple[bytes | None, ...] | None = None,
        artifact_downloads: Mapping[str, bytes] | None = None,
        initial_route_evidence: Mapping[str, object] | None = None,
        repair_route_evidence: Mapping[str, object] | None = None,
    ) -> None:
        self.fail_bundle = fail_bundle
        self.fail_bundle_once = fail_bundle_once
        self.run_id = run_id
        self.session_id = session_id
        self.create_status = create_status
        self.decision_token = decision_token
        self.decision_version = decision_version
        self.repair_create_status = repair_create_status
        self.repair_decision_token = repair_decision_token
        self.repair_decision_version = repair_decision_version
        self.statuses = list(statuses or (status,))
        self.artifacts = artifacts or []
        self.admin_artifacts = admin_artifacts
        self.events = [{"kind": "run.created"}] if events is None else events
        self.repair_events = repair_events
        self.events_envelope = events_envelope
        self.artifact_ids = artifact_ids or []
        self.response_project_id = response_project_id
        self.response_session_id = response_session_id
        self.details_run_id = details_run_id
        self.deliverable_quality = deliverable_quality
        self.deliverable_quality_sequence = list(deliverable_quality_sequence or ())
        self.current_deliverable_quality = deliverable_quality
        self.agent_standard = agent_standard
        self.agent_standard_sequence = list(agent_standard_sequence or ())
        self.current_agent_standard = agent_standard
        self.discussion_trace = discussion_trace
        self.discussion_trace_sequence = list(discussion_trace_sequence or ())
        self.current_discussion_trace = discussion_trace
        self.plugin_contract = plugin_contract
        self.plugin_contract_sequence = list(plugin_contract_sequence or ())
        self.current_plugin_contract = plugin_contract
        self.execution_evidence = execution_evidence
        self.execution_evidence_sequence = list(execution_evidence_sequence or ())
        self.current_execution_evidence = execution_evidence
        self.actual_mode = actual_mode
        self.repair_actual_mode = repair_actual_mode
        self.capability_approval_id = capability_approval_id
        self.capability_approval_version = capability_approval_version
        self.self_repair_decision_token = self_repair_decision_token
        self.self_repair_decision_version = self_repair_decision_version
        self.public_self_repair_proposal = public_self_repair_proposal
        self.workspace_bundle = workspace_bundle
        self.repair_workspace_bundle = repair_workspace_bundle
        self.workspace_bundle_sequence = list(workspace_bundle_sequence or ())
        self.artifact_downloads = dict(artifact_downloads or {})
        self.initial_route_evidence = dict(initial_route_evidence or {})
        self.repair_route_evidence = dict(repair_route_evidence or {})
        self.repair_run_id = f"{run_id}-repair"
        self._collecting_repair_run = False
        self.calls: list[tuple[str, str, str | None]] = []
        self.submitted_bodies: list[dict[str, object]] = []

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]:
        self.calls.append((method, path, idempotency_key))
        if method == "POST" and path == "/api/v1/runs":
            assert body is not None
            self.submitted_bodies.append(dict(body))
            assert body["workspace_session_id"] == self.session_id
            is_repair = "deliverable-repair" in (idempotency_key or "")
            run_id = self.repair_run_id if is_repair else self.run_id
            response: dict[str, object] = {
                "id": run_id,
                "status": (
                    self.repair_create_status
                    if is_repair and self.repair_create_status is not None
                    else self.create_status or self.statuses[0]
                ),
                "project_id": self.response_project_id or body["project_id"],
                "workspace_session_id": self.response_session_id or body["workspace_session_id"],
                "mode": (
                    self.repair_actual_mode
                    if is_repair and self.repair_actual_mode is not None
                    else self.actual_mode or body["mode"]
                ),
            }
            if "conversation_id" in body:
                response["conversation_id"] = body["conversation_id"]
            response.update(
                self.repair_route_evidence if is_repair else self.initial_route_evidence
            )
            decision_token = self.repair_decision_token if is_repair else self.decision_token
            decision_version = self.repair_decision_version if is_repair else self.decision_version
            if decision_token is not None:
                response["decision_token"] = decision_token
            if decision_version is not None:
                response["version"] = decision_version
            return response
        if path == f"/api/v1/runs/{self.run_id}/approve-project-preflight":
            assert body == {
                "decision_token": self.decision_token,
                "version": self.decision_version,
            }
            return {"id": self.run_id, "status": self.statuses[0]}
        if path == f"/api/v1/runs/{self.repair_run_id}/approve-project-preflight":
            assert body == {
                "decision_token": self.repair_decision_token,
                "version": self.repair_decision_version,
            }
            return {"id": self.repair_run_id, "status": self.statuses[0]}
        if path == f"/api/v1/runs/{self.run_id}/approve-capability":
            assert body == {
                "approval_id": self.capability_approval_id,
                "version": self.capability_approval_version,
            }
            return {"id": self.run_id, "status": "queued", "version": self.capability_approval_version}
        if path == f"/api/v1/runs/{self.run_id}/accept-repair":
            assert body == {
                "decision_token": self.self_repair_decision_token,
                "version": self.self_repair_decision_version,
            }
            response = {
                "id": self.repair_run_id,
                "status": "queued",
                "project_id": self.submitted_bodies[-1]["project_id"],
                "workspace_session_id": self.submitted_bodies[-1]["workspace_session_id"],
                "mode": self.repair_actual_mode
                or self.actual_mode
                or self.submitted_bodies[-1]["mode"],
            }
            if "conversation_id" in self.submitted_bodies[-1]:
                response["conversation_id"] = self.submitted_bodies[-1]["conversation_id"]
            return response
        if path == f"/api/v1/admin/runs/{self.run_id}":
            admin_response: dict[str, object] = {
                "id": self.run_id,
                "status": self.statuses[0],
                "version": self.capability_approval_version,
                "artifacts": self.admin_artifacts if self.admin_artifacts is not None else [],
                "explicit_details": {
                    "approval_id": self.capability_approval_id,
                    "version": str(self.capability_approval_version),
                },
            }
            if self.self_repair_decision_token is not None:
                admin_response["decision_token"] = self.self_repair_decision_token
                admin_response["version"] = self.self_repair_decision_version or 1
                admin_response["repair_proposal"] = _self_repair_proposal_fixture()
            return admin_response
        if path in {
            f"/api/v1/runs/{self.run_id}/details",
            f"/api/v1/runs/{self.repair_run_id}/details",
        }:
            self._collecting_repair_run = path == f"/api/v1/runs/{self.repair_run_id}/details"
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            if path == f"/api/v1/runs/{self.repair_run_id}/details":
                details_run_id = self.repair_run_id
            else:
                details_run_id = self.details_run_id or self.run_id
            deliverable_quality = self._next_deliverable_quality()
            agent_standard = self._next_agent_standard()
            discussion_trace = self._next_discussion_trace()
            plugin_contract = self._next_plugin_contract()
            details_response: dict[str, object] = {
                "id": details_run_id,
                "status": status,
                "artifacts": self.artifacts,
                "artifact_ids": self.artifact_ids,
                "mode": (
                    self.repair_actual_mode
                    if path == f"/api/v1/runs/{self.repair_run_id}/details"
                    and self.repair_actual_mode is not None
                    else self.actual_mode or self.submitted_bodies[-1]["mode"]
                ),
            }
            details_response.update(
                self.repair_route_evidence
                if path == f"/api/v1/runs/{self.repair_run_id}/details"
                else self.initial_route_evidence
            )
            if deliverable_quality:
                details_response["deliverable_quality"] = {
                    "requirements_satisfied": True,
                    "build_passed": True,
                    "tests_passed": True,
                    "interactive_checks_passed": True,
                    "no_placeholders": True,
                    "artifact_integrity": True,
                }
            if agent_standard:
                details_response["agent_standard_verification"] = {
                    "constraints_read": True,
                    "plan_before_implementation": True,
                    "reproducible_verification": True,
                    "root_cause_repair": True,
                }
            if discussion_trace:
                details_response["discussion_trace"] = {
                    "participants": ["planner", "reviewer"],
                    "member_statements": [
                        {
                            "agent": "planner",
                            "summary": "proposed the implementation path and acceptance gates",
                        },
                        {
                            "agent": "reviewer",
                            "summary": "challenged missing verification evidence before approval",
                        },
                    ],
                    "disagreements": [
                        {
                            "topic": "verification depth",
                            "resolution": "run build, unit, and interaction checks before finalizing",
                        }
                    ],
                    "verification_steps": ["compare requirements", "inspect artifacts", "review tests"],
                    "final_decision": (
                        "planner and reviewer selected the implementation path after evidence review"
                    ),
                }
            if plugin_contract:
                details_response["plugin_contract"] = {
                    "manifest_discovered": True,
                    "adapter_contract_checked": True,
                    "policy_boundary_checked": True,
                    "sandbox_profile_checked": True,
                    "failure_recovery_checked": True,
                    "manifest_ref": "project-scale-plugin-manifest",
                    "adapter_ref": "project.generate_zip",
                    "policy_ref": "fail-closed plugin policy",
                    "sandbox_ref": "workspace_write",
                    "recovery_ref": "install/start failure recovery",
                }
            if (
                path == f"/api/v1/runs/{self.run_id}/details"
                and self.public_self_repair_proposal
            ):
                if self.self_repair_decision_token is not None:
                    details_response["decision_token"] = self.self_repair_decision_token
                if self.self_repair_decision_version is not None:
                    details_response["version"] = self.self_repair_decision_version
                if self.self_repair_decision_token is not None:
                    details_response["repair_proposal"] = _self_repair_proposal_fixture()
            if status == "waiting_approval" and self.capability_approval_id is not None:
                details_response["clarification_reason"] = "capability requires approval"
                details_response["approval_id"] = self.capability_approval_id
                details_response["version"] = self.capability_approval_version
            return details_response
        if path in {
            f"/api/v1/runs/{self.run_id}/events",
            f"/api/v1/runs/{self.repair_run_id}/events",
        }:
            events = list(
                self.repair_events
                if path == f"/api/v1/runs/{self.repair_run_id}/events"
                and self.repair_events is not None
                else self.events
            )
            if path == f"/api/v1/runs/{self.repair_run_id}/events":
                events = [
                    {
                        **event,
                        "run_id": self.repair_run_id,
                    }
                    if isinstance(event, dict) and event.get("run_id") == self.run_id
                    else event
                    for event in events
                ]
                events.append({"kind": "deliverable.repair.completed", "run_id": self.repair_run_id})
            if self.events_envelope:
                return {"items": events}
            return events
        if path in {
            f"/api/v1/runs/{self.run_id}/cancel",
            f"/api/v1/runs/{self.repair_run_id}/cancel",
        }:
            return {"id": self.run_id, "status": "cancelled"}
        raise AssertionError(f"unexpected JSON request {method} {path}")

    def request_bytes(self, method: str, path: str) -> bytes:
        self.calls.append((method, path, None))
        artifact_prefix = f"/api/v1/runs/{self.run_id}/artifacts/"
        if method == "GET" and path.startswith(artifact_prefix) and path.endswith("/download"):
            artifact_id = path[len(artifact_prefix) : -len("/download")]
            if artifact_id in self.artifact_downloads:
                return self.artifact_downloads[artifact_id]
            raise RuntimeError("artifact download unavailable")
        if self.fail_bundle:
            raise RuntimeError("workspace bundle unavailable")
        if self.fail_bundle_once:
            self.fail_bundle_once = False
            raise RuntimeError("workspace bundle unavailable")
        if self._collecting_repair_run and self.repair_workspace_bundle is not None:
            return self.repair_workspace_bundle
        if self.workspace_bundle_sequence:
            item = self.workspace_bundle_sequence.pop(0)
            if item is None:
                raise RuntimeError("workspace bundle unavailable")
            return item
        if self.workspace_bundle is not None:
            return self.workspace_bundle
        self._next_execution_evidence()
        if not self.current_deliverable_quality:
            return _project_bundle({"README.md": "placeholder project"})
        if not self.current_agent_standard:
            return _project_bundle(
                {
                    "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                    "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                    "package.json": json.dumps(
                        {"scripts": {"build": "vite build", "test": "vitest run"}},
                        sort_keys=True,
                    ),
                    "src/main.ts": _functional_ts_source(),
                    "tests/app.test.ts": _functional_ts_test(),
                }
            )
        verification = (
            "- npm run build: passed exit 0; vite build completed\n"
            "- npm test: passed exit 0; 1 test passed\n"
            "- interaction smoke: passed\n"
            if self.current_execution_evidence
            else "- npm run build\n- npm test\n- interaction smoke planned\n"
        )
        return _project_bundle(
            {
                "README.md": "# Acceptance Fixture\n\nImplements the requested project scope.\n",
                "PROJECT_REQUIREMENTS.md": "- Requirement satisfied\n- Interaction verified\n",
                "IMPLEMENTATION_PLAN.md": _AGENT_STANDARD_IMPLEMENTATION_PLAN,
                "VERIFICATION.md": verification,
                "package.json": json.dumps(
                    {"scripts": {"build": "vite build", "test": "vitest run"}},
                    sort_keys=True,
                ),
                "src/main.ts": _functional_ts_source(),
                "tests/app.test.ts": _functional_ts_test(),
            }
        )

    def _next_deliverable_quality(self) -> bool:
        if self.deliverable_quality_sequence:
            self.current_deliverable_quality = self.deliverable_quality_sequence.pop(0)
        else:
            self.current_deliverable_quality = self.deliverable_quality
        return self.current_deliverable_quality

    def _next_agent_standard(self) -> bool:
        if self.agent_standard_sequence:
            self.current_agent_standard = self.agent_standard_sequence.pop(0)
        else:
            self.current_agent_standard = self.agent_standard
        return self.current_agent_standard

    def _next_discussion_trace(self) -> bool:
        if self.discussion_trace_sequence:
            self.current_discussion_trace = self.discussion_trace_sequence.pop(0)
        else:
            self.current_discussion_trace = self.discussion_trace
        return self.current_discussion_trace

    def _next_plugin_contract(self) -> bool:
        if self.plugin_contract_sequence:
            self.current_plugin_contract = self.plugin_contract_sequence.pop(0)
        else:
            self.current_plugin_contract = self.plugin_contract
        return self.current_plugin_contract

    def _next_execution_evidence(self) -> bool:
        if self.execution_evidence_sequence:
            self.current_execution_evidence = self.execution_evidence_sequence.pop(0)
        else:
            self.current_execution_evidence = self.execution_evidence
        return self.current_execution_evidence


def _trusted_agent_standard_event() -> dict[str, object]:
    return {
        "kind": "artifact.created",
        "payload": {
            "agent_standard_verification": {
                "constraints_read": True,
                "plan_before_implementation": True,
                "reproducible_verification": True,
                "root_cause_repair": True,
            }
        },
    }


def _auto_scale_plan(
    scale: str, *, benchmark_kind: ProjectScaleBenchmarkKind = "fixture"
) -> ProjectScaleRunPlan:
    base = build_project_scale_run_plan(
        benchmark_kind=benchmark_kind,
        scales=(scale,),
        flows=("artifact_production",),
        execute=True,
    )
    request = base.requests[0]
    body = dict(request.body)
    body.update(
        {
            "mode": "auto",
            "workspace_session_id": f"project-scale-{scale}-auto",
        }
    )
    return ProjectScaleRunPlan(
        requests=(
            ProjectScaleRunRequest(
                case_id=f"{scale}:auto",
                body=body,
                validation_focus=request.validation_focus,
            ),
        ),
        required_evidence=base.required_evidence,
        cleanup_actions=base.cleanup_actions,
        dry_run=False,
        execute=True,
        requires_bearer_token=base.requires_bearer_token,
        benchmark_kind=base.benchmark_kind,
    )


class WaitingUserModeAcceptanceClient(FakeAcceptanceClient):
    def __init__(self, *, run_id: str, session_id: str, token_source: str) -> None:
        super().__init__(
            run_id=run_id,
            session_id=session_id,
            status="completed",
            artifacts=[{"id": "artifact-1"}],
        )
        self.token_source = token_source
        self.decision_token = "mode-decision-token-abcdefghijklmnopqrstuvwxyz"
        self.preflight_token = "preflight-token-abcdefghijklmnopqrstuvwxyz123"
        self.mode_chosen = False
        self.choose_mode_bodies: list[dict[str, object]] = []
        self.selected_mode: str | None = None

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]:
        if method == "POST" and path == "/api/v1/runs":
            submission_response = super().request_json(
                method,
                path,
                body=body,
                idempotency_key=idempotency_key,
            )
            assert isinstance(submission_response, dict)
            submission_response["status"] = (
                "waiting_user_mode" if self.token_source == "submission" else "queued"
            )
            submission_response["mode"] = None
            submission_response["version"] = 1
            if self.token_source == "submission":
                submission_response["decision_token"] = self.decision_token
            return submission_response
        if method == "POST" and path == f"/api/v1/runs/{self.run_id}/choose-mode":
            self.calls.append((method, path, idempotency_key))
            assert body is not None
            self.choose_mode_bodies.append(dict(body))
            self.mode_chosen = True
            self.selected_mode = cast(str, body["mode"])
            self.actual_mode = self.selected_mode
            scale = self.run_id.split("-", 2)[1]
            waiting_preflight = scale in {"large", "ultra"}
            choice_response: dict[str, object] = {
                "id": self.run_id,
                "status": "waiting_approval" if waiting_preflight else "queued",
                "mode": self.selected_mode,
                "version": 2,
                "project_id": self.submitted_bodies[-1]["project_id"],
                "workspace_session_id": self.submitted_bodies[-1]["workspace_session_id"],
            }
            if waiting_preflight:
                choice_response["decision_token"] = self.preflight_token
            return choice_response
        if method == "POST" and path == f"/api/v1/runs/{self.run_id}/approve-project-preflight":
            self.calls.append((method, path, idempotency_key))
            assert body == {"decision_token": self.preflight_token, "version": 2}
            return {"id": self.run_id, "status": "queued", "mode": self.selected_mode}
        if (
            method == "GET"
            and path == f"/api/v1/runs/{self.run_id}/details"
            and not self.mode_chosen
        ):
            self.calls.append((method, path, idempotency_key))
            details_response: dict[str, object] = {
                "id": self.run_id,
                "status": "waiting_user_mode",
                "mode": None,
                "version": 2,
            }
            if self.token_source == "details":
                details_response["decision_token"] = self.decision_token
            return details_response
        return super().request_json(
            method,
            path,
            body=body,
            idempotency_key=idempotency_key,
        )


class WaitingModeThenCapabilityApprovalClient(WaitingUserModeAcceptanceClient):
    def __init__(self, *, run_id: str, session_id: str) -> None:
        super().__init__(run_id=run_id, session_id=session_id, token_source="submission")
        self.statuses = ["waiting_approval", "waiting_approval", "completed"]
        self.capability_approval_id = "approval-project-generate-zip"
        self.capability_approval_version = 3
        self.events = [
            {
                "kind": "approval.requested",
                "run_id": run_id,
                "approval_id": self.capability_approval_id,
            }
        ]
        self.capability_approval_bodies: list[dict[str, object]] = []

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]:
        if method == "GET" and path == f"/api/v1/admin/runs/{self.run_id}":
            raise AssertionError("real-user capability approval must not use admin run data")
        if method == "POST" and path == f"/api/v1/runs/{self.run_id}/approve-capability":
            assert body is not None
            self.capability_approval_bodies.append(dict(body))
        response = super().request_json(
            method,
            path,
            body=body,
            idempotency_key=idempotency_key,
        )
        if (
            method == "GET"
            and path == f"/api/v1/runs/{self.run_id}/details"
            and isinstance(response, dict)
            and response.get("status") == "waiting_approval"
        ):
            response["clarification_reason"] = "capability requires approval"
            response["decision_token"] = "capability-decision-token-public-details"
            response["version"] = self.capability_approval_version
            response.pop("approval_id", None)
        return response


def _project_bundle(files: dict[str, str]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def _functional_js_source() -> str:
    return (
        "export function formatGreeting(name) {\n"
        "  const value = String(name || '').trim();\n"
        "  if (!value) return 'Hello, guest';\n"
        "  return `Hello, ${value}`;\n"
        "}\n"
    )


def _functional_js_test() -> str:
    return (
        "import assert from 'node:assert/strict';\n"
        "import { formatGreeting } from '../src/main.js';\n"
        "assert.equal(formatGreeting(' Ada '), 'Hello, Ada');\n"
        "assert.equal(formatGreeting(''), 'Hello, guest');\n"
    )


def _functional_ts_source() -> str:
    return (
        "export function formatGreeting(name: string | undefined): string {\n"
        "  const value = String(name || '').trim();\n"
        "  if (!value) return 'Hello, guest';\n"
        "  return `Hello, ${value}`;\n"
        "}\n"
    )


def _functional_ts_test() -> str:
    return (
        "import { formatGreeting } from '../src/main';\n"
        "expect(formatGreeting(' Ada ')).toBe('Hello, Ada');\n"
        "expect(formatGreeting('')).toBe('Hello, guest');\n"
    )


def _self_repair_proposal_fixture() -> dict[str, object]:
    return {
        "kind": "self_repair",
        "failure_kind": "runtime_failure",
        "repair_action": "draft_repair_proposal",
        "requires_approval": True,
        "automatic_execution": False,
        "recovery_strategy": "retry_blocked_contract_chain_after_replanning",
        "orchestration_recovery_hint": "retry_blocked_contract_chain",
    }
