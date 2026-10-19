"""Source-bound blocked observations, never execution authority or delivery receipts."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import InitVar, dataclass, field
from types import MappingProxyType
from typing import Literal, cast

from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.runtime.contracts import JsonValue, _freeze_object
from agent_hub.skills.sandbox.base import SkillResult

ProjectPrerequisiteCategory = Literal[
    "missing_runtime", "approval", "unsupported", "integrity", "resource", "execution",
    "cleanup", "timeout", "unsafe_input",
]
ProjectPrerequisiteReason = Literal[
    "node_runtime_missing", "npm_runtime_missing", "systemd_authorization_required",
    "install_authorization_required", "runtime_access_denied", "linux_required",
    "unsupported_pnpm", "unsupported_python", "unsupported_project", "unsupported_workspaces",
    "unsupported_overrides", "unsupported_resolutions", "unsupported_package_manager",
    "unsupported_dev_engines", "node_runtime_untrusted", "npm_runtime_untrusted",
    "invalid_package_manifest", "input_lock_changed", "work_quota_exceeded", "deadline_exceeded",
    "cleanup_deadline_exceeded", "absolute_paths_required", "unsafe_path", "overlapping_paths",
    "work_not_empty", "unsafe_input_entry", "input_unavailable", "bwrap_unavailable",
    "node_probe_unavailable", "npm_probe_unavailable", "node_probe_failed", "npm_probe_failed",
    "user_namespace_unavailable", "workspace_mount_failed", "trusted_source_mount_failed",
    "node_mount_failed", "resolver_mount_failed", "hosts_mount_failed", "certificates_mount_failed",
    "lib64_mount_failed", "lib_mount_failed", "usr_mount_failed", "proc_mount_failed",
    "devices_mount_failed", "mount_propagation_failed", "tmpfs_mount_failed", "sandbox_mount_failed",
    "phase_launch_unavailable",
]

_REASON_GROUPS: Mapping[ProjectPrerequisiteCategory, tuple[ProjectPrerequisiteReason, ...]] = {
    "missing_runtime": ("node_runtime_missing", "npm_runtime_missing"),
    "approval": ("systemd_authorization_required", "install_authorization_required",
                 "runtime_access_denied"),
    "unsupported": ("linux_required", "unsupported_pnpm", "unsupported_python",
                    "unsupported_project", "unsupported_workspaces", "unsupported_overrides",
                    "unsupported_resolutions", "unsupported_package_manager", "unsupported_dev_engines"),
    "integrity": ("node_runtime_untrusted", "npm_runtime_untrusted", "invalid_package_manifest",
                  "input_lock_changed"),
    "resource": ("work_quota_exceeded",),
    "timeout": ("deadline_exceeded",),
    "cleanup": ("cleanup_deadline_exceeded",),
    "unsafe_input": ("absolute_paths_required", "unsafe_path", "overlapping_paths", "work_not_empty",
                     "unsafe_input_entry"),
    "execution": ("input_unavailable", "bwrap_unavailable", "node_probe_unavailable",
                  "npm_probe_unavailable", "node_probe_failed", "npm_probe_failed",
                  "user_namespace_unavailable", "workspace_mount_failed", "trusted_source_mount_failed",
                  "node_mount_failed", "resolver_mount_failed", "hosts_mount_failed",
                  "certificates_mount_failed", "lib64_mount_failed", "lib_mount_failed",
                  "usr_mount_failed", "proc_mount_failed", "devices_mount_failed",
                  "mount_propagation_failed", "tmpfs_mount_failed", "sandbox_mount_failed",
                  "phase_launch_unavailable"),
}
_CATEGORIES: Mapping[ProjectPrerequisiteReason, ProjectPrerequisiteCategory] = MappingProxyType({
    reason: category for category, reasons in _REASON_GROUPS.items() for reason in reasons
})
_PHASES = ("install", "build", "test")
_BEFORE_SOURCE_REASONS = frozenset({
    "absolute_paths_required", "unsafe_path", "overlapping_paths", "work_not_empty",
    "input_unavailable",
})
_PREFIX_REASONS = frozenset({
    "phase_launch_unavailable", "deadline_exceeded", "work_quota_exceeded",
})
_SHA = re.compile(r"[a-f0-9]{64}")
_VERSIONS = {
    "node": re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?"),
    "npm": re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?"),
}
_REPORT_FIELDS = {"schema_version", "status", "source_sha256", "reason", "checks", "runtime"}
_RESULT_FIELDS = ("exit_code", "timed_out", "stdout", "stderr")
_METADATA_FIELDS = {"sanitized_bytes", "returned_bytes", "sha256", "truncated", "redacted",
                    "redacted_lines"}
_DIAGNOSTIC_FIELDS = {"schema_version", "backend", "exit_code", "timed_out",
                      "stdout_metadata", "stderr_metadata"}
_EMPTY_SHA = hashlib.sha256(b"").hexdigest()
_ERROR = "invalid project prerequisite report"


def _need(condition: object) -> None:
    if not condition:
        raise ValueError(_ERROR)


def _object(value: object) -> Mapping[str, object]:
    _need(type(value) in (dict, MappingProxyType))
    result = cast(Mapping[str, object], value)
    _need(all(type(key) is str for key in result))
    return result


def _source(value: object) -> str:
    _need(type(value) is str)
    result = cast(str, value)
    _need(_SHA.fullmatch(result) is not None)
    return result


def _reason(value: object) -> ProjectPrerequisiteReason:
    _need(type(value) is str and value in _CATEGORIES)
    return cast(ProjectPrerequisiteReason, value)


def _runtime(value: object, *, drop_empty: bool) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    runtime = _object(value)
    _need(not set(runtime) - _VERSIONS.keys())
    for name, version in runtime.items():
        _need(type(version) is str)
        text = cast(str, version)
        if drop_empty and text == "":
            continue
        _need(len(text) <= 128 and _VERSIONS[name].fullmatch(text) is not None)
        result[name] = text
    return result


def _outcome(status: object, exit_code: object, timed_out: object) -> None:
    # Runner v1 never changes a failed report to blocked, even during cleanup.
    _need(type(status) is str and status == "passed")
    _need(type(exit_code) is int and exit_code == 0)
    _need(type(timed_out) is bool and timed_out is False)


def _sequence(statuses: Sequence[str], reason: ProjectPrerequisiteReason) -> None:
    passed = statuses.count("passed")
    _need(tuple(statuses) == ("passed",) * passed + ("not_run",) * (3 - passed))
    # These reasons can only be emitted before the source hash is assigned.
    _need(reason not in _BEFORE_SOURCE_REASONS)
    if reason == "cleanup_deadline_exceeded":
        return
    if reason == "input_lock_changed":
        _need(passed == 1)
    elif reason == "unsafe_input_entry":
        # Lock reads before/after install can reject unsafe entries too.
        _need(passed <= 1)
    elif reason in _PREFIX_REASONS:
        _need(passed < 3)
    else:
        _need(passed == 0)


def _metadata(value: object) -> None:
    metadata = _object(value)
    _need(set(metadata) == _METADATA_FIELDS)
    for key in ("sanitized_bytes", "returned_bytes", "redacted_lines"):
        number = metadata[key]
        _need(type(number) is int and 0 <= number <= 8192)
    sanitized = cast(int, metadata["sanitized_bytes"])
    returned = cast(int, metadata["returned_bytes"])
    redacted_lines = cast(int, metadata["redacted_lines"])
    _need(returned <= min(sanitized, 4096) and sanitized + redacted_lines <= 8192)
    _need(type(metadata["truncated"]) is bool and type(metadata["redacted"]) is bool)
    _need(metadata["redacted"] == (redacted_lines > 0))
    digest = _source(metadata["sha256"])
    _need((digest == _EMPTY_SHA) == (sanitized == 0))
    if metadata["truncated"]:
        # UTF-8 prefix clipping may discard at most three bytes before the marker.
        _need(sanitized > 4096 and 4093 <= returned <= 4096)
    else:
        _need(sanitized <= 4096 and returned == sanitized)


def _payload(value: object) -> Mapping[str, JsonValue]:
    report = _object(value)
    _need(set(report) == _REPORT_FIELDS | {"category"})
    _need(type(report["schema_version"]) is int and report["schema_version"] == 1)
    _need(type(report["status"]) is str and report["status"] == "blocked")
    _source(report["source_sha256"])
    reason = _reason(report["reason"])
    _need(type(report["category"]) is str and report["category"] == _CATEGORIES[reason])
    runtime = _runtime(report["runtime"], drop_empty=False)
    checks = report["checks"]
    _need(type(checks) in (list, tuple) and len(cast(Sequence[object], checks)) == 3)
    statuses: list[str] = []
    for phase, item in zip(_PHASES, cast(Sequence[object], checks), strict=True):
        check = _object(item)
        _need(type(check.get("phase")) is str and check["phase"] == phase)
        status = check.get("status")
        _need(type(status) is str)
        statuses.append(cast(str, status))
        if status == "not_run":
            _need(set(check) == {"phase", "status"})
            continue
        _need(set(check) == {"phase", "status", "execution_diagnostic"})
        diagnostic = _object(check["execution_diagnostic"])
        _need(set(diagnostic) == _DIAGNOSTIC_FIELDS)
        _need(type(diagnostic["schema_version"]) is int and diagnostic["schema_version"] == 1)
        _need(type(diagnostic["backend"]) is str and diagnostic["backend"] == "systemd")
        _outcome(status, diagnostic["exit_code"], diagnostic["timed_out"])
        _metadata(diagnostic["stdout_metadata"])
        _metadata(diagnostic["stderr_metadata"])
    _sequence(statuses, reason)
    _need(all(status == "not_run" for status in statuses) or set(runtime) == {"node", "npm"})
    return _freeze_object(report, name="project prerequisite observation")


def _raw_payload(raw_report: object, expected_source: str) -> Mapping[str, JsonValue]:
    _need(type(raw_report) is dict)
    report = _object(raw_report)
    _need(set(report) == _REPORT_FIELDS)
    _need(type(report["schema_version"]) is int and report["schema_version"] == 1)
    _need(type(report["status"]) is str and report["status"] == "blocked")
    _need(_source(report["source_sha256"]) == _source(expected_source))
    reason = _reason(report["reason"])
    runtime = _runtime(report["runtime"], drop_empty=True)
    checks = report["checks"]
    _need(type(checks) is list and len(cast(list[object], checks)) == 3)
    safe_checks: list[JsonValue] = []
    for phase, value in zip(_PHASES, cast(list[object], checks), strict=True):
        _need(type(value) is dict)
        check = _object(value)
        _need(set(check) == {"phase", "status", *_RESULT_FIELDS})
        _need(type(check["phase"]) is str and check["phase"] == phase)
        status = check["status"]
        _need(type(status) is str)
        result = SkillResult.model_validate({key: check[key] for key in _RESULT_FIELDS}, strict=True)
        _need(all(len(stream.encode("utf-8")) <= 8192 for stream in (result.stdout, result.stderr)))
        if status == "not_run":
            _need(result.exit_code is None and not result.timed_out and not result.stdout
                  and not result.stderr)
            safe_checks.append({"phase": phase, "status": "not_run"})
            continue
        _outcome(status, result.exit_code, result.timed_out)
        diagnostic = ExecutionDiagnostic(backend="systemd", result=result).to_payload()
        safe_checks.append({"phase": phase, "status": cast(str, status), "execution_diagnostic": {
            key: item for key, item in diagnostic.items() if key not in {"stdout", "stderr"}
        }})
    return _payload({
        "schema_version": 1, "status": "blocked", "source_sha256": expected_source,
        "reason": reason, "category": _CATEGORIES[reason], "checks": safe_checks, "runtime": runtime,
    })


@dataclass(frozen=True, slots=True)
class ProjectPrerequisiteObservation:
    """Validated blocked report; source equality is binding, not proof of runner provenance."""

    raw_report: InitVar[object]
    expected_source: InitVar[str]
    source_sha256: str = field(init=False)
    reason: ProjectPrerequisiteReason = field(init=False)
    category: ProjectPrerequisiteCategory = field(init=False)
    _report: Mapping[str, JsonValue] = field(init=False, repr=False)

    def __post_init__(self, raw_report: object, expected_source: str) -> None:
        try:
            report = _raw_payload(raw_report, expected_source)
        except (ValueError, TypeError, RecursionError):
            raise ValueError(_ERROR) from None
        object.__setattr__(self, "source_sha256", expected_source)
        object.__setattr__(self, "reason", report["reason"])
        object.__setattr__(self, "category", report["category"])
        object.__setattr__(self, "_report", report)

    def to_payload(self) -> Mapping[str, JsonValue]:
        return self._report


def validate_project_prerequisite_report(
    raw_report: object, expected_source: str,
) -> ProjectPrerequisiteObservation:
    """Validate decoded runner fields and capture sanitized process metadata once."""
    return ProjectPrerequisiteObservation(raw_report=raw_report, expected_source=expected_source)


def validated_project_prerequisite_payload(value: object) -> Mapping[str, JsonValue] | None:
    """Revalidate persisted metadata without inventing streams or a primary diagnostic."""
    try:
        return _payload(value)
    except (ValueError, TypeError, RecursionError):
        return None


def project_prerequisite_reason_category(reason: object) -> ProjectPrerequisiteCategory | None:
    """Classify an exact public reason; this grants no execution or install authority."""
    if type(reason) is not str:
        return None
    return _CATEGORIES.get(cast(ProjectPrerequisiteReason, reason))
