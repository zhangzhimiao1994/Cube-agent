"""Generated Node wrapper wiring; native bwrap/process boundaries stay explicitly fake."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from agent_hub.capabilities.project_contracts import build_project_check_contract
from agent_hub.skills.sandbox import project as broker
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy
from tests.unit.skills.sandbox.test_project_contract_runner import native_launch_boundary

SOURCE = Path(__file__).resolve().parents[4] / "src/agent_hub/skills/sandbox/project_runner.py"


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def raw_manifest() -> bytes:
    return encoded({"schema_version": 1, "stack": "node", "editable_paths": ["src"],
        "test_files": ["tests/acceptance.cjs"],
        "criteria": [{"id": "AC-001", "description": "Returns the requested total", "tests": ["total"]}]})


def helper_report(**changes: Any) -> str:
    return json.dumps({"schema_version": 1, "success": True, "passed_tests": ["total"],
                       "failed": 0, "skipped": 0, "todo": 0, "cancelled": 0, **changes})


def invocation(source_kind: str = "candidate") -> broker.ContractProjectInvocation:
    return broker.ContractProjectInvocation.model_validate({
        "execution_id": "generated_1", "tenant_id": UUID(int=1), "run_id": UUID(int=2),
        "project_id": "greenfield", "session_id": "one", "sandbox_profile": "workspace_write",
        "install_allowed": True, "timeout_seconds": 30, "source_kind": source_kind,
        "original_reference": {"schema_version": 1, "contract_sha256": "a" * 64,
                               "input_source_sha256": "b" * 64, "file_count": 4}}, strict=True)


@pytest.fixture
def runner() -> Any:
    spec = importlib.util.spec_from_file_location("generated_project_runner_test", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def case(tmp_path: Path) -> Iterator[Any]:
    original, candidate = tmp_path / "original", tmp_path / "candidate"
    original.mkdir()
    files = {"package.json": b'{"scripts":{"build":"node build.cjs","test":"echo fake-success"}}',
             "ACCEPTANCE.json": raw_manifest(), "tests/acceptance.cjs": b"// never run on host\n",
             "src/app.cjs": b"module.exports = 1;\n"}
    for name, raw in files.items():
        target = original / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    shutil.copytree(original, candidate)
    observed = {name: hashlib.sha256(b"f\0" + raw).hexdigest() for name, raw in files.items()}
    contract = build_project_check_contract(UUID(int=1), UUID(int=2), "greenfield", "one",
                                           hashlib.sha256(encoded(observed)).hexdigest(), ("src",), observed)
    control = tmp_path / "contract.json"
    control.write_bytes(encoded(contract.to_payload()))
    control.chmod(0o444)
    for path in original.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    original.chmod(0o555)
    try:
        yield SimpleNamespace(original=original, input=candidate, work=tmp_path / "work",
            control=control, contract=contract, files=observed, raw=files["ACCEPTANCE.json"],
            manifest_sha=hashlib.sha256(files["ACCEPTANCE.json"]).hexdigest())
    finally:
        control.chmod(0o600)
        original.chmod(0o700)
        for path in original.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)


def invoke(runner: Any, case: Any, *, generated: bool = True, **changes: Any) -> Any:
    options = {"check_contract": case.control, "original_input": case.original,
               "expected_contract_sha256": case.contract.protected_sha256, "install_allowed": True}
    if generated:
        options["generated_manifest_sha256"] = case.manifest_sha
    options.update(changes)
    return runner.run_project(case.input, case.work, 15, **options)


def runtime(runner: Any, monkeypatch: pytest.MonkeyPatch, *, raw: str | None = None,
            exit_code: int = 0, timed_out: bool = False, limited: bool = False) -> list[tuple[str, ...]]:
    commands = native_launch_boundary(runner, monkeypatch)
    original = runner._run_bounded
    def launch(command: Any, **kwargs: Any) -> Any:
        result = original(command, **kwargs)
        if "--test" in command:
            return runner.ProcessResult(exit_code, timed_out, limited,
                helper_report() if raw is None else raw, "private child text")
        return result
    monkeypatch.setattr(runner, "_run_bounded", launch)
    return commands


def test_root_generated_flag_reaches_protected_runner_cli_once() -> None:
    args = broker.build_project_systemd_command(invocation(), BrokerPolicy(), Path("/root/snapshot"),
        contract_path=Path("/root/contract.json"), original_snapshot=Path("/root/original"),
        contract_sha256="c" * 64, generated_manifest_sha256="d" * 64)
    assert args.count("--generated-manifest-sha256") == 1
    assert args[args.index("--generated-manifest-sha256") + 1] == "d" * 64
    assert "--check-contract" in args and "--original-input" in args
    assert "CapabilityBoundingSet=" in args and "ProtectSystem=strict" in args


@pytest.mark.parametrize("fault", ["original", "no_contract", "uppercase", "short", "bool"])
def test_root_flag_cannot_escape_candidate_contract_or_digest_validation(fault: str) -> None:
    item = invocation("original" if fault == "original" else "candidate")
    options: dict[str, Any] = {"contract_path": Path("/root/contract.json"),
        "original_snapshot": Path("/root/original"), "contract_sha256": "c" * 64,
        "generated_manifest_sha256": "d" * 64}
    if fault == "no_contract":
        item = broker.ProjectInvocation.model_validate({k: v for k, v in item.model_dump().items()
            if k not in {"source_kind", "original_reference"}}, strict=True)  # type: ignore[assignment]
        options = {"generated_manifest_sha256": "d" * 64}
    elif fault == "uppercase":
        options["generated_manifest_sha256"] = "D" * 64
    elif fault == "short":
        options["generated_manifest_sha256"] = "d" * 63
    elif fault == "bool":
        options["generated_manifest_sha256"] = True
    with pytest.raises(ValueError):
        broker.build_project_systemd_command(item, BrokerPolicy(), Path("/root/snapshot"), **options)


def test_generated_manifest_is_not_a_new_model_selected_wire_field() -> None:
    payload = broker.ContractProjectBrokerRequest(invocation=invocation()).model_dump()
    payload["invocation"]["generated_manifest_sha256"] = "d" * 64
    with pytest.raises(ValueError):
        broker.ContractProjectBrokerRequest.model_validate(payload, strict=True)


def test_generated_test_uses_trusted_helper_under_same_contract_mounts_and_binds_raw_manifest(
    runner: Any, case: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = runtime(runner, monkeypatch)
    report = invoke(runner, case)
    assert report["status"] == "passed" and [c["status"] for c in report["checks"]] == ["passed"] * 3
    assert len(commands) == 5 and all("--contract-test-boundary" in cmd for cmd in commands)
    test = commands[-1][commands[-1].index("--") + 1:]
    assert Path(test[0]).name == "node" and test[1] == "--test"
    assert test[2].startswith("--test-reporter=data:text/javascript,")
    assert test[-2:] == ("--", "tests/acceptance.cjs")
    assert not any(cmd[-2:] == ("run", "test") for cmd in commands)
    proof = report["generated_test_execution"]
    assert proof == {"schema_version": 1, "manifest_sha256": case.manifest_sha, "count": 1,
        "names": ("total",), "test_evidence_sha256": hashlib.sha256(encoded({"schema_version": 1,
                                                                              "passed_tests": ("total",)})).hexdigest()}
    assert not case.work.exists()


def test_without_explicit_flag_old_npm_test_and_report_shape_are_unchanged(
    runner: Any, case: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = runtime(runner, monkeypatch)
    report = invoke(runner, case, generated=False)
    assert report["status"] == "passed" and "generated_test_execution" not in report
    assert commands[-1][-2:] == ("run", "test")


def test_explicit_generated_flag_without_protection_blocks_before_any_copy(runner: Any, case: Any) -> None:
    report = runner.run_project(case.input, case.work, 5, install_allowed=True,
                                generated_manifest_sha256=case.manifest_sha)
    assert report["status"] == "blocked" and not case.work.exists()
    assert "generated_test_execution" not in report


@pytest.mark.parametrize("change", ["raw_sha", "formatting", "python", "missing_test_protection", "missing_manifest_protection"])
def test_changed_manifest_or_unprotected_paths_block_before_project_phases(
    runner: Any, case: Any, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    if change in {"formatting", "python"}:
        content = json.loads(case.raw)
        if change == "python":
            content["stack"] = "python"
            content["test_files"] = ["tests/test_core.py"]
        raw = json.dumps(content, indent=2).encode()
        (case.input / "ACCEPTANCE.json").write_bytes(raw)
        if change == "python":
            case.manifest_sha = hashlib.sha256(raw).hexdigest()
    if change.startswith("missing_"):
        payload = case.contract.to_payload()
        del payload["protected_files"]["tests/acceptance.cjs" if change == "missing_test_protection" else "ACCEPTANCE.json"]
        # Exercise the runner's independent protection check, not contract decoder rejection.
        monkeypatch.setattr(runner, "_read_check_contract", lambda *a: payload)
        monkeypatch.setattr(runner, "_prepare_contract_work", lambda *a: None)
    commands = runtime(runner, monkeypatch)
    changes: dict[str, Any] = {"generated_manifest_sha256": "0" * 64} if change == "raw_sha" else {}
    report = invoke(runner, case, **changes)
    assert report["status"] == "blocked" and "generated_test_execution" not in report
    assert not commands and not case.work.exists()


@pytest.mark.parametrize("raw", ["echo fake-success", '{"status":"passed"}', "{}", helper_report() + helper_report(),
    helper_report(passed_tests=[]), helper_report(passed_tests=["other"]), helper_report(skipped=1)])
def test_invalid_test_evidence_turns_exit_zero_into_diagnostic_failed_check(
    runner: Any, case: Any, monkeypatch: pytest.MonkeyPatch, raw: str,
) -> None:
    runtime(runner, monkeypatch, raw=raw)
    report = invoke(runner, case)
    test = report["checks"][-1]
    assert report["status"] == "failed" and test["status"] == "failed" and test["exit_code"] != 0
    assert test["stderr"] == "generated_acceptance_test_evidence_invalid"
    assert "generated_test_execution" not in report and not case.work.exists()


@pytest.mark.parametrize("exit_code,timed_out,limited", [(4, False, False), (0, True, False), (0, False, True)])
def test_success_json_cannot_override_process_failure(
    runner: Any, case: Any, monkeypatch: pytest.MonkeyPatch,
    exit_code: int, timed_out: bool, limited: bool,
) -> None:
    runtime(runner, monkeypatch, exit_code=exit_code, timed_out=timed_out, limited=limited)
    report = invoke(runner, case)
    assert report["status"] == "failed" and "generated_test_execution" not in report


def test_generated_parser_loads_only_trusted_pure_sources_under_isolated_stdlib_python() -> None:
    script = """import json, runpy, sys
m = runpy.run_path(sys.argv[1])
support = m['_load_generated_acceptance']()
manifest = support['parse_acceptance_manifest'](sys.argv[2].encode())
assert manifest.stack == 'node' and manifest.test_ids == ('total',)
assert not any(name == 'agent_hub' or name.startswith('agent_hub.') for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(SOURCE), raw_manifest().decode()],
                            capture_output=True, timeout=10, check=False)
    assert result.returncode == 0 and result.stderr == b""


@pytest.mark.parametrize("generated", [False, True])
def test_cli_forwards_generated_flag_only_when_explicit(case: Any, generated: bool) -> None:
    script = """import json, runpy, sys
m = runpy.run_path(sys.argv[1])
def run(*args, **kwargs):
    expected = sys.argv[2]
    if expected:
        assert kwargs['generated_manifest_sha256'] == expected
    else:
        assert 'generated_manifest_sha256' not in kwargs
    return m['_empty_report']()
m['main'].__globals__['run_project'] = run
raise SystemExit(m['main'](sys.argv[3:]))
"""
    command = [sys.executable, "-I", "-B", "-c", script, str(SOURCE), case.manifest_sha if generated else "",
        "--input", str(case.input), "--work", str(case.work), "--timeout", "10",
        "--check-contract", str(case.control), "--original-input", str(case.original),
        "--expected-contract-sha256", case.contract.protected_sha256]
    if generated:
        command.extend(("--generated-manifest-sha256", case.manifest_sha))
    result = subprocess.run(command, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0 and result.stderr == b""
    assert "generated_test_execution" not in json.loads(result.stdout)


def test_cli_generated_flag_alone_is_a_finite_error_without_work(case: Any) -> None:
    result = subprocess.run([sys.executable, "-I", "-B", str(SOURCE), "--input", str(case.input),
        "--work", str(case.work), "--timeout", "10", "--generated-manifest-sha256", case.manifest_sha],
        capture_output=True, timeout=10, check=False)
    assert result.returncode == 1 and result.stdout == b""
    assert result.stderr.decode().strip() == "project_runner_infrastructure_error"
    assert not case.work.exists()


def test_actual_node_echo_only_cannot_forge_generated_success(
    runner: Any, case: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("local Node unavailable; no network/download fallback")
    forged = helper_report()
    (case.input / "tests/acceptance.cjs").write_text("console.log(" + json.dumps(forged) + ");", encoding="utf-8")
    # Align the authenticated fixture to this echo-only test; still no actual Node test is registered.
    case.files["tests/acceptance.cjs"] = hashlib.sha256(b"f\0" + (case.input / "tests/acceptance.cjs").read_bytes()).hexdigest()
    original_test = case.original / "tests/acceptance.cjs"
    original_test.chmod(0o600)
    original_test.write_bytes((case.input / "tests/acceptance.cjs").read_bytes())
    original_test.chmod(0o444)
    case.contract = build_project_check_contract(UUID(int=1), UUID(int=2), "greenfield", "one",
        hashlib.sha256(encoded(case.files)).hexdigest(), ("src",), case.files)
    case.control.chmod(0o600)
    case.control.write_bytes(encoded(case.contract.to_payload()))
    case.control.chmod(0o444)
    runtime(runner, monkeypatch)
    previous = runner._run_bounded
    def run(command: Any, **kwargs: Any) -> Any:
        if "--test" not in command:
            return previous(command, **kwargs)
        position = command.index("--test")
        inner = (node, *command[position:])
        done = subprocess.run(inner, cwd=kwargs["cwd"], capture_output=True, text=True,
                              encoding="utf-8", timeout=15, check=False)
        return runner.ProcessResult(done.returncode, False, False, done.stdout, done.stderr)
    monkeypatch.setattr(runner, "_run_bounded", run)
    report = invoke(runner, case)
    assert report["status"] == "failed" and "generated_test_execution" not in report


@pytest.mark.parametrize("source_kind", ["candidate", "original"])
async def test_broker_derives_generated_flag_only_from_root_staged_metadata(
    case: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str,
) -> None:
    from agent_hub.skills.sandbox import project_contract_broker
    stage = tmp_path / "broker"
    stage.mkdir(mode=0o700)
    policy = BrokerPolicy(runtime_root=stage)
    service = broker.ProjectExecutionBroker(policy)
    commands = []
    metadata = {"schema_version": 1, "purpose": "generated_acceptance", "capture_phase": "post_model",
                "user_id": str(UUID(int=3)), "request_sha256": "9" * 64,
                "manifest_sha256": case.manifest_sha}
    stored = SimpleNamespace(snapshot=case.original, contract=case.contract,
        reference=SimpleNamespace(input_source_sha256=case.contract.input_source_sha256), generated_metadata=metadata)
    monkeypatch.setattr(project_contract_broker, "stage_original_contract", lambda **kw: stored)
    monkeypatch.setattr(broker, "_systemd_supported", lambda: True)
    monkeypatch.setattr(broker, "_private_project_runtime", lambda *a: None)
    async def snapshot(*args: Any, **kwargs: Any) -> str:
        kwargs["_manifest"].update(case.files)
        return cast(str, case.contract.input_source_sha256)
    async def stop(*args: Any) -> None:
        pass
    async def launch(command: Any, limits: Any, **kwargs: Any) -> SkillResult:
        commands.append(command)
        return SkillResult(exit_code=0, timed_out=False, stderr="", stdout=json.dumps({
            "schema_version": 1, "status": "blocked", "source_sha256": case.contract.input_source_sha256,
            "checks": [], "runtime": {}, "original_check_contract": {"schema_version": 1,
                "contract_sha256": case.contract.protected_sha256, "protected_sha256": case.contract.protected_sha256}}))
    monkeypatch.setattr(broker, "snapshot_project_source", snapshot)
    monkeypatch.setattr(service, "_kill_and_confirm", stop)
    monkeypatch.setattr(broker, "run_subprocess_with_limits", launch)
    result = await service.handle(broker.ContractProjectBrokerRequest(invocation=invocation(source_kind)),
                                  peer_uid=policy.allowed_uid)
    if source_kind == "original":
        assert not result.ok and not commands
    else:
        assert result.ok and len(commands) == 1
        command = commands[0]
        assert command[command.index("--generated-manifest-sha256") + 1] == case.manifest_sha
