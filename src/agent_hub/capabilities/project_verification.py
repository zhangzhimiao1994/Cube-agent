"""Task-authorized project verification and source-bound delivery receipts."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID, uuid4

from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.capabilities.project_prerequisites import (
    ProjectPrerequisiteObservation,
    validate_project_prerequisite_report,
)
from agent_hub.capabilities.scoped_read import authorized_workspace_scope
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runtime.contracts import JsonValue, _freeze_object
from agent_hub.skills.sandbox.base import SkillResult

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.project import ProjectInvocation

PROJECT_VERIFY_TOOL = "project.verify"
PROJECT_VERIFY_RESOURCE = "workspace/current/project-verification"
_SHA = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True, slots=True)
class ProjectVerificationFailure:
    """Validated failed execution metadata, not a successful delivery receipt."""

    report: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        report = _freeze_object(self.report, name="project verification failure")
        source = report.get("source_sha256")
        if (set(report) != {"schema_version", "status", "source_sha256", "checks", "runtime"}
            or type(report.get("schema_version")) is not int or report["schema_version"] != 1
            or report.get("status") != "failed" or not isinstance(source, str)
            or _SHA.fullmatch(source) is None):
            raise ValueError("invalid project verification failure")
        checks, runtime = report["checks"], report["runtime"]
        if (not isinstance(checks, tuple) or len(checks) != 3
            or not isinstance(runtime, Mapping) or set(runtime) - {"node", "npm"}
            or any(not isinstance(value, str)
                   or re.fullmatch(r"v?[0-9][A-Za-z0-9.+-]{0,63}", value) is None
                   for value in runtime.values())):
            raise ValueError("invalid project verification failure")
        failed = False
        for phase, check in zip(("install", "build", "test"), checks, strict=True):
            if not isinstance(check, Mapping) or check.get("phase") != phase:
                raise ValueError("invalid project verification failure")
            status = check.get("status")
            if status == "not_run":
                if not failed or set(check) != {"phase", "status"}:
                    raise ValueError("invalid project verification failure")
                continue
            diagnostic = check.get("execution_diagnostic")
            if (failed or status not in {"passed", "failed"}
                or set(check) != {"phase", "status", "execution_diagnostic"}
                or not isinstance(diagnostic, Mapping)
                or set(diagnostic) != {"schema_version", "backend", "exit_code", "timed_out",
                                       "stdout_metadata", "stderr_metadata"}
                or type(diagnostic["schema_version"]) is not int or diagnostic["schema_version"] != 1
                or diagnostic["backend"] != "systemd"
                or type(diagnostic["timed_out"]) is not bool
                or (diagnostic["exit_code"] is not None and type(diagnostic["exit_code"]) is not int)):
                raise ValueError("invalid project verification failure")
            if ((status == "passed" and (diagnostic["exit_code"] != 0 or diagnostic["timed_out"]))
                or (status == "failed" and not diagnostic["timed_out"]
                    and diagnostic["exit_code"] in (None, 0))):
                raise ValueError("invalid project verification failure")
            for stream in ("stdout_metadata", "stderr_metadata"):
                metadata = diagnostic[stream]
                if (not isinstance(metadata, Mapping)
                    or set(metadata) != {"sanitized_bytes", "returned_bytes", "sha256", "truncated",
                                         "redacted", "redacted_lines"}
                    or any(type(metadata[key]) is not int or not 0 <= cast(int, metadata[key]) <= 2**63 - 1
                           for key in ("sanitized_bytes", "returned_bytes", "redacted_lines"))
                    or cast(int, metadata["returned_bytes"]) > cast(int, metadata["sanitized_bytes"])
                    or any(type(metadata[key]) is not bool for key in ("truncated", "redacted"))
                    or not isinstance(metadata["sha256"], str)
                    or _SHA.fullmatch(metadata["sha256"]) is None):
                    raise ValueError("invalid project verification failure")
            failed = status == "failed"
        if not failed:
            raise ValueError("invalid project verification failure")
        object.__setattr__(self, "report", report)

    def to_payload(self) -> Mapping[str, JsonValue]:
        return self.report


def validated_project_failure_payload(
    value: object, *, diagnostic: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue] | None:
    """Validate persisted failure metadata and bind it to its process diagnostic."""
    if not isinstance(value, Mapping):
        return None
    try:
        report = ProjectVerificationFailure(value).to_payload()
        checks = cast(tuple[Mapping[str, JsonValue], ...], report["checks"])
        failed = next(check for check in checks if check["status"] == "failed")
        if failed["execution_diagnostic"] != {
            key: item for key, item in diagnostic.items() if key not in {"stdout", "stderr"}
        }:
            return None
        return report
    except (ValueError, TypeError, RecursionError):
        return None


class ProjectVerificationError(RuntimeError):
    def __init__(self, message: str, diagnostic: ExecutionDiagnostic | None = None, *,
                 project_failure: ProjectVerificationFailure | None = None,
                 project_prerequisite: ProjectPrerequisiteObservation | None = None) -> None:
        if project_prerequisite is not None and (
            type(project_prerequisite) is not ProjectPrerequisiteObservation
            or diagnostic is not None or project_failure is not None
        ):
            raise TypeError("project prerequisite must be a separate typed blocked observation")
        super().__init__(message)
        self.diagnostic = diagnostic
        self.project_failure = project_failure
        self.project_prerequisite = project_prerequisite


class ProjectExecutor(Protocol):
    async def run(self, invocation: ProjectInvocation) -> SkillResult: ...


class ProjectVerificationService:
    def __init__(self, *, root: Path, repository: object, executor: ProjectExecutor | None) -> None:
        self.root = root
        self.repository = repository
        self.executor = executor
        self._receipts: dict[tuple[UUID, UUID], tuple[ProjectInvocation, str]] = {}

    async def _invocation(
        self, tenant_id: UUID, run_id: UUID, actor: str,
    ) -> ProjectInvocation:
        from agent_hub.skills.sandbox.project import ProjectInvocation

        try:
            getter = getattr(self.repository, "get", None)
            if not callable(getter):
                raise TypeError
            record = await getter(tenant_id, run_id)
            project, session = authorized_workspace_scope(record, tenant_id=tenant_id, run_id=run_id)
            routing = getattr(record, "routing_decision", None)
            if not isinstance(routing, Mapping):
                raise TypeError
            permissions = routing.get("requested_permissions", ())
            profile = routing.get("sandbox_profile")
            if (profile not in {"restricted", "workspace_write"}
                or not isinstance(permissions, (tuple, list)) or "command.run" not in permissions):
                raise ValueError
            if routing.get("execution_backend", "systemd") != "systemd":
                raise ProjectVerificationError("project execution backend is unsupported")
            install_allowed = "network.read" in permissions
            if not install_allowed:
                actor_id = getattr(record, "actor_id", None)
                checker = getattr(self.repository, "is_capability_approval_scope_approved", None)
                if isinstance(actor_id, UUID) and callable(checker):
                    scope = capability_approval_scope(CapabilityRequest(
                        tenant_id=tenant_id, user_id=actor_id, agent_id=actor,
                        capability="command", operation="run", resource=PROJECT_VERIFY_RESOURCE,
                        arguments={}, run_id=run_id, idempotency_key="project-verification-scope",
                    ))
                    approved = checker(tenant_id, run_id, scope)
                    install_allowed = bool(
                        await cast(Awaitable[object], approved) if isawaitable(approved) else approved
                    )
            if not install_allowed:
                raise ProjectVerificationError("project dependency installation approval required")
            raw_timeout = routing.get("runtime_timeout_seconds", 300)
            if (not isinstance(raw_timeout, (int, float)) or isinstance(raw_timeout, bool)
                or not math.isfinite(raw_timeout) or raw_timeout <= 0):
                raise ValueError
            timeout = min(3600, max(1, math.ceil(raw_timeout)))
            return ProjectInvocation(
                execution_id="project_" + uuid4().hex, tenant_id=tenant_id, run_id=run_id,
                project_id=project, session_id=session, sandbox_profile=profile,
                install_allowed=True, timeout_seconds=timeout,
            )
        except ProjectVerificationError:
            raise
        except Exception:  # noqa: BLE001 - no repository/path details cross this boundary.
            raise ProjectVerificationError("project verification scope is not authorized") from None

    async def verify(self, *, tenant_id: UUID, run_id: UUID, actor: str) -> Mapping[str, JsonValue]:
        from agent_hub.skills.sandbox.project import (
            ProjectSystemdSandbox,
            project_source_fingerprint,
        )

        key = (tenant_id, run_id)
        self._receipts.pop(key, None)
        invocation = await self._invocation(tenant_id, run_id, actor)
        try:
            before = await project_source_fingerprint(self.root, invocation)
            try:
                result = await (self.executor or ProjectSystemdSandbox()).run(invocation)
            except Exception:  # noqa: BLE001 - executor exceptions are never trusted observations.
                # Only our decoded, source-checked report can create process evidence.
                raise ProjectVerificationError("project execution backend is unavailable") from None
            if result.exit_code != 0 or result.timed_out:
                raise ProjectVerificationError("project execution outcome is not confirmed")
            current = await project_source_fingerprint(self.root, invocation)
            if current != before:
                raise ProjectVerificationError("project source changed during verification")
            receipt = _validate_report(result.stdout, expected_source=before)
        except ProjectVerificationError:
            raise
        except Exception:  # noqa: BLE001 - backend failures are not trusted process evidence.
            raise ProjectVerificationError("project execution backend is unavailable") from None
        self._receipts[key] = (invocation, current)
        return {"verification": receipt, "summary": "Dependencies, build and tests verified in isolation."}

    async def require_current_receipt(self, tenant_id: UUID, run_id: UUID) -> str:
        from agent_hub.skills.sandbox.project import project_source_fingerprint

        record = self._receipts.get((tenant_id, run_id))
        if record is not None:
            invocation, expected = record
            try:
                if await project_source_fingerprint(self.root, invocation) == expected:
                    return expected
            except Exception:  # noqa: BLE001 - a stale/unreadable source cannot be delivered as verified.
                self._receipts.pop((tenant_id, run_id), None)
        raise ProjectVerificationError("project verification required before delivery")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _validate_report(text: str, *, expected_source: str) -> Mapping[str, JsonValue]:
    try:
        if len(text.encode("utf-8")) > 262144:
            raise ValueError
        report: object = json.loads(text, object_pairs_hook=_strict_object)
        if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
            or report.get("schema_version") != 1):
            raise ValueError
        if (report.get("source_sha256") != expected_source
            or _SHA.fullmatch(expected_source) is None):
            raise ValueError
        if report.get("status") == "blocked":
            prerequisite = validate_project_prerequisite_report(report, expected_source)
            raise ProjectVerificationError("project verification blocked by execution prerequisites",
                                           project_prerequisite=prerequisite)
        checks = report.get("checks")
        runtime = report.get("runtime")
        if not isinstance(checks, list) or len(checks) != 3 or not isinstance(runtime, dict):
            raise ValueError
        safe_checks: list[JsonValue] = []
        failure: tuple[str, ExecutionDiagnostic] | None = None
        for phase, check in zip(("install", "build", "test"), checks, strict=True):
            if not isinstance(check, dict) or check.get("phase") != phase:
                raise ValueError
            status = check.get("status")
            observed = SkillResult.model_validate({
                name: check[name] for name in ("exit_code", "timed_out", "stdout", "stderr")
            }, strict=True)
            if status not in {"passed", "failed", "not_run"}:
                raise ValueError
            if report.get("status") == "failed" and (
                (failure is None and status == "not_run")
                or (failure is not None and status != "not_run")
            ):
                raise ValueError
            if status == "not_run":
                if (observed.exit_code is not None or observed.timed_out
                    or observed.stdout or observed.stderr):
                    raise ValueError
                safe_checks.append({"phase": phase, "status": "not_run"})
                continue
            if status == "passed" and (observed.exit_code != 0 or observed.timed_out):
                raise ValueError
            diagnostic = ExecutionDiagnostic(backend="systemd", result=observed)
            safe_checks.append({"phase": phase, "status": status,
                                "execution_diagnostic": diagnostic.to_payload()})
            if status == "failed" and failure is None:
                if observed.exit_code in (0, None) and not observed.timed_out:
                    raise ValueError
                failure = (phase, diagnostic)
        if report.get("status") == "failed" and failure is not None:
            safe_failure_checks: list[JsonValue] = []
            for item in safe_checks:
                check = cast(Mapping[str, JsonValue], item)
                safe_check = dict(check)
                diagnostic_payload = check.get("execution_diagnostic")
                if isinstance(diagnostic_payload, Mapping):
                    safe_check["execution_diagnostic"] = {
                        key: value for key, value in diagnostic_payload.items()
                        if key not in {"stdout", "stderr"}
                    }
                safe_failure_checks.append(safe_check)
            failure_runtime: dict[str, JsonValue] = {}
            for name in ("node", "npm"):
                if name not in runtime:
                    continue
                value = runtime[name]
                if (not isinstance(value, str)
                    or re.fullmatch(r"v?[0-9][A-Za-z0-9.+-]{0,63}", value) is None):
                    raise ValueError
                failure_runtime[name] = value
            observation = ProjectVerificationFailure({
                "schema_version": 1, "status": "failed", "source_sha256": expected_source,
                "checks": tuple(safe_failure_checks), "runtime": failure_runtime,
            })
            raise ProjectVerificationError(f"project {failure[0]} failed", failure[1],
                                           project_failure=observation)
        if report.get("status") != "passed" or any(
            check["status"] != "passed" for check in checks
        ):
            raise ValueError
        safe_runtime: dict[str, JsonValue] = {}
        for name in ("node", "npm"):
            value = runtime.get(name)
            if not isinstance(value, str) or re.fullmatch(r"v?[0-9][A-Za-z0-9.+-]{0,63}", value) is None:
                raise ValueError
            safe_runtime[name] = value
        return {"schema_version": 1, "status": "passed", "source_sha256": expected_source,
                "checks": tuple(safe_checks), "runtime": safe_runtime}
    except ProjectVerificationError:
        raise
    except Exception:  # noqa: BLE001 - never accept malformed/untrusted success or emit its content.
        raise ProjectVerificationError("project execution report is invalid") from None
