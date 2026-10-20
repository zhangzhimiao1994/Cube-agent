"""Controller-only fixed dummy probe; no pytest, project hooks, units or grants.

Load with runpy.run_path and call run_native_capture_regression(inputs=inputs).
The supplied trusted recipe is used only for synthetic decoder expectations;
no wheel is installed, no cache is accessed, and no native receipt is issued.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from agent_hub.capabilities.python_project_inputs import PythonProjectInputs
from agent_hub.capabilities.python_project_reports import (
    PythonProjectFailure,
    decode_python_project_report,
)
from agent_hub.skills.sandbox.base import SkillResult
from agent_hub.skills.sandbox.python_project_process import (
    PYTHON_PHASE_OUTPUT_WITHHELD,
    python_native_report_limit,
    python_phase_capture_limit,
    python_root_response_limit,
    run_python_phase,
)
from agent_hub.skills.sandbox.python_project_protocol import PythonProjectInvocation

_DUMMY = r'''
import os, sys
os.write(1, b'{"fixture":"fixed_dummy","protocol":"intact"}\n')
case = sys.argv[1]
if case == 'half_line':
    os.write(2, b'progress ok\n' * 4000 + b'API_')
    os.write(2, b'KEY=' + b'q' * 60000 + b'synthetic-secret-sentinel\n')
elif case == 'multiline':
    os.write(2, b'progress ok\n' * 4000 + b'API_')
    os.write(2, b'KEY=|\n' + b'  synthetic-secret-sentinel\n' * 6000)
elif case == 'complete':
    os.write(2, b'progress ok\n' * 500)
    os.write(2, b'API_KEY=synthetic-secret-sentinel\n')
else:
    raise SystemExit(99)
os.write(2, b'ActualFailure: mismatch\n')
raise SystemExit(7)
'''
_OUTER = "import os,sys; os.write(1,sys.argv[1].encode('utf-8'))"


def run_native_capture_regression(*, inputs: PythonProjectInputs) -> dict[str, object]:
    """Run only trusted dummy children and decode their synthetic failed reports.

    This is capture/protocol regression evidence, never native project execution,
    interpreter attestation, an original observation, or fresh delivery authority.
    Raises AssertionError on mismatch; returns no raw logs or source/host paths.
    """
    if sys.flags.optimize:
        raise RuntimeError("capture_regression_requires_assertions")
    inputs = PythonProjectInputs.from_payload(inputs.to_payload())
    contract = inputs.contract
    invocation = PythonProjectInvocation.model_validate({
        "execution_id": "capture_context_fixture",
        "tenant_id": contract.tenant_id,
        "run_id": contract.run_id,
        "project_id": contract.project_id,
        "session_id": contract.session_id,
        "original_reference": inputs.original_reference.to_payload(),
        "sandbox_profile": "workspace_write",
        "source_kind": "original",
        "install_allowed": True,
        "timeout_seconds": 180,
        "recipe_sha256": inputs.sha256,
        "environment_input_sha256": inputs.environment_input_sha256,
        "output_limit_bytes": 262144,
    }, strict=True)
    budget = invocation.output_limit_bytes
    cases: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="python-capture-fixed-dummy-") as temp:
        for case in ("half_line", "multiline", "complete"):
            phase = run_python_phase(
                [sys.executable, "-I", "-S", "-B", "-c", _DUMMY, case],
                cwd=Path(temp), deadline=time.monotonic() + 10,
                capture_limit_bytes=python_phase_capture_limit(budget),
            )
            assert phase.exit_code == 7 and not phase.timed_out
            assert not phase.stdout_truncated
            assert json.loads(phase.stdout) == {"fixture": "fixed_dummy", "protocol": "intact"}
            context_lost = case != "complete"
            assert phase.stderr_truncated is context_lost
            if context_lost:
                assert phase.stderr == PYTHON_PHASE_OUTPUT_WITHHELD
            raw = {
                "schema_version": 2, "kind": "python_project", "status": "failed",
                "evidence": "native_process_only", "execution_id": invocation.execution_id,
                "source_sha256": inputs.original_reference.input_source_sha256,
                "recipe_sha256": inputs.sha256,
                "environment_input_sha256": inputs.environment_input_sha256,
                "environment_sha256": None,
                "original_check_contract": contract.protected_sha256,
                "runtime": inputs.interpreter.to_payload(),
                "wheel": None, "test_count": None, "exported_environment": False,
                "dependency_install": {"mode": "created",
                                       "origin_execution_id": invocation.execution_id},
                "checks": [{
                    "phase": "install", "status": "failed", "exit_code": phase.exit_code,
                    "timed_out": phase.timed_out, "stdout": phase.stdout, "stderr": phase.stderr,
                    "stdout_truncated": phase.stdout_truncated,
                    "stderr_truncated": phase.stderr_truncated,
                    "stdout_observed_bytes": phase.stdout_observed_bytes,
                    "stderr_observed_bytes": phase.stderr_observed_bytes,
                }],
            }
            raw_json = json.dumps(raw, ensure_ascii=True, separators=(",", ":"))
            # Mirror the outer backend capture using its report-level budget,
            # not the much smaller inner phase-stream budget.
            outer = run_python_phase(
                [sys.executable, "-I", "-S", "-B", "-c", _OUTER, raw_json],
                cwd=Path(temp), deadline=time.monotonic() + 10,
                capture_limit_bytes=python_native_report_limit(budget),
            )
            assert outer.exit_code == 0 and not outer.timed_out
            assert not outer.stdout_truncated and outer.stdout == raw_json
            envelope = json.dumps({"result": {"stdout": outer.stdout}})
            assert len(envelope.encode()) <= python_root_response_limit(budget)
            assert json.loads(envelope)["result"]["stdout"] == raw_json
            failure = decode_python_project_report(
                invocation=invocation, inputs=inputs,
                expected_source_sha256=inputs.original_reference.input_source_sha256,
                environment_reference=None,
                result=SkillResult(exit_code=0, timed_out=False,
                                   stdout=outer.stdout, stderr=outer.stderr),
            )
            assert type(failure) is PythonProjectFailure and failure.diagnostic is not None
            diagnostic = failure.diagnostic.to_payload()
            disclosed = str(diagnostic["stderr"])
            assert "synthetic-secret-sentinel" not in json.dumps(diagnostic, default=dict)
            actual_error_visible = "ActualFailure: mismatch" in disclosed
            assert actual_error_visible is (not context_lost)
            cases.append({
                "case": case, "exit_code": phase.exit_code,
                "phase_stdout_json_intact": True, "raw_report_complete": True,
                "root_envelope_roundtrip": True, "secret_visible": False,
                "phase_context_lost": context_lost,
                "actual_error_visible": actual_error_visible,
            })
    return {"fixture_only": True, "project_code_executed": False, "cases": cases}
