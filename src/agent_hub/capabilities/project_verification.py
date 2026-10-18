"""Task-authorized project verification and source-bound delivery receipts."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Awaitable, Mapping
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID, uuid4

from agent_hub.capabilities.approvals import capability_approval_scope
from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.capabilities.scoped_read import authorized_workspace_scope
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.base import SkillResult

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.project import ProjectInvocation

PROJECT_VERIFY_TOOL = "project.verify"
PROJECT_VERIFY_RESOURCE = "workspace/current/project-verification"
_SHA = re.compile(r"[a-f0-9]{64}")


class ProjectVerificationError(RuntimeError):
    def __init__(self, message: str, diagnostic: ExecutionDiagnostic | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


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
            result = await (self.executor or ProjectSystemdSandbox()).run(invocation)
            if result.exit_code != 0 or result.timed_out:
                raise ProjectVerificationError("project execution outcome is not confirmed")
            receipt = _validate_report(result.stdout, expected_source=before)
            current = await project_source_fingerprint(self.root, invocation)
            if current != before:
                raise ProjectVerificationError("project source changed during verification")
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
            raise ProjectVerificationError(f"project {failure[0]} failed", failure[1])
        if report.get("status") == "blocked":
            raise ProjectVerificationError("project verification blocked by execution prerequisites")
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
