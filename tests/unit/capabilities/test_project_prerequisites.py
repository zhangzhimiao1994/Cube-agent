from __future__ import annotations

import hashlib
import itertools
import json
import socket
import subprocess
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from agent_hub.capabilities.project_prerequisites import (
    ProjectPrerequisiteObservation,
    validate_project_prerequisite_report,
    validated_project_prerequisite_payload,
)
from agent_hub.skills.sandbox import project_runner

SOURCE = "a" * 64
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
PHASES = ("install", "build", "test")
RUNTIME = {"node": "v22.16.0", "npm": "10.9.2"}
REASONS = {
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


def raw_report(
    statuses: tuple[str, ...] = ("not_run", "not_run", "not_run"),
    *, reason: str = "node_runtime_missing",
) -> dict[str, Any]:
    return {
        "schema_version": 1, "status": "blocked", "source_sha256": SOURCE, "reason": reason,
        "checks": [{"phase": phase, "status": status,
                    "exit_code": None if status == "not_run" else 1 if status == "failed" else 0,
                    "timed_out": False, "stdout": "", "stderr": ""}
                   for phase, status in zip(PHASES, statuses, strict=True)],
        "runtime": dict(RUNTIME) if any(status != "not_run" for status in statuses)
        else {"node": "", "npm": ""},
    }


def empty_metadata() -> dict[str, Any]:
    return {"sanitized_bytes": 0, "returned_bytes": 0, "sha256": EMPTY_SHA,
            "truncated": False, "redacted": False, "redacted_lines": 0}


def persisted_report(
    statuses: tuple[str, ...] = ("passed", "not_run", "not_run"),
    *, reason: str = "phase_launch_unavailable",
) -> dict[str, Any]:
    return {
        "schema_version": 1, "status": "blocked", "source_sha256": SOURCE,
        "reason": reason,
        "category": next(category for category, reasons in REASONS.items() if reason in reasons),
        "runtime": dict(RUNTIME) if any(status != "not_run" for status in statuses) else {},
        "checks": [
            {"phase": phase, "status": status} if status == "not_run" else {
                "phase": phase, "status": status, "execution_diagnostic": {
                "schema_version": 1, "backend": "systemd",
                "exit_code": 1 if status == "failed" else 0, "timed_out": False,
                "stdout_metadata": empty_metadata(), "stderr_metadata": empty_metadata(),
            }}
            for phase, status in zip(PHASES, statuses, strict=True)
        ],
    }


@pytest.mark.parametrize("category,reason", [
    (category, reason) for category, reasons in REASONS.items() for reason in reasons
])
@pytest.mark.parametrize("passed_count", [0, 1, 2, 3])
def test_runner_v1_reason_must_match_the_actual_stage_in_both_entry_points(
    category: str, reason: str, passed_count: int,
) -> None:
    allowed_prefixes = {
        "absolute_paths_required": (), "unsafe_path": (), "overlapping_paths": (),
        "work_not_empty": (), "input_unavailable": (),
        "unsafe_input_entry": (0, 1), "input_lock_changed": (1,),
        "phase_launch_unavailable": (0, 1, 2), "deadline_exceeded": (0, 1, 2),
        "work_quota_exceeded": (0, 1, 2), "cleanup_deadline_exceeded": (0, 1, 2, 3),
    }
    statuses = ("passed",) * passed_count + ("not_run",) * (3 - passed_count)
    raw = raw_report(statuses, reason=reason)
    persisted = persisted_report(statuses, reason=reason)
    if passed_count not in allowed_prefixes.get(reason, (0,)):
        with pytest.raises(ValueError, match="invalid project prerequisite report"):
            validate_project_prerequisite_report(raw, SOURCE)
        assert validated_project_prerequisite_payload(persisted) is None
        return
    observation = validate_project_prerequisite_report(raw, SOURCE)
    assert type(observation) is ProjectPrerequisiteObservation
    assert observation.reason == reason and observation.category == category
    assert observation.source_sha256 == SOURCE
    assert observation.to_payload() == validated_project_prerequisite_payload(persisted)


@pytest.mark.parametrize("statuses", list(itertools.product(("passed", "failed", "not_run"), repeat=3)))
@pytest.mark.parametrize("reason", ["phase_launch_unavailable", "deadline_exceeded",
                                   "cleanup_deadline_exceeded"])
def test_blocked_rejects_every_failed_phase_or_out_of_order_prefix(
    statuses: tuple[str, ...], reason: str,
) -> None:
    allowed = {
        ("not_run", "not_run", "not_run"), ("passed", "not_run", "not_run"),
        ("passed", "passed", "not_run"),
    }
    if reason == "cleanup_deadline_exceeded":
        allowed.add(("passed", "passed", "passed"))
    raw = raw_report(statuses, reason=reason)
    persisted = persisted_report(statuses, reason=reason)
    if statuses not in allowed:
        with pytest.raises(ValueError, match="invalid project prerequisite report"):
            validate_project_prerequisite_report(raw, SOURCE)
        assert validated_project_prerequisite_payload(persisted) is None
        return
    payload = validate_project_prerequisite_report(raw, SOURCE).to_payload()
    assert validated_project_prerequisite_payload(persisted) == payload
    checks = payload["checks"]
    assert isinstance(checks, tuple)
    assert tuple(check["status"] for check in checks if isinstance(check, Mapping)) == statuses
    assert payload["status"] == "blocked"


def test_cleanup_can_observe_all_passed_without_creating_a_receipt() -> None:
    payload = validate_project_prerequisite_report(
        raw_report(("passed",) * 3, reason="cleanup_deadline_exceeded"), SOURCE,
    ).to_payload()
    assert payload["status"] == "blocked" and payload["category"] == "cleanup"
    assert set(payload) == {"schema_version", "status", "source_sha256", "reason", "category",
                            "checks", "runtime"}
    assert validated_project_prerequisite_payload(payload) == payload


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 1.0), ("schema_version", 2),
    ("status", "passed"), ("status", "failed"), ("source_sha256", "b" * 64),
    ("source_sha256", ""), ("reason", "node_runtime_installed"),
    ("reason", "phase_deadline_exceeded"), ("reason", "phase_output_limit_exceeded"),
    ("reason", "node_probe_failed private echo"), ("reason", None),
])
def test_raw_schema_source_status_and_reason_are_fail_closed(field: str, value: object) -> None:
    raw = raw_report()
    raw[field] = value
    with pytest.raises(ValueError, match="invalid project prerequisite report"):
        validate_project_prerequisite_report(raw, SOURCE)


@pytest.mark.parametrize("expected", ["", "A" * 64, "g" * 64, "a" * 63, None, True])
def test_expected_source_itself_must_be_a_lowercase_sha256(expected: Any) -> None:
    with pytest.raises(ValueError, match="invalid project prerequisite report"):
        validate_project_prerequisite_report(raw_report(), expected)


@pytest.mark.parametrize("extra", ["permissions", "install_allowed", "receipt", "installed",
                                  "execution_diagnostic", "verification_failure", "category"])
def test_raw_cannot_inject_authority_success_or_prebuilt_diagnostics(extra: str) -> None:
    raw = raw_report()
    raw[extra] = {"status": "passed", "network.read": True}
    with pytest.raises(ValueError, match="invalid project prerequisite report"):
        validate_project_prerequisite_report(raw, SOURCE)


@pytest.mark.parametrize("status,field,value", [
    ("not_run", "exit_code", 0), ("not_run", "timed_out", True),
    ("not_run", "stdout", "private source"), ("not_run", "stderr", "not executed"),
    ("passed", "exit_code", True), ("passed", "exit_code", "0"),
    ("passed", "exit_code", 1), ("passed", "timed_out", True),
    ("passed", "timed_out", 0), ("passed", "stdout", None),
    ("passed", "stderr", b"bytes"), ("passed", "stdout", "x" * 8193),
    ("failed", "exit_code", 0), ("failed", "exit_code", None),
    ("passed", "execution_diagnostic", {}), ("passed", "output_limited", False),
])
def test_raw_result_requires_strict_skill_result_and_consistent_status(
    status: str, field: str, value: object,
) -> None:
    raw = raw_report((status, "not_run", "not_run"), reason="phase_launch_unavailable")
    raw["checks"][0][field] = value
    with pytest.raises(ValueError, match="invalid project prerequisite report"):
        validate_project_prerequisite_report(raw, SOURCE)


@pytest.mark.parametrize("exit_code", [None, -9, 0])
def test_a_timed_out_executed_phase_cannot_be_a_blocked_report(exit_code: int | None) -> None:
    raw = raw_report(("failed", "not_run", "not_run"), reason="deadline_exceeded")
    raw["checks"][0].update(exit_code=exit_code, timed_out=True)
    with pytest.raises(ValueError, match="invalid project prerequisite report"):
        validate_project_prerequisite_report(raw, SOURCE)
    persisted = persisted_report(("failed", "not_run", "not_run"), reason="deadline_exceeded")
    persisted["checks"][0]["execution_diagnostic"].update(exit_code=exit_code, timed_out=True)
    assert validated_project_prerequisite_payload(persisted) is None


def test_safe_diagnostic_metadata_is_generated_once_without_retaining_streams() -> None:
    raw = raw_report(("passed", "passed", "not_run"), reason="cleanup_deadline_exceeded")
    raw["checks"][1].update(stdout="x" * 5000, stderr="password=synthetic-private-value\n")
    observation = validate_project_prerequisite_report(raw, SOURCE)
    payload = observation.to_payload()
    check = payload["checks"]
    assert isinstance(check, tuple) and isinstance(check[1], Mapping)
    diagnostic = check[1]["execution_diagnostic"]
    assert isinstance(diagnostic, Mapping)
    assert set(diagnostic) == {"schema_version", "backend", "exit_code", "timed_out",
                               "stdout_metadata", "stderr_metadata"}
    assert diagnostic["stdout_metadata"] == {
        "sanitized_bytes": 5000, "returned_bytes": 4096,
        "sha256": hashlib.sha256(b"x" * 5000).hexdigest(), "truncated": True,
        "redacted": False, "redacted_lines": 0,
    }
    assert diagnostic["stderr_metadata"] == {
        **empty_metadata(), "redacted": True, "redacted_lines": 1,
    }
    assert "synthetic-private-value" not in repr(payload) + repr(observation)
    assert "x" * 100 not in repr(payload) + repr(observation)
    assert validated_project_prerequisite_payload(payload) == payload


@pytest.mark.parametrize("runtime", [
    {"python": "3.12.0"}, {"node": "22.16.0"}, {"npm": "v10.9.2"},
    {"node": "v22.16"}, {"node": "v22.16.0\n"}, {"node": None},
    {"npm": 10}, {"node": "v22.16.0+private"}, {"node": "v1.2.3-" + "a" * 128},
])
def test_unobserved_keys_and_bad_runtime_versions_are_rejected(runtime: object) -> None:
    raw = raw_report()
    raw["runtime"] = runtime
    with pytest.raises(ValueError, match="invalid project prerequisite report"):
        validate_project_prerequisite_report(raw, SOURCE)


def test_runtime_empty_placeholders_are_removed_and_input_is_deeply_detached() -> None:
    raw = raw_report()
    raw["runtime"]["node"] = "v22.16.0-rc.1"
    observation = validate_project_prerequisite_report(raw, SOURCE)
    assert observation.to_payload()["runtime"] == {"node": "v22.16.0-rc.1"}
    raw["runtime"]["node"] = "changed"
    raw["checks"][0]["phase"] = "changed"
    assert observation.to_payload()["runtime"] == {"node": "v22.16.0-rc.1"}
    mutable_observation: Any = observation
    with pytest.raises(FrozenInstanceError):
        mutable_observation.reason = "installed"
    mutable: Any = observation.to_payload()
    with pytest.raises(TypeError):
        mutable["runtime"]["node"] = "changed"
    with pytest.raises(TypeError):
        mutable["checks"][0]["status"] = "passed"


def test_clean_payload_revalidation_is_separate_from_raw_factory() -> None:
    expected = persisted_report()
    frozen = validated_project_prerequisite_payload(expected)
    assert frozen == validate_project_prerequisite_report(
        raw_report(("passed", "not_run", "not_run"), reason="phase_launch_unavailable"), SOURCE,
    ).to_payload()
    assert frozen is not None
    roundtrip = json.loads(json.dumps(frozen, default=dict))
    assert validated_project_prerequisite_payload(roundtrip) == frozen
    expected["runtime"]["node"] = "changed"
    assert frozen["runtime"] == RUNTIME
    assert validated_project_prerequisite_payload(raw_report()) is None
    with pytest.raises(ValueError, match="invalid project prerequisite report"):
        ProjectPrerequisiteObservation(raw_report=persisted_report(), expected_source=SOURCE)


@pytest.mark.parametrize("field,value", [
    ("status", "passed"), ("status", "failed"), ("category", "approval"),
    ("reason", "runtime_installed"), ("schema_version", True), ("source_sha256", "A" * 64),
    ("permissions", []), ("receipt", {}), ("execution_diagnostic", {}),
    ("verification_failure", {}), ("runtime", {"node": ""}),
])
def test_persisted_payload_rejects_forged_schema_category_authority_and_unknown_fields(
    field: str, value: object,
) -> None:
    payload = persisted_report()
    payload[field] = value
    assert validated_project_prerequisite_payload(payload) is None


@pytest.mark.parametrize("field,value", [
    ("sanitized_bytes", True), ("sanitized_bytes", -1), ("sanitized_bytes", 8193),
    ("returned_bytes", 1), ("returned_bytes", 4097), ("sha256", "g" * 64),
    ("sha256", "a" * 64), ("truncated", True), ("redacted", True),
    ("redacted_lines", 1), ("redacted_lines", False), ("redacted_lines", 8193),
    ("stdout", "forged stream"),
])
def test_persisted_stream_metadata_rejects_impossible_types_hashes_counts_and_flags(
    field: str, value: object,
) -> None:
    payload = persisted_report()
    payload["checks"][0]["execution_diagnostic"]["stdout_metadata"][field] = value
    assert validated_project_prerequisite_payload(payload) is None


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("backend", "docker"), ("exit_code", True),
    ("exit_code", 1), ("timed_out", 1), ("timed_out", True),
    ("stdout", "fake"), ("stderr", "private"), ("extra", {}),
])
def test_persisted_phase_diagnostics_are_metadata_only_and_consistent(field: str, value: object) -> None:
    payload = persisted_report()
    payload["checks"][0]["execution_diagnostic"][field] = value
    assert validated_project_prerequisite_payload(payload) is None


def test_persisted_payload_rechecks_phase_order_not_run_shape_and_failed_outcome() -> None:
    variants: list[object] = []
    changed = persisted_report()
    changed["checks"][1]["stdout"] = ""
    variants.append(changed)
    changed = persisted_report()
    changed["checks"].reverse()
    variants.append(changed)
    changed = persisted_report()
    changed["checks"][0]["status"] = "failed"
    variants.append(changed)
    changed = persisted_report()
    changed["checks"][0], changed["checks"][1] = changed["checks"][1], changed["checks"][0]
    variants.append(changed)
    values: list[object] = [*variants, None, [], "{}", deepcopy(raw_report())]
    for value in values:
        assert validated_project_prerequisite_payload(value) is None


def test_real_runner_blocked_shape_can_be_observed_without_execution_or_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def prohibited(*args: object, **kwargs: object) -> Any:
        pytest.fail("pure observation must not run commands, use network or write files")

    monkeypatch.setattr(socket, "socket", prohibited)
    monkeypatch.setattr(subprocess, "Popen", prohibited)
    monkeypatch.setattr(Path, "write_text", prohibited)
    monkeypatch.setattr(project_runner, "_prepare_work", lambda *args: None)
    monkeypatch.setattr(project_runner, "_copy_snapshot", lambda *args: SOURCE)
    monkeypatch.setattr(project_runner, "_manifest", lambda *args: {})
    monkeypatch.setattr(project_runner, "_load_sandbox", dict)
    monkeypatch.setattr(project_runner, "_backend_reason", lambda sandbox: "node_runtime_missing")
    monkeypatch.setattr(project_runner, "_cleanup_work", lambda *args: None)
    report = project_runner.run_project(Path("/input"), Path("/work"), 10)
    payload = validate_project_prerequisite_report(report, SOURCE).to_payload()
    assert payload["status"] == "blocked" and payload["reason"] == "node_runtime_missing"
    assert validated_project_prerequisite_payload(payload) == payload
