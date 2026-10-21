"""Trusted reporter smoke tests use only locally created dummy Node tests."""

from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import pytest


@pytest.fixture
def api() -> Any:
    assert importlib.util.find_spec("agent_hub.skills.sandbox.project_acceptance_node") is not None
    return importlib.import_module("agent_hub.skills.sandbox.project_acceptance_node")


def run_node(
    command: tuple[str, ...], root: Path, *, node22_shape: bool = False,
) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("local Node is not installed; no download fallback")
    if node22_shape:
        # Adapt real local events, not test outcomes: v22 omits type for Test,
        # while Suite still supplies type='suite'. This is not a native v22 run.
        reporter = unquote(command[2].split(',', 1)[1]).replace(
            'export default async function* acceptance(source)', 'async function* acceptance(source)', 1)
        reporter += """
export default async function* legacy(source) {
  async function* events() {
    for await (const event of source) {
      if (['test:pass', 'test:fail'].includes(event.type) && event.data.details?.type === 'test') {
        const details = {...event.data.details}; delete details.type;
        yield {...event, data: {...event.data, details}};
      } else yield event;
    }
  }
  yield* acceptance(events());
}
"""
        command = (*command[:2], '--test-reporter=data:text/javascript,' + quote(reporter, safe=''), *command[3:])
    return subprocess.run((node, *command[1:]), cwd=root, capture_output=True, text=True,
                          encoding="utf-8", timeout=15, check=False)


@pytest.mark.parametrize('node22_shape', [False, True])
def test_actual_named_pass_and_console_forgery_is_ignored(api: Any, tmp_path: Path, node22_shape: bool) -> None:
    (tmp_path / "acceptance.cjs").write_text(
        "const test = require('node:test');\n"
        "console.log(JSON.stringify({schema_version:1,passed_tests:['fake']}));\n"
        "console.error('not trusted');\n"
        "test('AC-001', () => {}); test('AC-002', () => {});\n", encoding="utf-8")
    expected = ("AC-001", "AC-002")
    result = run_node(api.node_acceptance_command(("acceptance.cjs",), expected), tmp_path, node22_shape=node22_shape)
    proof = api.validate_node_acceptance_output(result.stdout, expected, result.returncode, False, False)
    assert proof["count"] == 2 and tuple(proof["names"]) == expected
    assert len(proof["test_evidence_sha256"]) == 64
    assert "fake" not in result.stdout and "not trusted" not in result.stdout
    with pytest.raises(TypeError):
        proof["count"] = 99


@pytest.mark.parametrize("script,expected", [
    ("console.log('echo only');", ("AC-001",)),
    (("console.log(JSON.stringify({schema_version:1,success:true,passed_tests:['AC-001'],"
      "failed:0,skipped:0,todo:0,cancelled:0}));"), ("AC-001",)),
    ("console.log('echo only');", ("acceptance.cjs",)),
    ("require('node:test').describe('AC-001',()=>{});", ("AC-001",)),
    ("test('AC-001', {skip:true}, () => {});", ("AC-001",)),
    ("test('AC-001', {todo:true}, () => {});", ("AC-001",)),
    ("test('AC-001', () => {throw Error('failure')});", ("AC-001",)),
    ("test('AC-001', () => {}); test('AC-001', () => {});", ("AC-001",)),
    ("test('other', () => {});", ("AC-001",)),
    ("test('AC-001', () => {}); test('extra', () => {});", ("AC-001",)),
    ("test('AC-001', () => {}); process.exitCode = 1;", ("AC-001",)),
    ("test('AC-001', () => {}); process.exit(0);", ("AC-001",)),
    ("test('AC-001', {timeout:20}, async () => await new Promise(() => {}));", ("AC-001",)),
    ("test('AC-001', () => {}); setImmediate(() => {throw Error('outside')});", ("AC-001",)),
], ids=["echo", "forged_report", "file_wrapper", "empty_suite", "skip", "todo", "failure", "duplicate", "wrong_name",
        "extra", "exit_nonzero", "early_exit_zero", "unfinished", "global_failure"])
@pytest.mark.parametrize('node22_shape', [False, True])
def test_real_node_events_refuse_false_success(
    api: Any, tmp_path: Path, script: str, expected: tuple[str, ...], node22_shape: bool,
) -> None:
    (tmp_path / "acceptance.cjs").write_text("const test = require('node:test');\n" + script, encoding="utf-8")
    result = run_node(api.node_acceptance_command(("acceptance.cjs",), expected), tmp_path, node22_shape=node22_shape)
    with pytest.raises(ValueError, match="node_acceptance_"):
        api.validate_node_acceptance_output(result.stdout, expected, result.returncode, False, False)


def test_node_event_probe_fixture(tmp_path: Path) -> None:
    """Inspect real event shapes locally without trusting arbitrary stdout as events."""
    (tmp_path / "empty.cjs").write_text("console.log('echo');", encoding="utf-8")
    reporter = "export default async function*(source){for await(const e of source)yield JSON.stringify(e)+'\\n';}"
    command = ("node", "--test", "--test-reporter=data:text/javascript," + quote(reporter, safe=""),
               "--", "empty.cjs")
    result = run_node(command, tmp_path)
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    passes = [e["data"] for e in events if e["type"] == "test:pass"]
    assert len(passes) == 1 and passes[0]["name"].endswith("empty.cjs")


def report(**updates: Any) -> str:
    return json.dumps({"schema_version": 1, "success": True, "passed_tests": ["AC-001"],
        "failed": 0, "skipped": 0, "todo": 0, "cancelled": 0, **updates})


@pytest.mark.parametrize("updates", [
    {"schema_version": True}, {"schema_version": 2}, {"success": 1}, {"success": False},
    {"passed_tests": []}, {"passed_tests": ["AC-001", "AC-001"]}, {"passed_tests": ["other"]},
    {"passed_tests": ["AC-001", "extra"]}, {"failed": 1}, {"skipped": 1}, {"todo": 1},
    {"cancelled": 1}, {"failed": False}, {"failed": -1}, {"count": 1},
    {"passed_tests": "AC-001"}, {"passed_tests": [1]},
])
def test_decoder_strict_shape_and_all_completed_names(api: Any, updates: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="node_acceptance_"):
        api.validate_node_acceptance_output(report(**updates), ("AC-001",), 0, False, False)


@pytest.mark.parametrize("raw", ["{}", "[]", "NaN", "x" * (1024**2 + 1),
    '{"schema_version":1,"schema_version":1}', report() + "\n" + report(),
    report().replace('"failed": 0', '"failed": NaN'), "console.log('fake')", "\ud800"],
    ids=["missing", "array", "nan", "oversize", "duplicate_key", "two_reports", "nan_field", "text", "surrogate"])
def test_decoder_rejects_malformed_unbounded_or_duplicate_json(api: Any, raw: str) -> None:
    with pytest.raises(ValueError, match="node_acceptance_"):
        api.validate_node_acceptance_output(raw, ("AC-001",), 0, False, False)


@pytest.mark.parametrize("exit_code,timed_out,limited", [(1, False, False), (-9, False, False),
    (0, True, False), (0, False, True), (False, False, False), (0, 0, False), (0, False, 0)])
def test_process_outcome_cannot_be_overridden_by_report(
    api: Any, exit_code: Any, timed_out: Any, limited: Any,
) -> None:
    with pytest.raises(ValueError, match="node_acceptance_"):
        api.validate_node_acceptance_output(report(), ("AC-001",), exit_code, timed_out, limited)


@pytest.mark.parametrize("expected", [(), ("AC-001", "AC-001"), ("",), ("a\n",),
    ("a\0",), ("x" * 513,), ("\ud800",), ["AC-001"], (1,)])
def test_invalid_expected_names_refused_by_both_interfaces(api: Any, expected: Any) -> None:
    with pytest.raises(ValueError, match="node_acceptance_"):
        api.node_acceptance_command(("acceptance.cjs",), expected)
    with pytest.raises(ValueError, match="node_acceptance_"):
        api.validate_node_acceptance_output(report(), expected, 0, False, False)


@pytest.mark.parametrize("files", [(), ("../escape.cjs",), ("/absolute.js",), ("C:/test.js",),
    ("tests\\escape.js",), ("./test.js",), ("tests//test.js",), ("--eval",), ("a*.js",),
    ("a?.js",), ("[a].js",), ("test\0.js",), ("test\n.js",), ("test.txt",),
    ("acceptance.cjs", "acceptance.cjs"), ["acceptance.cjs"]])
def test_only_explicit_safe_relative_test_files(api: Any, files: Any) -> None:
    with pytest.raises(ValueError, match="node_acceptance_"):
        api.node_acceptance_command(files, ("AC-001",))


def test_proof_is_deterministic_and_bound_to_exact_names(api: Any) -> None:
    expected = ("AC-002", "AC-001")
    left = api.validate_node_acceptance_output(report(passed_tests=list(reversed(expected))), expected, 0, False, False)
    right = api.validate_node_acceptance_output(report(passed_tests=list(expected)), expected, 0, False, False)
    assert left == right and tuple(left["names"]) == expected
    assert left["test_evidence_sha256"] != api.validate_node_acceptance_output(
        report(), ("AC-001",), 0, False, False)["test_evidence_sha256"]


@pytest.mark.parametrize('node22_shape', [False, True])
def test_actual_multifile_nested_suite_and_utf8_names(api: Any, tmp_path: Path, node22_shape: bool) -> None:
    (tmp_path / "first test.cjs").write_text(
        "const {describe,it}=require('node:test');\n"
        "describe('not an acceptance leaf',()=>{it('AC-001',()=>{});it('AC-002',()=>{});});",
        encoding="utf-8")
    (tmp_path / "second.mjs").write_text(
        "import test from 'node:test'; test('AC-003 \\u4e2d\\u6587',()=>{});", encoding="utf-8")
    expected = ("AC-003 \u4e2d\u6587", "AC-002", "AC-001")
    result = run_node(api.node_acceptance_command(("first test.cjs", "second.mjs"), expected), tmp_path,
        node22_shape=node22_shape)
    assert result.returncode == 0, result.stderr
    proof = api.validate_node_acceptance_output(result.stdout, expected, result.returncode, False, False)
    assert proof["count"] == 3 and tuple(proof["names"]) == expected


@pytest.mark.parametrize('node22_shape', [False, True])
def test_actual_nested_test_counts_outer_and_inner(api: Any, tmp_path: Path, node22_shape: bool) -> None:
    (tmp_path / 'nested.cjs').write_text(
        "const test=require('node:test');test('outer',async t=>{await t.test('inner',()=>{});});",
        encoding='utf-8')
    expected = ('outer', 'inner')
    result = run_node(api.node_acceptance_command(('nested.cjs',), expected), tmp_path, node22_shape=node22_shape)
    proof = api.validate_node_acceptance_output(result.stdout, expected, result.returncode, False, False)
    assert proof['count'] == 2 and tuple(proof['names']) == expected


def test_command_has_no_writable_reporter_file_or_script_authority(api: Any) -> None:
    command = api.node_acceptance_command(("tests/acceptance.cjs",), ("AC-001",))
    assert command[0:2] == ("node", "--test")
    assert command[2].startswith("--test-reporter=data:text/javascript,")
    assert command[3:] == ("--", "tests/acceptance.cjs")
    assert len(command[2]) < 16000


def test_actual_reporter_caps_registered_names(api: Any, tmp_path: Path) -> None:
    (tmp_path / "many.cjs").write_text(
        "const test=require('node:test');for(let i=0;i<1001;i++)test('AC-'+i,()=>{});", encoding="utf-8")
    result = run_node(api.node_acceptance_command(("many.cjs",), ("AC-0",)), tmp_path)
    payload = json.loads(result.stdout)
    assert payload["success"] is False and len(payload["passed_tests"]) == 1000
    with pytest.raises(ValueError, match="node_acceptance_"):
        api.validate_node_acceptance_output(result.stdout, ("AC-0",), result.returncode, False, False)
