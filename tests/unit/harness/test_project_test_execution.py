from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Mapping, Sequence
from io import BytesIO
from pathlib import Path

import pytest

from agent_hub.harness import project_scale_runner as runner
from agent_hub.harness import project_test_execution as proof
from agent_hub.harness.project_scale import ProjectScaleRunPlan, ProjectScaleRunRequest

_NODE = shutil.which("node")


@pytest.fixture(autouse=True)
def _local_windows_node_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Native proof uses Linux namespaces; this Windows-only own fixture needs the OS root.
    if os.name == "nt":
        monkeypatch.setattr(proof, "_PROCESS_ENV", {
            "PATH": "/usr/bin:/bin", "SystemRoot": os.environ["SystemRoot"],
        })


def _bundle(script: str, test_source: str) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("package.json", json.dumps({"scripts": {"test": script}}))
        archive.writestr("test/main.test.cjs", test_source)
    return output.getvalue()


def _own_node_command(
    root: Path, command: Sequence[str], reporter: Path, config: Mapping[str, str]
) -> list[str]:
    assert _NODE is not None
    assert root.is_dir()
    assert command[:2] == ("node", "--test")
    return [_NODE, "--test", f"--test-reporter={reporter.as_uri()}", *command[2:]]


def _isolate_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    # These unrelated gates are held healthy; project commands are never installed/run here.
    monkeypatch.setattr(runner, "_run_generated_project_command", lambda *args, **kwargs: None)
    for name in (
        "validate_small_task_api", "validate_medium_crm_api",
        "validate_large_order_ops_api", "validate_ultra_portfolio_api",
    ):
        monkeypatch.setattr(runner, name, lambda *args, **kwargs: ())
    monkeypatch.setattr(runner, "scale_validation_profile", lambda scale: None)
    monkeypatch.setattr(proof, "_test_command", _own_node_command)


@pytest.mark.parametrize("scale", ("small", "medium", "large", "ultra"))
@pytest.mark.parametrize("mode", ("noop", "load_only"))
def test_capability_does_not_credit_noop_or_module_load_only(
    monkeypatch: pytest.MonkeyPatch, scale: str, mode: str
) -> None:
    _isolate_runner(monkeypatch)
    script, source = (
        ("node -e \"process.exit(0)\"", "require('node:assert/strict').fail('own unused failure');")
        if mode == "noop" else
        ("node --test", "process.exit(0); require('node:assert/strict').fail('own unused failure');")
    )
    result = runner._validate_generated_project_bundle(
        _bundle(script, source), commands=(("npm", "test"),), timeout_seconds=10,
        absolute_deadline=time.monotonic() + 10, requirements_case_id=f"{scale}:auto",
    )
    assert not result.passed
    assert any(reason.startswith("requirements: test execution") for reason in result.reasons)
    assert runner._generated_project_validation_is_repairable(result)
    if mode == "load_only":
        assert result.reasons == (proof._Reason.NO_TESTS.value,)


def test_generic_benchmark_keeps_existing_command_only_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_runner(monkeypatch)
    result = runner._validate_generated_project_bundle(
        _bundle("node -e \"process.exit(0)\"", "throw Error('own unused failure');"),
        commands=(("npm", "test"),), timeout_seconds=10,
    )
    assert result.passed


def test_local_builtin_reporter_distinguishes_test_body_from_file_wrapper(tmp_path: Path) -> None:
    assert _NODE is not None, "a local Node executable is required for the own fixture"
    (tmp_path / "test").mkdir()
    (tmp_path / "test/main.test.cjs").write_text(
        "const { test } = require('node:test');\n"
        "test('own passing body', () => {});\n"
        "test('own failing body', () => { require('node:assert/strict').fail('own-body'); });\n",
        encoding="utf-8",
    )
    reporter = tmp_path / "reporter.cjs"
    reporter.write_text(
        "module.exports = async function* (events) { for await (const e of events) {\n"
        "if (e.type === 'test:pass' || e.type === 'test:fail') { const d = e.data;\n"
        "yield JSON.stringify({pass:e.type==='test:pass', name:d.name, file:!!d.file,\n"
        "line:d.line, column:d.column, type:d.details?.type,\n"
        "failure:d.details?.error?.failureType, code:d.details?.error?.cause?.code,\n"
        "body:d.details?.error?.cause?.message==='own-body'})+'\\n'; } }};\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [_NODE, "--test", f"--test-reporter={reporter.as_uri()}"], cwd=tmp_path,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=10,
    )
    assert completed.returncode != 0
    events = [json.loads(line) for line in completed.stdout.splitlines()]
    passed = next(event for event in events if event["name"] == "own passing body")
    failed = next(event for event in events if event["name"] == "own failing body")
    assert passed["pass"] and passed["file"] and passed["line"] > 1
    assert failed["body"] and failed["code"] == "ERR_ASSERTION"
    assert failed["failure"] == "testCodeFailure"


def _project(tmp_path: Path, *, script: str = "node --test", source: str | None = None) -> Path:
    root = tmp_path / "original"
    (root / "test").mkdir(parents=True)
    (root / "node_modules/owned-fixture").mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps({"scripts": {"test": script}}), encoding="utf-8"
    )
    (root / "node_modules/owned-fixture/index.js").write_text(
        "exports.add = (a, b) => a + b;\n", encoding="utf-8"
    )
    (root / "test/main.test.cjs").write_text(
        source if source is not None else (
            "const {test} = require('node:test');\n"
            "test('own named addition', () => {\n"
            "require('node:assert/strict').equal(require('owned-fixture').add(1,2), 3);\n"
            "require('node:fs').writeFileSync('own-test-state', 'written only in clone');\n"
            "});\n"
        ), encoding="utf-8",
    )
    return root


def _verify(root: Path, *, timeout: float = 10) -> tuple[str, ...]:
    return proof.verify_project_test_execution(
        root, timeout_seconds=timeout, absolute_deadline=time.monotonic() + timeout, config={},
    )


def test_trusted_reporter_emits_one_structured_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    reporter = tmp_path / "trusted-reporter.cjs"
    reporter.write_text(proof._reporter_source("own-name", "own-body", "own-file"), encoding="utf-8")
    monkeypatch.setattr(proof, "_test_command", _own_node_command)
    code, report = proof._run_report(root, ("node", "--test"), reporter, {}, time.monotonic() + 10)
    assert code == 0
    assert report == {"schema": 1, "passed": 1, "failed": 0, "canary": False}


@pytest.mark.parametrize("script", (
    "node --test", "node --test test/main.test.cjs", "node --test test/*.test.cjs",
))
def test_actual_named_body_and_failing_canary_use_existing_dependencies_only_in_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: str
) -> None:
    root = _project(tmp_path, script=script)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    copies: list[Path] = []

    def own_command(
        copied: Path, command: Sequence[str], reporter: Path, config: Mapping[str, str]
    ) -> list[str]:
        copies.append(copied)
        assert copied != root and reporter.parent != copied
        assert not os.path.samefile(
            copied / "node_modules/owned-fixture/index.js", root / "node_modules/owned-fixture/index.js"
        )
        return _own_node_command(copied, command, reporter, config)

    monkeypatch.setattr(proof, "_test_command", own_command)
    assert _verify(root) == ()
    assert len(copies) == 2
    assert all(not copied.exists() for copied in copies)
    assert before == {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("mutant", ("load_only", "top_level_throw", "syntax_failure", "skipped"))
def test_module_loading_compile_failure_or_skipped_canary_cannot_prove_test_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutant: str
) -> None:
    root = _project(tmp_path)
    calls = 0

    def own_command(
        copied: Path, command: Sequence[str], reporter: Path, config: Mapping[str, str]
    ) -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            canary = next((copied / "test").glob("agent-hub-*.test.cjs"))
            lines = canary.read_text(encoding="utf-8").splitlines()
            name = json.loads(lines[0].removeprefix("require('node:test').test(").split(", () =>")[0])
            body = json.loads(lines[1].removeprefix("require('node:assert/strict').fail(").removesuffix(");"))
            source = {
                "load_only": "process.exit(0);",
                "top_level_throw": "throw Object.assign(new Error(" + json.dumps(body)
                + "), {code:'ERR_ASSERTION'});",
                "syntax_failure": "this is not valid javascript;",
                "skipped": "require('node:test').test(" + json.dumps(name)
                + ", {skip:true}, () => {require('node:assert/strict').fail(" + json.dumps(body) + ");});",
            }[mutant]
            canary.write_text(source, encoding="utf-8")
        return _own_node_command(copied, command, reporter, config)

    monkeypatch.setattr(proof, "_test_command", own_command)
    assert _verify(root) == (proof._Reason.CANARY.value,)
    assert calls == 2


@pytest.mark.parametrize("source", (
    "process.stdout.write('all tests passed, 999 passed');",
    "require('node:test').test('own skipped', {skip:true}, () => {throw Error('private');});",
    "require('node:test').test.todo('own todo');",
))
def test_output_claims_skips_and_todo_do_not_count_as_executed_tests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str
) -> None:
    monkeypatch.setattr(proof, "_test_command", _own_node_command)
    assert _verify(_project(tmp_path, source=source)) == (proof._Reason.NO_TESTS.value,)


def test_baseline_failure_exports_only_fixed_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _project(tmp_path, source=(
        "require('node:test').test('own failure', () => {\n"
        "require('node:assert/strict').fail('OWN_PRIVATE_ERROR_NOT_EXPORTED');});\n"
    ))
    monkeypatch.setattr(proof, "_test_command", _own_node_command)
    assert _verify(root) == (proof._Reason.BASELINE.value,)


@pytest.mark.parametrize("script", ("node test/main.test.cjs", "jest --json", "vitest run", "echo passed"))
def test_unsupported_runners_are_honest_repairable_no_credit(
    tmp_path: Path, script: str
) -> None:
    reasons = _verify(_project(tmp_path, script=script))
    assert reasons == (proof._Reason.UNSUPPORTED.value,)
    assert "node:test" in reasons[0] and "node --test" in reasons[0]
    assert runner._generated_project_validation_is_repairable(
        runner._EvidenceCheck(passed=False, reasons=reasons)
    )


def test_expired_deadline_starts_no_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def forbidden(*args: object, **kwargs: object) -> list[str]:
        pytest.fail("expired proof must not start a process")

    monkeypatch.setattr(proof, "_test_command", forbidden)
    assert _verify(_project(tmp_path), timeout=0) == (proof._Reason.DEADLINE.value,)


def test_cleanup_failure_is_fail_closed_after_an_otherwise_valid_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_cleanup = tempfile.TemporaryDirectory.cleanup

    def failed_cleanup(temporary: tempfile.TemporaryDirectory[str]) -> None:
        original_cleanup(temporary)
        raise OSError("OWN_PRIVATE_CLEANUP_ERROR")

    monkeypatch.setattr(proof, "_test_command", _own_node_command)
    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", failed_cleanup)
    assert _verify(_project(tmp_path)) == (proof._Reason.CLEANUP.value,)


def test_actual_sandbox_binding_protects_reporter_and_canary_without_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    reporter = tmp_path / "agent-hub-own.reporter.cjs"
    reporter.write_text("own", encoding="utf-8")
    canary = root / "test/agent-hub-own.test.cjs"
    canary.write_text("own", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def generated(
        command: Sequence[str], *, cwd: Path, config: Mapping[str, str],
    ) -> list[str]:
        assert cwd == root and config == {}
        calls.append(tuple(command))
        return ["bwrap", "--unshare-net", "--bind", str(root), "/workspace", "--", *command]

    monkeypatch.setattr(proof, "generated_command", generated)
    argv = proof._test_command(root, ("node", "--test"), reporter, {})
    boundary = argv.index("--")
    assert argv[boundary - 6:boundary] == [
        "--ro-bind", str(reporter), proof._REPORTER_PATH,
        "--ro-bind", str(canary), "/workspace/test/agent-hub-own.test.cjs",
    ]
    assert "--unshare-net" in argv[:boundary]
    assert calls == [("node", "--test", f"--test-reporter=file://{proof._REPORTER_PATH}")]


@pytest.mark.parametrize("limit", ("bytes", "entries"))
def test_disposable_copy_budget_failure_starts_no_test_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, limit: str,
) -> None:
    root = _project(tmp_path)
    monkeypatch.setattr(proof, "_COPY_MAX_BYTES" if limit == "bytes" else "_COPY_MAX_ENTRIES", 1)

    def forbidden(*args: object, **kwargs: object) -> list[str]:
        pytest.fail("copy budget rejection must not execute project tests")

    monkeypatch.setattr(proof, "_test_command", forbidden)
    assert _verify(root) == (proof._Reason.COPY.value,)


def test_original_hardlink_is_copied_not_shared(tmp_path: Path) -> None:
    root = _project(tmp_path)
    original = root / "node_modules/owned-fixture/index.js"
    alias = root / "own-alias.js"
    os.link(original, alias)
    copied = tmp_path / "copy"
    proof._copy_project(root, copied, time.monotonic() + 10)
    assert not os.path.samefile(copied / "own-alias.js", original)
    (copied / "own-alias.js").write_text("mutated clone only", encoding="utf-8")
    assert original.read_text(encoding="utf-8") == "exports.add = (a, b) => a + b;\n"


@pytest.mark.parametrize("outside", (False, True))
def test_copy_dereferences_only_internal_symlinks(tmp_path: Path, outside: bool) -> None:
    root = _project(tmp_path)
    target = tmp_path / "own-outside.js" if outside else root / "own-inside.js"
    target.write_text("own file", encoding="utf-8")
    try:
        (root / "own-link.js").symlink_to(target)
    except OSError:
        pytest.skip("local OS disallows own symlink creation; native fixture must cover this")
    copied = tmp_path / "copy"
    if outside:
        with pytest.raises(proof._ProofFailure) as failure:
            proof._copy_project(root, copied, time.monotonic() + 10)
        assert failure.value.reason is proof._Reason.COPY
    else:
        proof._copy_project(root, copied, time.monotonic() + 10)
        assert not (copied / "own-link.js").is_symlink()
        assert not os.path.samefile(copied / "own-link.js", target)


def test_copy_and_both_test_runs_share_one_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    deadlines: list[float] = []
    original_copy = proof._copy_project

    def copy(root: Path, destination: Path, deadline: float) -> None:
        deadlines.append(deadline)
        original_copy(root, destination, deadline)

    def report(
        root: Path, command: Sequence[str], reporter: Path, config: Mapping[str, str],
        deadline: float,
    ) -> tuple[int, dict[str, object]]:
        deadlines.append(deadline)
        if len(deadlines) == 2:
            return 0, {"schema": 1, "passed": 1, "failed": 0, "canary": False}
        raise proof._ProofFailure(proof._Reason.DEADLINE)

    monkeypatch.setattr(proof, "_copy_project", copy)
    monkeypatch.setattr(proof, "_run_report", report)
    assert _verify(_project(tmp_path)) == (proof._Reason.DEADLINE.value,)
    assert len(deadlines) == 3 and len(set(deadlines)) == 1


@pytest.mark.parametrize("scale", ("small", "medium", "large", "ultra"))
def test_capability_guidance_requires_named_builtin_bodies_without_mutating_plan(scale: str) -> None:
    body: dict[str, object] = {"message": "own project request", "model_profile": "own-default"}
    result = runner._capability_test_execution_body(
        body, case_id=f"{scale}:auto", benchmark_kind="capability",
    )
    assert body == {"message": "own project request", "model_profile": "own-default"}
    assert result["model_profile"] == "own-default"
    message = str(result["message"])
    assert message.startswith("own project request")
    assert "node:test" in message and "node --test" in message
    assert "named" in message and "no-op" in message


@pytest.mark.parametrize("case_id,kind", (
    ("small:auto", "fixture"), ("medium:auto", "fixture"),
    ("large:auto", "fixture"), ("ultra:auto", "fixture"), ("unknown:auto", "capability"),
))
def test_generic_benchmark_does_not_receive_new_test_contract(case_id: str, kind: str) -> None:
    body: dict[str, object] = {"message": "own request"}
    assert runner._capability_test_execution_body(
        body, case_id=case_id, benchmark_kind=kind,
    ) == body


def test_stdout_flood_is_stopped_at_capture_budget_not_written_to_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    def command(
        root: Path, command: Sequence[str], reporter: Path, config: Mapping[str, str],
    ) -> list[str]:
        return [sys.executable, "-c", "import os\nwhile True: os.write(1, b'x' * 65536)"]

    monkeypatch.setattr(proof, "_test_command", command)

    def disk_output_forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("stdout capture must not use a temporary output file")

    monkeypatch.setattr(tempfile, "TemporaryFile", disk_output_forbidden)
    with pytest.raises(proof._ProofFailure) as failure:
        proof._run_report(tmp_path, ("node", "--test"), tmp_path / "own", {}, time.monotonic() + 5)
    assert failure.value.reason is proof._Reason.REPORT


def test_hanging_stdout_is_stopped_by_shared_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    def command(
        root: Path, command: Sequence[str], reporter: Path, config: Mapping[str, str],
    ) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    monkeypatch.setattr(proof, "_test_command", command)
    with pytest.raises(proof._ProofFailure) as failure:
        proof._run_report(tmp_path, ("node", "--test"), tmp_path / "own", {}, time.monotonic() + 0.2)
    assert failure.value.reason is proof._Reason.DEADLINE


def test_real_execute_entry_binds_capability_guidance_before_initial_submission() -> None:
    bodies: list[dict[str, object]] = []

    class OwnClient:
        def request_json(
            self, method: str, path: str, *, body: dict[str, object] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, object] | list[object]:
            assert method == "POST" and path == "/api/v1/runs" and body is not None
            bodies.append(dict(body))
            raise RuntimeError("own submission deliberately stopped before any execution")

        def request_bytes(self, method: str, path: str) -> bytes:
            pytest.fail("this own binding fixture does not fetch any artifact")

    body: dict[str, object] = {"message": "own original", "mode": "auto"}
    plan = ProjectScaleRunPlan(
        requests=(ProjectScaleRunRequest("small:auto", body, ()),), benchmark_kind="capability",
    )
    runner.execute_project_scale_plan(plan, OwnClient())
    assert len(bodies) == 1 and "node:test" in str(bodies[0]["message"])
    assert body == {"message": "own original", "mode": "auto"}
    assert "runtime_timeout_seconds" not in bodies[0]


@pytest.mark.parametrize("load_only", (False, True))
def test_node_22_events_without_type_still_require_actual_named_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, load_only: bool,
) -> None:
    root = _project(tmp_path, source=(
        "process.exit(0);" if load_only else
        "require('node:test').test('own legacy body', () => {\n"
        "require('node:assert/strict').equal(2 + 3, 5);});\n"
    ))

    def legacy_command(
        copied: Path, command: Sequence[str], reporter: Path, config: Mapping[str, str],
    ) -> list[str]:
        preserved = reporter.with_name("preserved-" + reporter.name)
        if not preserved.exists():
            preserved.write_bytes(reporter.read_bytes())
            reporter.write_text(
                "const forward = require(" + json.dumps(str(preserved)) + ");\n"
                "module.exports = async function* (events) {\n"
                "async function* legacy() {for await (const e of events) {\n"
                "if (e.data?.details) { const details = {...e.data.details}; delete details.type;\n"
                "yield {...e, data:{...e.data, details}}; } else yield e; }}\n"
                "yield* forward(legacy()); };\n", encoding="utf-8",
            )
        return _own_node_command(copied, command, reporter, config)

    monkeypatch.setattr(proof, "_test_command", legacy_command)
    expected = (proof._Reason.NO_TESTS.value,) if load_only else ()
    assert _verify(root) == expected
