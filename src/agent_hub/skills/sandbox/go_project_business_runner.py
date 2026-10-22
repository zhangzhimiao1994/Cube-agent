"""One isolated native CLI observation, never a receipt or whole-project PASS.

Only a caller-owned systemd DynamicUser may use this helper. The caller supplies
fresh contract-bound work and owns real timeout/cgroup/storage containment.
Unavailable boundaries raise GoBusinessNotAvailable without a host fallback.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

from agent_hub.capabilities.go_project_business import GoBusinessCase
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

from .go_project_business_mounts import (
    GoBusinessCleanupIncomplete,
    GoBusinessNotAvailable,
    build_go_business_mounts,
)
from .python_project_process import (
    PYTHON_PHASE_OUTPUT_WITHHELD,
    PythonPhaseCleanupError,
    PythonPhaseProcessResult,
    run_python_phase,
)

__all__ = ['GoBusinessCleanupIncomplete', 'GoBusinessNotAvailable', 'GoBusinessObservation',
           'build_go_business_mounts', 'run_go_business_case']

Reason = Literal['timeout', 'exit_mismatch', 'stdout_mismatch', 'stderr_mismatch', 'output_incomplete']


@dataclass(frozen=True, slots=True)
class GoBusinessObservation:
    case_id: str
    criterion_id: str
    case_sha256: str
    expected_exit: int
    exit_code: int | None
    timed_out: bool
    stdout_sha256: str | None
    stderr_sha256: str | None
    stdout_observed_bytes: int
    stderr_observed_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_utf8_complete: bool
    stderr_utf8_complete: bool
    stdout_matches: bool
    stderr_matches: bool
    passed: bool
    reason: Reason | None

    def __post_init__(self) -> None:
        values = (self.timed_out, self.stdout_truncated, self.stderr_truncated,
                  self.stdout_utf8_complete, self.stderr_utf8_complete,
                  self.stdout_matches, self.stderr_matches, self.passed)
        if any(type(value) is not bool for value in values):
            raise ValueError('go_business_observation_invalid')

    def to_payload(self) -> dict[str, object]:
        """Fixed safe metadata fields; complete business streams are never returned."""
        return asdict(self)


def _stream(
    text: str, observed: int, truncated: bool, expected: bytes, maximum: int,
) -> tuple[bool, str | None, bool]:
    if (type(text) is not str or type(observed) is not int or observed < 0
            or type(truncated) is not bool):
        raise ValueError('go_business_capture_invalid')
    try:
        raw = text.encode('utf-8')
    except UnicodeError:
        return False, None, False
    # The shared collector ignores invalid UTF-8. Any dropped byte changes the
    # count, so only a full round trip can carry a hash or match expectation.
    complete = not truncated and observed == len(raw) <= maximum
    return complete, hashlib.sha256(raw).hexdigest() if complete else None, complete and raw == expected


def run_go_business_case(
    case: GoBusinessCase, *, work_root: Path, binary_root: Path,
    expected_binary_sha256: str, expected_binary_bytes: int,
    contract_arguments: Mapping[str, object], deadline: float, output_limit_bytes: int,
    on_collected: Callable[[PythonPhaseProcessResult], None] | None = None,
) -> GoBusinessObservation:
    """Compare actual native exit/complete UTF-8 bytes with protected case data.

    deadline is an absolute monotonic timestamp; output_limit_bytes is the
    complete per-stream capture budget. Suite/fixture budgets belong to callers.
    Cleanup uncertainty propagates as ProcessCleanupIncomplete, with pins held
    by GoBusinessCleanupIncomplete until outer cleanup is confirmed.
    An optional trusted caller sink may retain bounded diagnostics after confirmed
    drain and pin checks; those streams are not part of the receipt observation.
    """
    if type(case) is not GoBusinessCase:
        raise ValueError('go_business_case_invalid')
    if on_collected is not None and not callable(on_collected):
        raise ValueError('go_business_diagnostic_sink_invalid')
    case = GoBusinessCase.from_payload(case.to_payload())
    if type(output_limit_bytes) is not int or output_limit_bytes <= 0:
        raise ValueError('go_business_output_budget_invalid')
    try:
        valid_deadline = type(deadline) in {int, float} and math.isfinite(deadline) and deadline > 0
    except OverflowError:
        valid_deadline = False
    if not valid_deadline:
        raise ValueError('go_business_deadline_invalid')
    canonical = json.dumps(case.to_payload(), sort_keys=True, separators=(',', ':'),
                           ensure_ascii=True, allow_nan=False).encode('ascii')
    with build_go_business_mounts(case=case, work_root=work_root, binary_root=binary_root,
            expected_binary_sha256=expected_binary_sha256, expected_binary_bytes=expected_binary_bytes,
            contract_arguments=contract_arguments) as plan:
        try:
            result = run_python_phase(plan.argv, cwd=work_root, deadline=deadline,
                                      capture_limit_bytes=output_limit_bytes, pass_fds=plan.pass_fds)
        except PythonPhaseCleanupError as error:
            raise ProcessCleanupIncomplete('go_business_cleanup_incomplete') from error
    if (type(result.timed_out) is not bool
            or result.exit_code is not None and type(result.exit_code) is not int):
        raise ValueError('go_business_capture_invalid')
    stdout_complete, stdout_sha, stdout_matches = _stream(
        result.stdout, result.stdout_observed_bytes, result.stdout_truncated,
        case.expected_stdout.encode('utf-8'), output_limit_bytes)
    stderr_complete, stderr_sha, stderr_matches = _stream(
        result.stderr, result.stderr_observed_bytes, result.stderr_truncated,
        case.expected_stderr.encode('utf-8'), output_limit_bytes)
    reason: Reason | None = None
    if result.timed_out:
        reason = 'timeout'
    elif result.exit_code is None or not stdout_complete or not stderr_complete:
        reason = 'output_incomplete'
    elif result.exit_code != case.expected_exit:
        reason = 'exit_mismatch'
    elif not stdout_matches:
        reason = 'stdout_mismatch'
    elif not stderr_matches:
        reason = 'stderr_mismatch'
    observation = GoBusinessObservation(
        case_id=case.id, criterion_id=case.criterion_id, case_sha256=hashlib.sha256(canonical).hexdigest(),
        expected_exit=case.expected_exit, exit_code=result.exit_code, timed_out=result.timed_out,
        stdout_sha256=stdout_sha, stderr_sha256=stderr_sha,
        stdout_observed_bytes=result.stdout_observed_bytes, stderr_observed_bytes=result.stderr_observed_bytes,
        stdout_truncated=result.stdout_truncated, stderr_truncated=result.stderr_truncated,
        stdout_utf8_complete=stdout_complete, stderr_utf8_complete=stderr_complete,
        stdout_matches=stdout_matches, stderr_matches=stderr_matches, passed=reason is None, reason=reason,
    )
    if on_collected is not None:
        # An incomplete preview is not the actual stream. Preserve original
        # counters/flags in the observation, but mark diagnostic text withheld.
        withheld = PYTHON_PHASE_OUTPUT_WITHHELD[:output_limit_bytes]
        on_collected(replace(result,
            stdout=result.stdout if stdout_complete else withheld,
            stderr=result.stderr if stderr_complete else withheld,
            stdout_truncated=result.stdout_truncated or not stdout_complete,
            stderr_truncated=result.stderr_truncated or not stderr_complete))
    return observation
