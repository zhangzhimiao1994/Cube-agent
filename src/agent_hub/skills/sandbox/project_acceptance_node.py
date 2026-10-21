"""Pure command/projection for trusted, process-isolated node:test reporting.

The caller supplies the trusted Node binary, clears NODE_OPTIONS/NODE_PATH and
owns sandboxing, source protection, limits and process drain. This module never
spawns code or grants authority. The decoder must receive that command's stdout,
not a model assertion, npm output or an arbitrary persisted JSON report.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import cast
from urllib.parse import quote

_MAX_TESTS = 1000
_MAX_NAME_BYTES = 512
_MAX_OUTPUT_BYTES = 1024**2
_KEYS = frozenset({"schema_version", "success", "passed_tests", "failed", "skipped", "todo", "cancelled"})

# This runs in the test runner's parent process. Child stdout/stderr are events,
# never reporter instructions. No third-party reporter or writable helper file.
_REPORTER = r"""
import { resolve } from 'node:path';
export default async function* acceptance(source) {
  const names = [], seen = new Set();
  let invalid = false, summary = null, summaries = 0;
  let failed = 0, skipped = 0, todo = 0, cancelled = 0;
  try {
    for await (const event of source) {
      const d = event.data;
      if (event.type === 'test:summary' && d.file === undefined) {
        summaries++;
        summary = d;
      }
      if (event.type !== 'test:pass' && event.type !== 'test:fail') continue;
      if (event.type === 'test:fail') failed = Math.min(failed + 1, 1000000);
      const skip = d.skip !== undefined && d.skip !== false;
      const pending = d.todo !== undefined && d.todo !== false;
      if (skip) skipped = Math.min(skipped + 1, 1000000);
      if (pending) todo = Math.min(todo + 1, 1000000);
      if (d.details?.type === 'suite') continue;
      if (d.details?.type !== 'test' || typeof d.name !== 'string' || !d.name.trim()
          || Buffer.byteLength(d.name, 'utf8') > 512 || /[\x00-\x1f\x7f]/u.test(d.name)
          || typeof d.file !== 'string' || !Number.isInteger(d.line) || d.line < 1
          || !Number.isInteger(d.column) || d.column < 1 || seen.size >= 1000
          || seen.has(d.name)) { invalid = true; continue; }
      // Node reports an empty/echo-only file as a successful file subtest.
      // It is not evidence that any named acceptance test was registered.
      if (resolve(d.name) === resolve(d.file)) { invalid = true; continue; }
      seen.add(d.name);
      if (event.type === 'test:pass' && !skip && !pending) names.push(d.name);
    }
  } catch { invalid = true; }
  const c = summary?.counts;
  if (c) {
    for (const key of ['tests', 'passed', 'failed', 'skipped', 'todo', 'cancelled', 'suites']) {
      if (!Number.isSafeInteger(c[key]) || c[key] < 0 || c[key] > 1000000) invalid = true;
    }
    failed = Math.max(failed, c.failed || 0);
    skipped = Math.max(skipped, c.skipped || 0);
    todo = Math.max(todo, c.todo || 0);
    cancelled = c.cancelled || 0;
  }
  const success = !invalid && summaries === 1 && summary?.success === true
    && names.length > 0 && c?.tests === names.length && c?.passed === names.length
    && failed === 0 && skipped === 0 && todo === 0 && cancelled === 0;
  yield JSON.stringify({schema_version: 1, success, passed_tests: names,
                        failed, skipped, todo, cancelled}) + '\n';
}
""".strip()


def _need(value: object, code: str) -> None:
    if not value:
        raise ValueError(code)


def _names(expected: tuple[str, ...]) -> tuple[str, ...]:
    try:
        _need(type(expected) is tuple and 0 < len(expected) <= _MAX_TESTS, "node_acceptance_input_invalid")
        for name in expected:
            _need(type(name) is str and name.strip() and len(name.encode("utf-8")) <= _MAX_NAME_BYTES
                  and not any(ord(c) < 32 or ord(c) == 127 for c in name), "node_acceptance_input_invalid")
        _need(len(set(expected)) == len(expected), "node_acceptance_input_invalid")
        return expected
    except (TypeError, UnicodeError):
        raise ValueError("node_acceptance_input_invalid") from None


def node_acceptance_command(test_files: tuple[str, ...], expected_tests: tuple[str, ...]) -> tuple[str, ...]:
    """Return an explicit node:test command; it never runs npm or discovers tests.

    First recipe: <=256 unique JS/CJS/MJS files and <=1000 unique exact names.
    Names resolving to the test file itself are reserved (Node's empty-file pass).
    File existence, links and immutable hashes are the caller's responsibility.
    """
    _names(expected_tests)
    _need(type(test_files) is tuple and 0 < len(test_files) <= 256, "node_acceptance_input_invalid")
    for path in test_files:
        _need(type(path) is str and 0 < len(path) <= 512, "node_acceptance_input_invalid")
        parts = path.split("/")
        _need(len(parts) <= 32 and all(
            re.fullmatch(r"[A-Za-z0-9_.+ -]+", part) and part not in {".", ".."}
            and not part.startswith("-") and not part.endswith((" ", ".")) for part in parts)
            and path.endswith((".js", ".cjs", ".mjs")), "node_acceptance_input_invalid")
    _need(len(set(test_files)) == len(test_files), "node_acceptance_input_invalid")
    return ("node", "--test", "--test-reporter=data:text/javascript," + quote(_REPORTER, safe=""),
            "--", *test_files)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _need(key not in result, "node_acceptance_output_invalid")
        result[key] = value
    return result


def _constant(_: str) -> object:
    raise ValueError("node_acceptance_output_invalid")


def validate_node_acceptance_output(
    raw: str, expected_tests: tuple[str, ...], exit_code: int, timed_out: bool, output_limited: bool,
) -> Mapping[str, object]:
    """Validate actual isolated command output; a JSON body alone is not evidence.

    Return deeply immutable schema_version/count/names/test_evidence_sha256.
    The SHA covers canonical named successful evidence, not logs, paths or time.
    """
    expected = _names(expected_tests)
    _need(type(exit_code) is int and exit_code == 0 and type(timed_out) is bool and not timed_out
          and type(output_limited) is bool and not output_limited, "node_acceptance_process_failed")
    try:
        _need(type(raw) is str and 0 < len(raw) <= _MAX_OUTPUT_BYTES
              and len(raw.encode("utf-8")) <= _MAX_OUTPUT_BYTES, "node_acceptance_output_invalid")
        payload = cast(object, json.loads(raw, object_pairs_hook=_object, parse_constant=_constant))
        _need(type(payload) is dict, "node_acceptance_output_invalid")
        assert isinstance(payload, dict)
        _need(set(payload) == _KEYS and type(payload["schema_version"]) is int
              and payload["schema_version"] == 1 and type(payload["success"]) is bool,
              "node_acceptance_output_invalid")
        _need(payload["success"] is True, "node_acceptance_tests_failed")
        for key in ("failed", "skipped", "todo", "cancelled"):
            _need(type(payload[key]) is int and payload[key] == 0, "node_acceptance_tests_failed")
        actual = payload["passed_tests"]
        _need(type(actual) is list, "node_acceptance_output_invalid")
        _need(_names(tuple(actual)) and set(actual) == set(expected), "node_acceptance_tests_failed")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("node_acceptance_output_invalid") from None
    canonical = json.dumps({"schema_version": 1, "passed_tests": expected},
                           ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    return MappingProxyType({"schema_version": 1, "count": len(expected), "names": expected,
                             "test_evidence_sha256": hashlib.sha256(canonical).hexdigest()})
