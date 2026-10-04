"""Real Node fixture regressions for the Python-owned large-module contract."""

from __future__ import annotations

import importlib
import json
import os
import runpy
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "src/agent_hub/harness/project_validation_modules.py"
_FIXTURE = _ROOT / "tests/fixtures/project_business/large_modules.cjs"
_COUNTS = {
    "inventory_stores": 2, "inventory_instances": 3, "inventory_calls": 14,
    "inventory_commits": 14, "inventory_successes": 10, "inventory_conflicts": 4,
    "concurrency_pairs": 1, "reporting_instances": 2, "reporting_snapshots": 6,
    "reporting_reads": 6, "composition_http_requests": 9, "composition_markers": 5,
    "starts": 1, "stops": 1,
}


def _require_fixture_runtime() -> None:
    if shutil.which("node") is None:
        pytest.skip("real module fixture requires Node")
    if sys.platform != "win32" and not (
        sys.platform == "linux"
        and os.environ.get("AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX") in {"bwrap", "systemd"}
        and Path("/usr/bin/bwrap").is_file()
        and os.access("/usr/bin/bwrap", os.X_OK)
    ):
        pytest.skip("real Linux module fixtures require authorized public bwrap sandbox")


def _api() -> Callable[[Path, float], dict[str, Any]]:
    assert _MODULE.is_file(), "large-module evaluator is not implemented"
    _require_fixture_runtime()
    if sys.platform != "win32":
        wrapper = importlib.import_module("agent_hub.harness.project_requirements")
        return cast(Callable[[Path, float], dict[str, Any]], wrapper.validate_large_order_modules)
    return cast(Callable[[Path, float], dict[str, Any]],
                runpy.run_path(str(_MODULE))["validate_large_modules"])


def _project(tmp_path: Path, *, source: str | None = None, esm: bool = False) -> Path:
    _require_fixture_runtime()
    root = tmp_path / "module project with spaces"
    root.mkdir()
    assert _FIXTURE.is_file(), "side-effect-free module fixture is not implemented"
    (root / "acceptance.cjs").write_text(
        _FIXTURE.read_text(encoding="utf-8") if source is None else source, encoding="utf-8",
    )
    entry = "./acceptance.cjs"
    if esm:
        (root / "acceptance.mjs").write_text(
            "import api from './acceptance.cjs';\n"
            "export const {createInventory, createReporting, createApp} = api;\n",
            encoding="utf-8",
        )
        entry = "./acceptance.mjs"
    (root / "package.json").write_text(json.dumps({
        "private": True, "agent_hub_acceptance": {"version": 1, "entry": entry},
    }), encoding="utf-8")
    return root


def _assert_cleanup(root: Path) -> None:
    for line in (root / "app-observations.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if "dataDir" in record:
            assert not Path(record["dataDir"]).exists(), "private app data directory leaked"
        if "port" in record:
            with socket.socket() as sock:
                sock.settimeout(0.3)
                assert sock.connect_ex(("127.0.0.1", record["port"])) != 0


@pytest.mark.parametrize("esm", [False, True], ids=["cjs", "esm"])
def test_real_modules_observe_all_counts_without_windows_isolation_credit(
    tmp_path: Path, esm: bool,
) -> None:
    validate = _api()
    root = _project(tmp_path, esm=esm)
    result = validate(root, 25)
    assert result["status"] == ("unknown" if sys.platform == "win32" else "passed"), result
    assert result["npm_start_module_binding"] == "unknown"
    assert result["cleanup_ok"] is True
    assert result["isolation_verified"] is (sys.platform != "win32")
    assert {key: result["measurements"][key] for key in _COUNTS} == _COUNTS
    if sys.platform == "win32":
        assert "isolation" in " ".join(result["reasons"]).lower()
    if sys.platform != "win32":
        return  # Linux exercises the public sandbox; its project mount is read-only.
    requests = [json.loads(line) for line in
                (root / "http-observations.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(requests) == 9
    assert [item["status"] for item in requests] == [201, 201, 201, 201, 201, 409, 200, 200, 200]
    reports = [item["body"] for item in requests[-3:]]
    assert reports[0] == {"orders": {"total": 0, "authorized_count": 0},
                          "inventory": {"reserved_units": 1},
                          "fulfillment": {"total": 0, "cancelled_count": 0}}
    assert reports[1] != reports[2]
    _assert_cleanup(root)


def test_async_factories_can_return_frozen_instances(tmp_path: Path) -> None:
    validate = _api()
    source = _FIXTURE.read_text(encoding="utf-8").replace(
        "module.exports = {createInventory, createReporting, createApp};",
        "module.exports = {createApp, "
        "createInventory: async options => Object.freeze(createInventory(options)), "
        "createReporting: async options => Object.freeze(createReporting(options))};",
    )
    root = _project(tmp_path, source=source)
    result = validate(root, 25)
    assert result["status"] == ("unknown" if sys.platform == "win32" else "passed"), result
    assert result["measurements"]["composition_markers"] == 5, result
    assert result["cleanup_ok"] is True


@pytest.mark.parametrize("kinds", [
    ("sync", "sync", "sync"), ("promise", "promise", "promise"),
    ("sync", "promise", "sync"), ("promise", "sync", "promise"),
    ("sync", "sync", "promise"),
], ids=["sync", "promise", "mixed-reserve", "mixed-stock-report", "report-only"])
@pytest.mark.parametrize("per_request", [False, True], ids=["shared", "per-request"])
def test_http_injection_preserves_method_return_kinds(
    tmp_path: Path, kinds: tuple[str, str, str], per_request: bool,
) -> None:
    validate = _api()
    source = _FIXTURE.read_text(encoding="utf-8")
    # The app's store is genuinely synchronous; the controlled inventory store
    # used by the evaluator retains its normal asynchronous transaction protocol.
    source = source.replace("const pending = queue.then(() => {", "const pending = (() => {")
    source = source.replace(
        "      });\n      queue = pending.catch(() => {});\n      return pending;",
        "      })();\n      return pending;",
    )
    source = source.replace("async snapshot() {", "snapshot() {")
    source = source.replace("const rows = await read();", "const rows = read();")
    for method, signature, kind in zip(
        ("addStock", "reserve", "snapshot"),
        ("addStock({sku, quantity})", "reserve({sku, quantity, reason})", "snapshot()"),
        kinds, strict=True,
    ):
        if kind == "promise":
            source = source.replace(f"    {signature} {{", f"    async {signature} {{")
        call = f"reporting.{method}()" if method == "snapshot" else f"inventory.{method}(body)"
        send = "send(200, result)" if method == "snapshot" else "send(result.status, result.body)"
        original = f"const result = await {call};\n        return {send};"
        failure = "send(500, {error: {code: 'RETURN_KIND', message: '" + method + "'}})"
        if kind == "promise":
            replacement = (
                f"const pending = {call};\n"
                f"        if (!(pending instanceof Promise)) return {failure};\n"
                f"        return pending.then(result => {send}).catch(() => {failure});"
            )
        else:
            replacement = (
                f"const result = {call};\n"
                f"        if (result && typeof result.then === 'function') return {failure};\n"
                f"        return {send};"
            )
        assert original in source
        source = source.replace(original, replacement)
    if per_request:
        factories = (
            "  const inventory = await inventoryFactory({store});\n"
            "  const reporting = await reportingFactory({read: () => rows});\n"
        )
        assert factories in source
        source = source.replace(factories, "")
        source = source.replace("      const body = raw ? JSON.parse(raw) : {};",
                                "      const body = raw ? JSON.parse(raw) : {};\n" + factories)
    root = _project(tmp_path, source=source)
    result = validate(root, 25)
    assert result["status"] == ("unknown" if sys.platform == "win32" else "passed"), result
    assert {key: result["measurements"][key] for key in _COUNTS} == _COUNTS
    assert result["cleanup_ok"] is True
    if sys.platform == "win32":
        _assert_cleanup(root)


def test_successful_reservation_can_remove_exhausted_stock_key(tmp_path: Path) -> None:
    validate = _api()
    source = _FIXTURE.read_text(encoding="utf-8").replace(
        "draft.stock[sku] -= quantity;",
        "draft.stock[sku] -= quantity; if (draft.stock[sku] === 0) delete draft.stock[sku];",
    )
    root = _project(tmp_path, source=source)
    result = validate(root, 25)
    assert result["status"] == ("unknown" if sys.platform == "win32" else "passed"), result
    assert {key: result["measurements"][key] for key in _COUNTS} == _COUNTS
    assert result["cleanup_ok"] is True
    if sys.platform == "win32":
        _assert_cleanup(root)


@pytest.mark.parametrize("sparse", [False, True], ids=["delete-zero", "reinsert-zero"])
def test_conflict_must_preserve_accepted_zero_key_representation(
    tmp_path: Path, sparse: bool,
) -> None:
    validate = _api()
    source = _FIXTURE.read_text(encoding="utf-8")
    if sparse:
        source = source.replace(
            "draft.stock[sku] -= quantity;",
            "draft.stock[sku] -= quantity; if (draft.stock[sku] === 0) delete draft.stock[sku];",
        )
        mutation = (
            "if (!(sku in draft.stock) && draft.reservations.some(row => row.sku === sku)) "
            "draft.stock[sku] = 0;"
        )
    else:
        mutation = "if (draft.stock[sku] === 0) delete draft.stock[sku];"
    source = source.replace("return conflict();", mutation + " return conflict();")
    root = _project(tmp_path, source=source)
    result = validate(root, 12)
    assert result["status"] == "failed", result
    assert "entire transaction state mismatch" in " ".join(result["reasons"]), result
    # Six earlier operations and the depletion commit succeed; only the following
    # conflict's representation change is rejected, even though stock stays zero.
    assert result["measurements"]["inventory_calls"] == 7, result
    assert result["measurements"]["inventory_commits"] == 7, result
    assert result["cleanup_ok"] is True


_MUTATIONS = [
    ("overwrite_stock", "(draft.stock[sku] || 0) + quantity", "quantity", "inventory"),
    ("no_deduction", "draft.stock[sku] -= quantity;", "", "inventory"),
    ("delete_nonzero_stock", "draft.stock[sku] -= quantity;",
     "draft.stock[sku] -= quantity; delete draft.stock[sku];", "inventory"),
    ("bool_zero_stock", "draft.stock[sku] -= quantity;",
     "draft.stock[sku] -= quantity; if (draft.stock[sku] === 0) draft.stock[sku] = false;",
     "inventory"),
    ("failed_conflict_writes", "return conflict();", "draft.stock[sku] = -1; return conflict();",
     "inventory"),
    ("reservation_corruption", "draft.reservations.push(row);",
     "draft.reservations.push({...row, quantity: quantity + 1});", "inventory"),
    ("bool_id", "id: randomUUID(), sku", "id: true, sku", "inventory"),
    ("global_store", "function createInventory({store}) {",
     "let shared; function createInventory({store}) { store = shared || (shared = store);",
     "inventory"),
    ("bypass_transactions", "return store.transact(draft => {",
     "return ((update) => update({stock: {}, reservations: []}))(draft => {", "inventory"),
    ("split_transactions", "addStock({sku, quantity}) {",
     "async addStock({sku, quantity}) { await store.transact(() => null);", "inventory"),
    ("async_updater", "store.transact(draft => {", "store.transact(async draft => {", "inventory"),
    ("throwing_updater", "draft.stock[sku] -= quantity;",
     "draft.stock[sku] -= quantity; throw new Error('updater threw');", "inventory"),
    ("static_report", "const rows = await read();",
     "const rows = {orders: [], reservations: [], fulfillment: []};", "reporting"),
    ("cached_report", "function createReporting({read}) {",
     ("function createReporting({read}) { const original = read; let cached; "
      "read = () => cached || (cached = original());"), "reporting"),
    ("count_rows", "rows.reservations.reduce((sum, row) => sum + row.quantity, 0)",
     "rows.reservations.length", "reporting"),
    ("mutate_reader", "const rows = await read();",
     "const rows = await read(); rows.orders.push({payment_state: 'authorized'});", "reporting"),
    ("discard_inventory_injection", "await inventoryFactory({store})",
     "await createInventory({store})", "composition"),
    ("discard_report_injection", "await reportingFactory({read: () => rows})",
     "await createReporting({read: () => rows})", "composition"),
    ("cache_report_injection", "const result = await reporting.snapshot();",
     "const result = reportCache || (reportCache = await reporting.snapshot());", "composition"),
    ("discard_success_body", "return send(result.status, result.body);",
     "return send(result.status, {...result.body, id: randomUUID()});", "composition"),
    ("close_failure", "async close() {", "async close() { throw new Error('close failed');",
     "close"),
]


@pytest.mark.parametrize("name,old,new,reason", _MUTATIONS, ids=[m[0] for m in _MUTATIONS])
def test_real_node_mutants_fail_closed(
    tmp_path: Path, name: str, old: str, new: str, reason: str,
) -> None:
    validate = _api()
    source = _FIXTURE.read_text(encoding="utf-8")
    assert old in source, name
    root = _project(tmp_path, source=source.replace(old, new))
    result = validate(root, 12)
    assert result["status"] == "failed", (name, result)
    assert reason in " ".join(result["reasons"]).lower(), result
    assert all(0 <= result["measurements"][key] <= value for key, value in _COUNTS.items())
    if (root / "app-observations.jsonl").exists():
        _assert_cleanup(root)


@pytest.mark.parametrize("prefix", [
    "require('node:http').createServer(() => {}).listen(0, '127.0.0.1');",
    "require('node:child_process').spawn(process.execPath, ['-e', 'setInterval(()=>{},1000)']);",
    "console.log(JSON.stringify({status:'passed',cleanup_ok:true}));",
    "process.exit(0);",
    "process.stdout.write('x'.repeat(3 * 1024 * 1024));",
], ids=["import_listener", "import_child", "forged_verdict", "premature_exit", "flood"])
def test_import_side_effects_and_forged_stdout_never_pass(tmp_path: Path, prefix: str) -> None:
    validate = _api()
    root = _project(tmp_path, source=prefix + "\n" + _FIXTURE.read_text(encoding="utf-8"))
    result = validate(root, 8)
    assert result["status"] == "failed", result
    assert result["measurements"]["composition_http_requests"] == 0


@pytest.mark.parametrize("locator", [None, {"version": True, "entry": "./acceptance.cjs"},
                                     {"version": 1, "entry": "../outside.cjs"},
                                     {"version": 1, "entry": "C:\\outside.cjs"}])
def test_invalid_locator_fails_before_node_calls(tmp_path: Path, locator: object) -> None:
    validate = _api()
    root = _project(tmp_path)
    (root / "package.json").write_text(json.dumps({"agent_hub_acceptance": locator}),
                                       encoding="utf-8")
    result = validate(root, 8)
    assert result["status"] == "failed", result
    assert result["measurements"]["inventory_calls"] == 0
    assert not (root / "app-observations.jsonl").exists()


@pytest.mark.parametrize("number", ["1e999", "-1e999"])
@pytest.mark.parametrize("channel", ["locator", "http"])
def test_exponent_overflow_is_rejected_in_all_json_inputs(
    tmp_path: Path, number: str, channel: str,
) -> None:
    validate = _api()
    source = _FIXTURE.read_text(encoding="utf-8")
    if channel == "http":
        source = source.replace(
            "res.end(JSON.stringify(payload));",
            "res.end(JSON.stringify(payload).slice(0, -1) + ',\"overflow\":" + number + "}');",
        )
    root = _project(tmp_path, source=source)
    if channel == "locator":
        package = root / "package.json"
        raw = package.read_text(encoding="utf-8")
        package.write_text(raw[:-1] + ', "overflow": ' + number + "}", encoding="utf-8")
    result = validate(root, 12)
    assert result["status"] == "failed", result
    assert "nonfinite" in " ".join(result["reasons"]).lower(), result
    assert result["cleanup_ok"] is True


def test_fixture_import_does_not_start_servers_children_or_create_files(tmp_path: Path) -> None:
    root = _project(tmp_path)
    before = set(root.iterdir())
    result = subprocess.run(["node", "-e", "require('./acceptance.cjs')"], cwd=root,
                            capture_output=True, timeout=3, check=False)
    assert result.returncode == 0, result.stderr
    assert not result.stdout
    assert set(root.iterdir()) == before


def test_evaluator_import_has_no_process_or_filesystem_side_effects(tmp_path: Path) -> None:
    assert _MODULE.is_file(), "large-module evaluator is not implemented"
    code = ("import runpy, subprocess, sys; "
            "subprocess.Popen=lambda *a,**k: (_ for _ in ()).throw(AssertionError('spawn')); "
            "runpy.run_path(sys.argv[1])")
    before = set(tmp_path.iterdir())
    result = subprocess.run([sys.executable, "-c", code, str(_MODULE)], cwd=tmp_path,
                            capture_output=True, timeout=3, check=False)
    assert result.returncode == 0, result.stderr
    assert not result.stdout
    assert set(tmp_path.iterdir()) == before


def test_single_deadline_bounds_a_hung_factory(tmp_path: Path) -> None:
    validate = _api()
    source = _FIXTURE.read_text(encoding="utf-8").replace(
        "function createInventory({store}) {",
        "async function createInventory({store}) { await new Promise(() => {});",
    )
    root = _project(tmp_path, source=source)
    started = time.monotonic()
    result = validate(root, 1.5)
    assert time.monotonic() - started < 8
    assert result["status"] == "failed", result
    assert result["cleanup_ok"] is True
