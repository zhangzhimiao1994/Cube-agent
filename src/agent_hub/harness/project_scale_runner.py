from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from agent_hub.harness.project_requirements import (
    validate_large_order_ops_api,
    validate_medium_crm_api,
    validate_small_task_api,
    validate_ultra_portfolio_api,
)
from agent_hub.harness.project_scale import (
    ProjectScaleBenchmarkKind,
    ProjectScaleRunPlan,
    build_project_scale_run_plan,
)

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_QUALITY_KEYS = frozenset(
    {
        "requirements_satisfied",
        "build_passed",
        "tests_passed",
        "interactive_checks_passed",
        "no_placeholders",
        "artifact_integrity",
    }
)
_QUALITY_PAYLOAD_KEYS = (
    "deliverable_quality",
    "project_quality",
    "quality_gate",
    "acceptance",
)
_AGENT_STANDARD_KEYS = frozenset(
    {
        "constraints_read",
        "plan_before_implementation",
        "reproducible_verification",
        "root_cause_repair",
    }
)
_AGENT_STANDARD_PAYLOAD_KEYS = (
    "agent_standard_verification",
    "codex_claude_verification",
    "verification_standard",
)
_DISCUSSION_TRACE_PAYLOAD_KEYS = (
    "discussion_trace",
    "dispatch_discussion_trace",
    "coordination_trace",
)
_PLUGIN_CONTRACT_KEYS = frozenset(
    {
        "manifest_discovered",
        "adapter_contract_checked",
        "policy_boundary_checked",
        "sandbox_profile_checked",
        "failure_recovery_checked",
    }
)
_PLUGIN_CONTRACT_DETAIL_KEYS = (
    ("manifest_ref", "manifest_name", "manifest_path", "manifest"),
    ("adapter_ref", "adapter_name", "adapter_kind", "adapter_contract"),
    ("policy_ref", "policy_boundary", "capability_policy", "policy"),
    ("sandbox_ref", "sandbox_profile", "sandbox_policy", "sandbox"),
    ("recovery_ref", "failure_modes", "recovery_plan", "failure_recovery"),
)
_PLUGIN_CONTRACT_PAYLOAD_KEYS = (
    "plugin_contract",
    "plugin_capability_contract",
    "plugin_validation",
)
_EMBEDDED_WORKSPACE_MAX_FILES = 200
_EMBEDDED_WORKSPACE_MAX_FILE_BYTES = 512_000
_EMBEDDED_WORKSPACE_MAX_TOTAL_BYTES = 2_000_000
_GENERATED_PROJECT_MAX_FILES = 200
_GENERATED_PROJECT_MAX_FILE_BYTES = 2_000_000
_GENERATED_PROJECT_MAX_TOTAL_BYTES = 20_000_000
_DEFAULT_GENERATED_PROJECT_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"),
    ("npm", "run", "build"),
    ("npm", "test"),
)
_GENERATED_PROJECT_OUTPUT_TAIL_CHARS = 2_000
_DISCUSSION_TRACE_FLOWS = frozenset(
    {
        "dispatch",
        "hybrid",
        "multi_agent",
        "plugin",
        "model_failure",
        "self_repair",
        "artifact_production",
        "capability_validation",
    }
)
_AUTHENTICATION_BUSY_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)
_PLACEHOLDER_MARKERS = (
    "lorem ipsum",
    "placeholder project",
    "coming soon",
    "not implemented",
    "mock only",
    "stub only",
    "todo: replace",
    "replace with real implementation",
    "dummy implementation",
    "dummy data only",
    "sample app",
    "hello world",
    "demo only",
)
_VERIFICATION_REPORT_BASENAMES = frozenset(
    {
        "verification.md",
        "verification-report.md",
        "verification_report.md",
        "test-report.md",
        "test_report.md",
        "acceptance-report.md",
        "acceptance_report.md",
        "validation.md",
    }
)
_IMPLEMENTATION_PLAN_BASENAMES = frozenset(
    {
        "implementation-plan.md",
        "implementation_plan.md",
        "project-plan.md",
        "project_plan.md",
        "plan.md",
        "architecture-plan.md",
        "architecture_plan.md",
    }
)
_READING_EVIDENCE_BASENAMES = frozenset(
    {
        "constraints-reading-evidence.json",
        "constraints_reading_evidence.json",
        "context-reading-evidence.json",
        "context_reading_evidence.json",
        "reading-evidence.json",
        "reading_evidence.json",
    }
)
_EXECUTION_PASS_MARKERS = (
    "passed",
    "pass",
    "success",
    "succeeded",
    "ok",
    "0 failed",
)
_EXECUTION_PASS_RE = re.compile(r"(?:\b(?:passed|pass|success|succeeded|ok)\b|0 failed)")


class AcceptanceClient(Protocol):
    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]: ...

    def request_bytes(self, method: str, path: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ProjectScaleCaseResult:
    case_id: str
    run_id: str | None
    status: str | None
    evidence: dict[str, bool]
    validation_focus: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def required_evidence(self) -> tuple[str, ...]:
        required: tuple[str, ...] = (
            "run_details",
            "run_events",
            "terminal_status",
            "final_artifacts",
            "deliverable_quality",
            "agent_standard_verification",
            "project_preflight_approval",
            "workspace_bundle",
            "cleanup_cancel",
        )
        if not _case_requires_project_preflight(self.case_id):
            required = tuple(key for key in required if key != "project_preflight_approval")
        if _case_requires_discussion_trace(self.case_id):
            required = (*required, "discussion_trace")
        if _case_requires_plugin_contract(self.case_id):
            required = (*required, "plugin_contract")
        if "self_repair" in self.case_id or "model_failure" in self.case_id:
            required = (*required, "self_repair_trace")
        if "generated_project_validation" in self.evidence:
            required = (*required, "generated_project_validation")
        if "requirements_validation" in self.evidence:
            required = (*required, "requirements_validation")
        return required

    @property
    def missing_evidence(self) -> tuple[str, ...]:
        return tuple(key for key in self.required_evidence if self.evidence.get(key) is not True)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.missing_evidence

    @property
    def repair_attempted(self) -> bool:
        return (
            self.evidence.get("deliverable_repair_trace") is True
            or self.evidence.get("self_repair_trace") is True
        )

    @property
    def repair_outcome(self) -> str:
        if not self.repair_attempted:
            return "not_attempted"
        if not self.missing_evidence and not self.errors:
            return "passed"
        return "failed"

    def to_payload(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "status": self.status,
            "ok": self.ok,
            "repair_attempted": self.repair_attempted,
            "repair_outcome": self.repair_outcome,
            "validation_focus": list(self.validation_focus),
            "required_evidence": list(self.required_evidence),
            "missing_evidence": list(self.missing_evidence),
            "evidence": dict(self.evidence),
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class ProjectScaleExecutionReport:
    results: tuple[ProjectScaleCaseResult, ...]
    benchmark_kind: str = "fixture"

    @property
    def case_count(self) -> int:
        return len(self.results)

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def failed_results(self) -> tuple[ProjectScaleCaseResult, ...]:
        return tuple(result for result in self.results if not result.ok)

    def to_payload(self) -> dict[str, object]:
        failed_results = self.failed_results
        capability_verified = (
            self.benchmark_kind == "capability" and self.case_count > 0 and self.ok
        )
        return {
            "execute": True,
            "dry_run": False,
            "benchmark_kind": self.benchmark_kind,
            "capability_verified": capability_verified,
            "verification_scope": (
                "synthetic fixture regression; not real project capability or recovery proof"
                if self.benchmark_kind == "fixture"
                else "actual build/test, per-case independent business checks, and "
                "runtime process evidence verified"
                if capability_verified
                else "actual build/test and per-case independent business checks; "
                "runtime process evidence unverified"
            ),
            "ok": self.ok,
            "case_count": self.case_count,
            "failed_case_count": len(failed_results),
            "failed_cases": [result.case_id for result in failed_results],
            "missing_evidence_summary": _summarize_missing_evidence(failed_results),
            "failed_validation_focus": _summarize_validation_focus(failed_results),
            "results": [result.to_payload() for result in self.results],
        }


class UrllibAcceptanceClient:
    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str = "",
        timeout: float = 20.0,
        username: str | None = None,
        password: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._bearer_token = bearer_token
        self._timeout = timeout
        self._username = username
        self._password = password
        self._tenant_id = tenant_id

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | list[object]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._bearer_token}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        raw = self._request(method, path, headers=headers, data=data)
        decoded = raw.decode("utf-8")
        parsed = json.loads(decoded)
        if not isinstance(parsed, dict | list):
            raise TypeError(f"{method} {path} returned non-object JSON")
        return parsed

    def request_bytes(self, method: str, path: str) -> bytes:
        headers = {"Authorization": f"Bearer {self._bearer_token}"}
        return self._request(method, path, headers=headers, data=None)

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        data: bytes | None,
        retry_auth: bool = True,
        include_bearer: bool = True,
    ) -> bytes:
        if retry_auth and include_bearer and not self._bearer_token and self._can_login():
            self._refresh_bearer_token()
        if include_bearer and self._bearer_token:
            headers = dict(headers)
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        url = urljoin(self._base_url, path.lstrip("/"))
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return cast(bytes, response.read())
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            if retry_auth and error.code == 401 and self._can_login() and _is_invalid_token_body(body):
                self._refresh_bearer_token()
                return self._request(
                    method,
                    path,
                    headers=headers,
                    data=data,
                    retry_auth=False,
                    include_bearer=include_bearer,
                )
            raise RuntimeError(f"{method} {path} failed status={error.code} body={body[:240]}") from error
        except URLError as error:
            raise RuntimeError(f"{method} {path} failed: {error.reason}") from error

    def _can_login(self) -> bool:
        return bool(self._username and self._password)

    def _refresh_bearer_token(self) -> None:
        if not self._can_login():
            raise RuntimeError("acceptance login credentials are unavailable")
        body: dict[str, object] = {
            "username": self._username,
            "password": self._password,
        }
        if self._tenant_id:
            body["tenant_id"] = self._tenant_id
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        raw = self._request_acceptance_login(data)
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict) or not isinstance(parsed.get("access_token"), str):
            raise TypeError("acceptance login returned invalid token response")
        self._bearer_token = cast(str, parsed["access_token"])

    def _request_acceptance_login(self, data: bytes) -> bytes:
        for attempt, delay in enumerate((0.0, *_AUTHENTICATION_BUSY_RETRY_DELAYS_SECONDS)):
            if delay > 0:
                time.sleep(delay)
            try:
                return self._request(
                    "POST",
                    "/api/v1/auth/login",
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    data=data,
                    retry_auth=False,
                    include_bearer=False,
                )
            except RuntimeError as error:
                if (
                    attempt >= len(_AUTHENTICATION_BUSY_RETRY_DELAYS_SECONDS)
                    or not _is_authentication_busy_error_message(str(error))
                ):
                    raise
        raise RuntimeError("acceptance login failed after authentication busy retries")


@dataclass(frozen=True, slots=True)
class _RunObservation:
    status: str | None
    details: dict[str, object] | None
    events: list[object] | None
    workspace_bundle: bytes | None


@dataclass(frozen=True, slots=True)
class _EvidenceCheck:
    passed: bool
    reasons: tuple[str, ...]


def execute_project_scale_plan(
    plan: ProjectScaleRunPlan,
    client: AcceptanceClient,
    *,
    wait_seconds: float = 0,
    poll_interval_seconds: float = 2,
    execution_id: str | None = None,
    validate_generated_project: bool = False,
    generated_project_commands: Sequence[Sequence[str]] | None = None,
    generated_project_timeout_seconds: float = 120,
) -> ProjectScaleExecutionReport:
    validate_generated_project = validate_generated_project or plan.benchmark_kind == "capability"
    results: list[ProjectScaleCaseResult] = []
    for index, run_request in enumerate(plan.requests):
        request_body = _scoped_execution_body(run_request.body, execution_id=execution_id)
        evidence = {
            "run_details": False,
            "run_events": False,
            "terminal_status": False,
            "final_artifacts": False,
            "deliverable_quality": False,
            "agent_standard_verification": False,
            "discussion_trace": False,
            "plugin_contract": False,
            "deliverable_repair_trace": False,
            "self_repair_trace": False,
            "project_preflight_approval": False,
            "workspace_bundle": False,
            "cleanup_cancel": False,
        }
        if validate_generated_project:
            evidence["generated_project_validation"] = False
        if plan.benchmark_kind == "capability":
            evidence["requirements_validation"] = False
        errors: list[str] = []
        run_id: str | None = None
        status: str | None = None
        try:
            response = client.request_json(
                "POST",
                "/api/v1/runs",
                body=request_body,
                idempotency_key=_idempotency_key(
                    run_request.case_id,
                    index,
                    execution_id=execution_id,
                ),
            )
            if not isinstance(response, dict):
                raise TypeError("run create returned non-object JSON")
            raw_run_id = response.get("id")
            if not isinstance(raw_run_id, str) or not raw_run_id:
                raise RuntimeError("run create response missing id")
            run_id = raw_run_id
            status = _string_value(response.get("status"))
            _validate_run_submission_scope(response, request_body)
            _extend_unique(
                errors,
                _validate_mode_control(
                    response,
                    requested_body=request_body,
                    validation_focus=run_request.validation_focus,
                ),
            )
            if _case_requires_project_preflight(run_request.case_id):
                approval_body = _project_preflight_approval_body(response)
                approval = client.request_json(
                    "POST",
                    f"/api/v1/runs/{quote(run_id)}/approve-project-preflight",
                    body=approval_body,
                )
                if not isinstance(approval, dict):
                    raise TypeError("project preflight approval returned non-object JSON")
                evidence["project_preflight_approval"] = True
                status = _string_value(approval.get("status")) or status

            observation = _collect_run_observation(
                client,
                run_id=run_id,
                body=request_body,
                wait_seconds=wait_seconds,
                poll_interval_seconds=poll_interval_seconds,
                current_status=status,
                evidence=evidence,
                errors=errors,
            )
            status = observation.status
            initial_self_repair_trace = _has_self_repair_trace(observation.events)
            _extend_unique(
                errors,
                _validate_mode_control(
                    observation.details,
                    requested_body=request_body,
                    validation_focus=run_request.validation_focus,
                ),
            )
            if evidence["terminal_status"] and status != "completed" and observation.details:
                repair_body = _self_repair_acceptance_body(
                    client,
                    run_id=run_id,
                    details=observation.details,
                )
                if repair_body is not None:
                    repair_response = client.request_json(
                        "POST",
                        f"/api/v1/runs/{quote(run_id)}/accept-repair",
                        body=repair_body,
                    )
                    if not isinstance(repair_response, dict):
                        raise TypeError("self repair acceptance returned non-object JSON")
                    _validate_run_submission_scope(repair_response, request_body)
                    _extend_unique(
                        errors,
                        _validate_mode_control(
                            repair_response,
                            requested_body=request_body,
                            validation_focus=run_request.validation_focus,
                        ),
                    )
                    repair_run_id = repair_response.get("id")
                    if not isinstance(repair_run_id, str) or not repair_run_id:
                        raise RuntimeError("self repair acceptance response missing id")
                    run_id = repair_run_id
                    status = _string_value(repair_response.get("status")) or status
                    evidence["deliverable_repair_trace"] = True
                    self_repair_observation = _collect_run_observation(
                        client,
                        run_id=run_id,
                        body=request_body,
                        wait_seconds=wait_seconds,
                        poll_interval_seconds=poll_interval_seconds,
                        current_status=status,
                        evidence=evidence,
                        errors=errors,
                    )
                    observation = self_repair_observation
                    status = self_repair_observation.status
                    _extend_unique(
                        errors,
                        _validate_mode_control(
                            self_repair_observation.details,
                            requested_body=request_body,
                            validation_focus=run_request.validation_focus,
                        ),
                    )
            evidence["self_repair_trace"] = initial_self_repair_trace or _has_self_repair_trace(
                observation.events
            )
            deliverable_quality = _evaluate_deliverable_quality(
                observation.details,
                observation.events,
                observation.workspace_bundle,
            )
            evidence["deliverable_quality"] = deliverable_quality.passed
            agent_standard_verification = _evaluate_agent_standard_verification(
                observation.details,
                observation.events,
                observation.workspace_bundle,
                benchmark_kind=plan.benchmark_kind,
            )
            evidence["agent_standard_verification"] = agent_standard_verification.passed
            discussion_trace = _evaluate_discussion_trace(
                observation.details,
                observation.events,
                case_id=run_request.case_id,
            )
            evidence["discussion_trace"] = discussion_trace.passed
            plugin_contract = _evaluate_plugin_contract(
                observation.details,
                observation.events,
                case_id=run_request.case_id,
            )
            evidence["plugin_contract"] = plugin_contract.passed
            generated_project_validation = _EvidenceCheck(passed=True, reasons=())
            if validate_generated_project:
                generated_project_validation = _validate_generated_project_bundle(
                    observation.workspace_bundle,
                    commands=generated_project_commands or _DEFAULT_GENERATED_PROJECT_COMMANDS,
                    timeout_seconds=generated_project_timeout_seconds,
                    requirements_case_id=(
                        run_request.case_id if plan.benchmark_kind == "capability" else None
                    ),
                )
                evidence["generated_project_validation"] = generated_project_validation.passed
                if plan.benchmark_kind == "capability":
                    evidence["requirements_validation"] = generated_project_validation.passed
                    deliverable_quality = _executed_capability_quality(
                        observation.workspace_bundle, generated_project_validation
                    )
                    evidence["deliverable_quality"] = deliverable_quality.passed
            if _should_attempt_deliverable_repair(
                status=status,
                evidence=evidence,
                case_id=run_request.case_id,
                benchmark_kind=plan.benchmark_kind,
            ):
                repair_response = client.request_json(
                    "POST",
                    "/api/v1/runs",
                    body=_deliverable_repair_body(
                        request_body,
                        run_request.case_id,
                        benchmark_kind=plan.benchmark_kind,
                        failed_reasons=(
                            *deliverable_quality.reasons,
                            *agent_standard_verification.reasons,
                            *discussion_trace.reasons,
                            *plugin_contract.reasons,
                            *generated_project_validation.reasons,
                        ),
                    ),
                    idempotency_key=_deliverable_repair_idempotency_key(
                        run_request.case_id,
                        index,
                        execution_id=execution_id,
                    ),
                )
                if not isinstance(repair_response, dict):
                    raise TypeError("deliverable repair returned non-object JSON")
                _validate_run_submission_scope(repair_response, request_body)
                _extend_unique(
                    errors,
                    _validate_mode_control(
                        repair_response,
                        requested_body=request_body,
                        validation_focus=run_request.validation_focus,
                    ),
                )
                repair_run_id = repair_response.get("id")
                if not isinstance(repair_run_id, str) or not repair_run_id:
                    raise RuntimeError("deliverable repair response missing id")
                evidence["deliverable_repair_trace"] = True
                run_id = repair_run_id
                status = _string_value(repair_response.get("status")) or status
                repair_observation = _collect_run_observation(
                    client,
                    run_id=run_id,
                    body=request_body,
                    wait_seconds=wait_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                    current_status=status,
                    evidence=evidence,
                    errors=errors,
                )
                status = repair_observation.status
                _extend_unique(
                    errors,
                    _validate_mode_control(
                        repair_observation.details,
                        requested_body=request_body,
                        validation_focus=run_request.validation_focus,
                    ),
                )
                evidence["self_repair_trace"] = (
                    evidence["self_repair_trace"]
                    or _has_self_repair_trace(repair_observation.events)
                    or _has_deliverable_repair_trace(repair_observation.events)
                )
                deliverable_quality = _evaluate_deliverable_quality(
                    repair_observation.details,
                    repair_observation.events,
                    repair_observation.workspace_bundle,
                )
                evidence["deliverable_quality"] = deliverable_quality.passed
                agent_standard_verification = _evaluate_agent_standard_verification(
                    repair_observation.details,
                    repair_observation.events,
                    repair_observation.workspace_bundle,
                    benchmark_kind=plan.benchmark_kind,
                )
                evidence["agent_standard_verification"] = agent_standard_verification.passed
                discussion_trace = _evaluate_discussion_trace(
                    repair_observation.details,
                    repair_observation.events,
                    case_id=run_request.case_id,
                )
                evidence["discussion_trace"] = discussion_trace.passed
                plugin_contract = _evaluate_plugin_contract(
                    repair_observation.details,
                    repair_observation.events,
                    case_id=run_request.case_id,
                )
                evidence["plugin_contract"] = plugin_contract.passed
                if validate_generated_project:
                    generated_project_validation = _validate_generated_project_bundle(
                        repair_observation.workspace_bundle,
                        commands=generated_project_commands or _DEFAULT_GENERATED_PROJECT_COMMANDS,
                        timeout_seconds=generated_project_timeout_seconds,
                        requirements_case_id=(
                            run_request.case_id if plan.benchmark_kind == "capability" else None
                        ),
                    )
                    evidence["generated_project_validation"] = (
                        generated_project_validation.passed
                    )
                    if plan.benchmark_kind == "capability":
                        evidence["requirements_validation"] = generated_project_validation.passed
                        deliverable_quality = _executed_capability_quality(
                            repair_observation.workspace_bundle, generated_project_validation
                        )
                        evidence["deliverable_quality"] = deliverable_quality.passed
                if evidence["workspace_bundle"]:
                    _drop_recovered_workspace_bundle_errors(errors)
            if (
                evidence["workspace_bundle"]
                and evidence["final_artifacts"]
                and (
                    not evidence["deliverable_quality"]
                    or not evidence["agent_standard_verification"]
                    or (
                        _case_requires_discussion_trace(run_request.case_id)
                        and not evidence["discussion_trace"]
                    )
                    or (
                        _case_requires_plugin_contract(run_request.case_id)
                        and not evidence["plugin_contract"]
                    )
                    or (
                        validate_generated_project
                        and not evidence["generated_project_validation"]
                    )
                )
            ):
                if not evidence["deliverable_quality"]:
                    errors.extend(deliverable_quality.reasons)
                if not evidence["agent_standard_verification"]:
                    errors.extend(agent_standard_verification.reasons)
                if (
                    _case_requires_discussion_trace(run_request.case_id)
                    and not evidence["discussion_trace"]
                ):
                    errors.extend(discussion_trace.reasons)
                if (
                    _case_requires_plugin_contract(run_request.case_id)
                    and not evidence["plugin_contract"]
                ):
                    errors.extend(plugin_contract.reasons)
                if validate_generated_project and not evidence["generated_project_validation"]:
                    errors.extend(generated_project_validation.reasons)
            if evidence["terminal_status"] and status != "completed":
                errors.append(f"terminal_status: {status or 'unknown'}")
        except Exception as error:  # noqa: BLE001 - collect per-case failures and continue.
            errors.append(str(error))
        finally:
            if run_id is not None:
                if _is_terminal_status(status):
                    evidence["cleanup_cancel"] = True
                else:
                    try:
                        cleanup = client.request_json("POST", f"/api/v1/runs/{quote(run_id)}/cancel")
                        evidence["cleanup_cancel"] = isinstance(cleanup, dict)
                    except Exception as error:  # noqa: BLE001 - cleanup failure is evidence.
                        errors.append(f"cleanup_cancel: {error}")
        results.append(
            ProjectScaleCaseResult(
                case_id=run_request.case_id,
                run_id=run_id,
                status=status,
                evidence=evidence,
                validation_focus=run_request.validation_focus,
                errors=tuple(errors),
            )
        )
    return ProjectScaleExecutionReport(results=tuple(results), benchmark_kind=plan.benchmark_kind)


def _acceptance_credentials_from_env() -> tuple[str | None, str | None, str | None]:
    username = os.environ.get("AGENT_HUB_ACCEPTANCE_USERNAME") or os.environ.get(
        "AGENT_HUB_ACCEPTANCE_LOGIN_USERNAME"
    )
    password = os.environ.get("AGENT_HUB_ACCEPTANCE_PASSWORD") or os.environ.get(
        "AGENT_HUB_ACCEPTANCE_LOGIN_PASSWORD"
    )
    tenant_id = os.environ.get("AGENT_HUB_ACCEPTANCE_TENANT_ID") or os.environ.get(
        "AGENT_HUB_ACCEPTANCE_LOGIN_TENANT_ID"
    )
    return username, password, tenant_id


def _env_flag(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agent_hub.harness.project_scale_runner",
        description="Build or execute fixture regression or real project capability probes.",
    )
    parser.add_argument("--scale", action="append", dest="scales", default=None)
    parser.add_argument("--flow", action="append", dest="flows", default=None)
    parser.add_argument(
        "--benchmark-kind",
        choices=("fixture", "capability"),
        default=os.environ.get("AGENT_HUB_PROJECT_SCALE_BENCHMARK_KIND", "fixture"),
        help="Fixture uses synthetic evidence; capability uses real requirements and build checks.",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AGENT_HUB_ACCEPTANCE_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("AGENT_HUB_ACCEPTANCE_MAX_TIME_SECONDS", "20")),
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=float(os.environ.get("AGENT_HUB_PROJECT_SCALE_WAIT_SECONDS", "0")),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get("AGENT_HUB_PROJECT_SCALE_POLL_INTERVAL_SECONDS", "2")),
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("AGENT_HUB_PROJECT_SCALE_REPORT_PATH"),
        help="Write the JSON plan or execution report to this path.",
    )
    parser.add_argument(
        "--execution-id",
        default=os.environ.get("AGENT_HUB_PROJECT_SCALE_EXECUTION_ID"),
        help="Scope execution idempotency keys; generated automatically for --execute.",
    )
    parser.add_argument(
        "--verify-artifact-build",
        "--validate-generated-project",
        action="store_true",
        dest="validate_generated_project",
        default=_env_flag("AGENT_HUB_PROJECT_SCALE_VERIFY_ARTIFACT_BUILD")
        or _env_flag("AGENT_HUB_PROJECT_SCALE_VALIDATE_GENERATED_PROJECT"),
        help=(
            "After each executed case, download the generated workspace/artifact ZIP, "
            "extract it safely, and run real generated-project validation commands."
        ),
    )
    parser.add_argument(
        "--artifact-build-timeout",
        type=float,
        default=float(
            os.environ.get("AGENT_HUB_PROJECT_SCALE_ARTIFACT_BUILD_TIMEOUT_SECONDS", "120")
        ),
        help="Timeout in seconds for each generated-project validation command.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)

    bearer_token = os.environ.get("AGENT_HUB_ACCEPTANCE_BEARER_TOKEN", "")
    username, password, tenant_id = _acceptance_credentials_from_env()
    if args.execute and not bearer_token and not (username and password):
        parser.error(
            "AGENT_HUB_ACCEPTANCE_BEARER_TOKEN or "
            "AGENT_HUB_ACCEPTANCE_USERNAME/PASSWORD is required for --execute "
            "(AGENT_HUB_ACCEPTANCE_LOGIN_USERNAME/PASSWORD is also accepted)"
        )

    try:
        plan = build_project_scale_run_plan(
            scales=tuple(args.scales) if args.scales is not None else None,
            flows=tuple(args.flows) if args.flows is not None else None,
            execute=args.execute,
            benchmark_kind=args.benchmark_kind,
        )
    except ValueError as error:
        parser.error(str(error))

    if args.execute:
        report = execute_project_scale_plan(
            plan,
            UrllibAcceptanceClient(
                base_url=args.base_url,
                bearer_token=bearer_token,
                timeout=args.timeout,
                username=username,
                password=password,
                tenant_id=tenant_id,
            ),
            wait_seconds=args.wait_seconds,
            poll_interval_seconds=args.poll_interval,
            execution_id=args.execution_id or _default_execution_id(),
            validate_generated_project=args.validate_generated_project,
            generated_project_timeout_seconds=args.artifact_build_timeout,
        )
        payload = report.to_payload()
    else:
        payload = plan.to_payload()
    if args.output:
        Path(args.output).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        capability_verified = str(bool(payload.get("capability_verified"))).lower()
        print(f"benchmark_kind={plan.benchmark_kind} capability_verified={capability_verified}")
        if args.execute:
            print(f"project-scale execution cases={report.case_count} ok={str(report.ok).lower()}")
            for result in report.results:
                print(format_project_scale_result_line(result))
        else:
            print(
                f"project-scale plan cases={plan.case_count} "
                f"dry_run={str(plan.dry_run).lower()} execute={str(plan.execute).lower()}"
            )
            for request in plan.requests:
                print(f"{request.case_id} focus={','.join(request.validation_focus)}")
    return 0 if (not args.execute or report.ok) else 1


def format_project_scale_result_line(result: ProjectScaleCaseResult) -> str:
    parts = [
        result.case_id,
        f"run_id={result.run_id or '-'}",
        f"ok={str(result.ok).lower()}",
    ]
    if result.validation_focus:
        parts.append(f"focus={','.join(result.validation_focus)}")
    if result.missing_evidence:
        parts.append(f"missing={','.join(result.missing_evidence)}")
    if result.errors:
        parts.append(f"errors={len(result.errors)}")
    if result.repair_attempted:
        parts.append(f"repair={result.repair_outcome}")
    return " ".join(parts)


def _summarize_missing_evidence(results: Sequence[ProjectScaleCaseResult]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for result in results:
        for evidence in result.missing_evidence:
            summary[evidence] = summary.get(evidence, 0) + 1
    return summary


def _summarize_validation_focus(results: Sequence[ProjectScaleCaseResult]) -> list[str]:
    focus: dict[str, None] = {}
    for result in results:
        for item in result.validation_focus:
            focus.setdefault(item, None)
    return list(focus)


def _is_invalid_token_body(body: str) -> bool:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, Mapping):
        return False
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return False
    return error.get("code") == "invalid_token"


def _is_authentication_busy_error_message(message: str) -> bool:
    if "status=429" not in message:
        return False
    body_marker = " body="
    body_index = message.find(body_marker)
    if body_index < 0:
        return False
    try:
        payload = json.loads(message[body_index + len(body_marker) :])
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, Mapping):
        return False
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return False
    return error.get("code") == "authentication_busy"


def _extend_unique(target: list[str], items: Sequence[str]) -> None:
    for item in items:
        if item not in target:
            target.append(item)


def _drop_recovered_workspace_bundle_errors(errors: list[str]) -> None:
    errors[:] = [
        error
        for error in errors
        if not error.startswith(
            ("workspace_bundle: workspace bundle unavailable", "workspace_bundle: GET ")
        )
    ]


def _workspace_bundle_path(body: dict[str, object]) -> str:
    project_id = body.get("project_id")
    session_id = body.get("workspace_session_id")
    if not isinstance(project_id, str) or not project_id:
        raise RuntimeError("run request missing project_id")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("run request missing workspace_session_id")
    return (
        f"/api/v1/workspaces/projects/{quote(project_id, safe='')}/"
        f"sessions/{quote(session_id, safe='')}/bundle/download"
    )


def _artifact_download_paths(
    *,
    run_id: str,
    details: Mapping[str, object] | None,
    events: Sequence[object] | None,
) -> tuple[str, ...]:
    paths: list[str] = []
    encoded_run_id = quote(run_id)

    def append_path(path: str) -> None:
        if path in paths:
            return
        if path.startswith(f"/api/v1/runs/{encoded_run_id}/artifacts/") and path.endswith(
            "/download"
        ):
            paths.append(path)
            return
        if path.startswith(f"/api/v1/admin/runs/{encoded_run_id}/artifacts/") and path.endswith(
            "/download"
        ):
            paths.append(path)

    def append_artifact_id(value: object) -> None:
        artifact_id = _string_value(value)
        if artifact_id:
            append_path(
                f"/api/v1/runs/{encoded_run_id}/artifacts/{quote(artifact_id)}/download"
            )

    def visit_mapping(mapping: Mapping[str, object], *, artifact_like: bool = False) -> None:
        if artifact_like:
            append_artifact_id(mapping.get("id"))
        append_artifact_id(mapping.get("artifact_id"))
        download_url = mapping.get("download_url")
        if isinstance(download_url, str):
            append_path(download_url)

        artifact = mapping.get("artifact")
        if isinstance(artifact, Mapping):
            visit_mapping(artifact, artifact_like=True)

        payload = mapping.get("payload")
        if isinstance(payload, Mapping):
            append_artifact_id(payload.get("artifact_id"))
            payload_download_url = payload.get("download_url")
            if isinstance(payload_download_url, str):
                append_path(payload_download_url)

        artifacts = mapping.get("artifacts")
        if isinstance(artifacts, Sequence) and not isinstance(artifacts, str | bytes):
            for artifact_item in artifacts:
                if isinstance(artifact_item, Mapping):
                    visit_mapping(artifact_item, artifact_like=True)

        artifact_ids = mapping.get("artifact_ids")
        if isinstance(artifact_ids, Sequence) and not isinstance(artifact_ids, str | bytes):
            for artifact_id in artifact_ids:
                append_artifact_id(artifact_id)

    if details is not None:
        visit_mapping(details)
    if events is not None:
        for event in events:
            if isinstance(event, Mapping):
                visit_mapping(event)
    return tuple(paths)


def _downloaded_workspace_bundle_from_artifacts(
    client: AcceptanceClient,
    *,
    run_id: str,
    details: Mapping[str, object] | None,
    events: Sequence[object] | None,
) -> bytes | None:
    for path in _artifact_download_paths(run_id=run_id, details=details, events=events):
        try:
            raw = client.request_bytes("GET", path)
        except Exception:  # noqa: BLE001 - try remaining artifacts before failing the bundle.
            raw = None
        if raw is None:
            continue
        bundle = _workspace_bundle_from_downloaded_artifact(raw)
        if bundle is not None:
            return bundle
    return None


def _workspace_bundle_from_downloaded_artifact(raw: bytes) -> bytes | None:
    if zipfile.is_zipfile(BytesIO(raw)):
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return _embedded_workspace_bundle_from_text(text)


def _validate_generated_project_bundle(
    workspace_bundle: bytes | None,
    *,
    commands: Sequence[Sequence[str]],
    timeout_seconds: float,
    requirements_case_id: str | None = None,
) -> _EvidenceCheck:
    if workspace_bundle is None:
        return _EvidenceCheck(
            passed=False,
            reasons=("generated_project_validation: missing workspace bundle",),
        )
    if not commands:
        return _EvidenceCheck(
            passed=False,
            reasons=("generated_project_validation: no validation commands configured",),
        )
    try:
        with tempfile.TemporaryDirectory(prefix="agent-hub-project-scale-") as temp_dir:
            root = Path(temp_dir)
            _extract_workspace_bundle_safely(workspace_bundle, root)
            for command in commands:
                reason = _run_generated_project_command(
                    command,
                    cwd=root,
                    timeout_seconds=timeout_seconds,
                )
                if reason is not None:
                    return _EvidenceCheck(passed=False, reasons=(reason,))
            if requirements_case_id is not None:
                scale = requirements_case_id.split(":", 1)[0]
                validators = {
                    "large": validate_large_order_ops_api,
                    "small": validate_small_task_api,
                    "medium": validate_medium_crm_api,
                    "ultra": validate_ultra_portfolio_api,
                }
                validator = validators.get(scale)
                if validator is None:
                    return _EvidenceCheck(
                        passed=False,
                        reasons=("requirements: independent evaluator unavailable for this scale",),
                    )
                failures = validator(root, timeout_seconds=timeout_seconds)
                if failures:
                    return _EvidenceCheck(passed=False, reasons=failures)
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        return _EvidenceCheck(
            passed=False,
            reasons=(f"generated_project_validation: {error}",),
        )
    return _EvidenceCheck(passed=True, reasons=())


def _extract_workspace_bundle_safely(workspace_bundle: bytes, root: Path) -> None:
    total_size = 0
    with zipfile.ZipFile(BytesIO(workspace_bundle)) as archive:
        infos = archive.infolist()
        if len(infos) > _GENERATED_PROJECT_MAX_FILES:
            raise RuntimeError("workspace bundle contains too many files")
        for info in infos:
            if info.is_dir():
                continue
            relative_path = _safe_zip_member_path(info.filename)
            if info.file_size > _GENERATED_PROJECT_MAX_FILE_BYTES:
                raise RuntimeError(f"workspace bundle file too large: {info.filename}")
            total_size += info.file_size
            if total_size > _GENERATED_PROJECT_MAX_TOTAL_BYTES:
                raise RuntimeError("workspace bundle total size is too large")
            target = root / Path(*relative_path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as destination:
                destination.write(source.read())


def _safe_zip_member_path(value: str) -> PurePosixPath:
    if "\\" in value:
        raise RuntimeError(f"workspace bundle has unsafe path: {value}")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise RuntimeError(f"workspace bundle has unsafe path: {value}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError(f"workspace bundle has unsafe path: {value}")
    if ":" in path.parts[0]:
        raise RuntimeError(f"workspace bundle has unsafe path: {value}")
    return path


def _run_generated_project_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
) -> str | None:
    if not command or any(not isinstance(part, str) or not part for part in command):
        return "generated_project_validation: invalid validation command"
    safe_env = _generated_project_command_env()
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=safe_env,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(timeout_seconds, 1),
        )
    except subprocess.TimeoutExpired:
        return (
            "generated_project_validation: command timed out "
            f"timeout={max(timeout_seconds, 1):g}s command={_format_command(command)}"
        )
    except OSError as error:
        return (
            "generated_project_validation: command could not start "
            f"command={_format_command(command)} reason={error}"
        )
    if completed.returncode != 0:
        output_tail = _generated_project_output_tail(
            f"{completed.stdout or ''}\n{completed.stderr or ''}"
        )
        output_note = f" output_tail={output_tail}" if output_tail else ""
        return (
            "generated_project_validation: command failed "
            f"exit={completed.returncode} command={_format_command(command)}{output_note}"
        )
    return None


def _generated_project_output_tail(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return ""
    if len(text) > _GENERATED_PROJECT_OUTPUT_TAIL_CHARS:
        text = "..." + text[-_GENERATED_PROJECT_OUTPUT_TAIL_CHARS:]
    return json.dumps(text, ensure_ascii=False)


def _generated_project_command_env() -> dict[str, str]:
    keep_keys = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "COMSPEC",
        "HOME",
        "USERPROFILE",
        "TEMP",
        "TMP",
        "NPM_CONFIG_REGISTRY",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in keep_keys}


def _format_command(command: Sequence[str]) -> str:
    return " ".join(command)


def _collect_run_observation(
    client: AcceptanceClient,
    *,
    run_id: str,
    body: dict[str, object],
    wait_seconds: float,
    poll_interval_seconds: float,
    current_status: str | None,
    evidence: dict[str, bool],
    errors: list[str],
) -> _RunObservation:
    status = current_status
    details: dict[str, object] | None = None
    events: list[object] | None = None
    workspace_bundle: bytes | None = None
    approved_capabilities: set[str] = set()

    deadline = time.monotonic() + max(wait_seconds, 0)
    while True:
        details_response = client.request_json("GET", f"/api/v1/runs/{quote(run_id)}/details")
        evidence["run_details"] = isinstance(details_response, dict)
        if isinstance(details_response, dict):
            details = details_response
            _validate_run_details_scope(details, run_id)
            status = _string_value(details.get("status")) or status
            evidence["final_artifacts"] = _has_final_artifacts(details)
            approved_status = _approve_pending_capability(
                client,
                run_id=run_id,
                details=details,
                approved_capabilities=approved_capabilities,
                errors=errors,
            )
            if approved_status is not None:
                status = approved_status
        if _is_terminal_status(status):
            evidence["terminal_status"] = True
            break
        if wait_seconds <= 0 or time.monotonic() >= deadline:
            break
        if poll_interval_seconds > 0:
            time.sleep(poll_interval_seconds)

    events_response = client.request_json("GET", f"/api/v1/runs/{quote(run_id)}/events")
    normalized_events = _run_events_items(events_response)
    if isinstance(normalized_events, list) and normalized_events:
        events = normalized_events
        evidence["run_events"] = True
        errors.extend(_validate_run_events_scope(normalized_events, run_id))
    elif isinstance(normalized_events, list):
        events = normalized_events
        errors.append("run_events: empty event stream")
    else:
        errors.append("run_events: returned non-list JSON")

    try:
        workspace_bundle = client.request_bytes("GET", _workspace_bundle_path(body))
        evidence["workspace_bundle"] = True
    except Exception as error:  # noqa: BLE001 - acceptance reports must continue cleanup.
        workspace_bundle = _embedded_workspace_bundle_from_observation(details, events)
        downloaded_bundle = _downloaded_workspace_bundle_from_artifacts(
            client,
            run_id=run_id,
            details=details,
            events=events,
        )
        if downloaded_bundle is not None:
            workspace_bundle = downloaded_bundle
        if workspace_bundle is None:
            admin_details = _admin_run_details(client, run_id=run_id)
            if admin_details is not None:
                workspace_bundle = _embedded_workspace_bundle_from_observation(admin_details, events)
                if workspace_bundle is None:
                    downloaded_bundle = _downloaded_workspace_bundle_from_artifacts(
                        client,
                        run_id=run_id,
                        details=admin_details,
                        events=events,
                    )
                    if downloaded_bundle is not None:
                        workspace_bundle = downloaded_bundle
        if workspace_bundle is not None:
            evidence["workspace_bundle"] = True
        else:
            errors.append(f"workspace_bundle: {error}")

    return _RunObservation(
        status=status,
        details=details,
        events=events,
        workspace_bundle=workspace_bundle,
    )


def _validate_run_submission_scope(response: dict[str, object], body: dict[str, object]) -> None:
    for field in ("project_id", "workspace_session_id"):
        expected = body.get(field)
        actual = response.get(field)
        if not isinstance(expected, str) or not expected:
            raise RuntimeError(f"run request missing {field}")
        if actual != expected:
            got = actual if isinstance(actual, str) and actual else "missing"
            raise RuntimeError(f"run scope mismatch: {field} expected {expected} got {got}")


def _run_events_items(response: dict[str, object] | list[object]) -> list[object] | None:
    if isinstance(response, list):
        return response
    items = response.get("items") if isinstance(response, dict) else None
    return items if isinstance(items, list) else None


def _admin_run_details(
    client: AcceptanceClient,
    *,
    run_id: str,
) -> dict[str, object] | None:
    try:
        response = client.request_json("GET", f"/api/v1/admin/runs/{quote(run_id)}")
    except Exception:  # noqa: BLE001 - admin detail is a best-effort recovery path.
        return None
    if not isinstance(response, dict):
        return None
    if response.get("id") != run_id:
        return None
    return response


def _embedded_workspace_bundle_from_observation(
    details: dict[str, object] | None,
    events: list[object] | None,
) -> bytes | None:
    candidates: list[Mapping[str, object]] = []
    if details is not None:
        candidates.append(details)
    if events is not None:
        candidates.extend(event for event in events if isinstance(event, Mapping))
    for candidate in candidates:
        bundle = _embedded_workspace_bundle_from_mapping(candidate)
        if bundle is not None:
            return bundle
    return None


def _embedded_workspace_bundle_from_mapping(mapping: Mapping[str, object]) -> bytes | None:
    direct = _embedded_workspace_bundle_from_payload(mapping)
    if direct is not None:
        return direct
    artifact = mapping.get("artifact")
    if isinstance(artifact, Mapping):
        artifact_bundle = _embedded_workspace_bundle_from_payload(artifact)
        if artifact_bundle is not None:
            return artifact_bundle
    payload = mapping.get("payload")
    if isinstance(payload, Mapping):
        payload_bundle = _embedded_workspace_bundle_from_payload(payload)
        if payload_bundle is not None:
            return payload_bundle
    artifacts = mapping.get("artifacts")
    if isinstance(artifacts, Sequence) and not isinstance(artifacts, str | bytes):
        for artifact_item in artifacts:
            if not isinstance(artifact_item, Mapping):
                continue
            artifact_bundle = _embedded_workspace_bundle_from_payload(artifact_item)
            if artifact_bundle is not None:
                return artifact_bundle
    return None


def _embedded_workspace_bundle_from_payload(payload: Mapping[str, object]) -> bytes | None:
    workspace_bundle = payload.get("workspace_bundle")
    if isinstance(workspace_bundle, Mapping):
        bundle = _workspace_bundle_mapping_to_zip(workspace_bundle)
        if bundle is not None:
            return bundle
    content = payload.get("content")
    if isinstance(content, Mapping):
        text = content.get("text")
        bundle = _embedded_workspace_bundle_from_text(text)
        if bundle is not None:
            return bundle
    for key in ("text", "output", "result", "summary"):
        bundle = _embedded_workspace_bundle_from_text(payload.get(key))
        if bundle is not None:
            return bundle
    return None


def _embedded_workspace_bundle_from_text(value: object) -> bytes | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("```"):
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            text = text[first : last + 1]
    parsed = _json_mapping_from_text(text)
    if isinstance(parsed, Mapping):
        bundle = _embedded_workspace_bundle_from_payload(parsed)
        if bundle is not None:
            return bundle
    return _markdown_file_blocks_to_zip(text)


def _json_mapping_from_text(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("```"):
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            text = text[first : last + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _workspace_bundle_mapping_to_zip(workspace_bundle: Mapping[str, object]) -> bytes | None:
    files = workspace_bundle.get("files")
    if not isinstance(files, Mapping) or not files:
        return None
    if len(files) > _EMBEDDED_WORKSPACE_MAX_FILES:
        return None
    normalized: dict[str, str] = {}
    total_bytes = 0
    for raw_path, raw_content in files.items():
        if not isinstance(raw_path, str) or not isinstance(raw_content, str):
            return None
        path = _safe_embedded_workspace_path(raw_path)
        if path is None:
            return None
        content_bytes = raw_content.encode("utf-8")
        if len(content_bytes) > _EMBEDDED_WORKSPACE_MAX_FILE_BYTES:
            return None
        total_bytes += len(content_bytes)
        if total_bytes > _EMBEDDED_WORKSPACE_MAX_TOTAL_BYTES:
            return None
        normalized[path] = raw_content
    if not normalized:
        return None
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for path, content in sorted(normalized.items()):
            archive.writestr(path, content)
    return buffer.getvalue()


def _markdown_file_blocks_to_zip(text: str) -> bytes | None:
    lines = text.splitlines()
    files: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        path_text = _markdown_file_heading_path(line)
        if path_text is None:
            index += 1
            continue
        path = _safe_embedded_workspace_path(path_text)
        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        if path is None or index >= len(lines) or not lines[index].lstrip().startswith("```"):
            continue
        index += 1
        content_lines: list[str] = []
        while index < len(lines) and not lines[index].lstrip().startswith("```"):
            content_lines.append(lines[index])
            index += 1
        if index < len(lines):
            index += 1
        files[path] = "\n".join(content_lines).rstrip() + "\n"
    if not files:
        files = _markdown_file_blocks_to_files_regex(text)
    if not files:
        return None
    return _workspace_bundle_mapping_to_zip({"files": files})


def _markdown_file_heading_path(line: str) -> str | None:
    if any(line.startswith(f"{prefix} `") for prefix in ("##", "###", "####")) and line.endswith("`"):
        return line.split("`", 1)[1][:-1]
    match = re.match(r"^#{2,4}\s+([A-Za-z0-9._/-]+)\s*$", line)
    if match is None:
        return None
    candidate = match.group(1)
    name = candidate.rsplit("/", 1)[-1]
    if "/" not in candidate and "." not in name and name not in {"Dockerfile", "Makefile", "README", "LICENSE"}:
        return None
    return candidate


def _markdown_file_blocks_to_files_regex(text: str) -> dict[str, str]:
    files: dict[str, str] = {}
    heading_pattern = re.compile(
        r"(?ms)#{2,4}\s+(?:`([^`\r\n]+)`|([A-Za-z0-9._/-]+))[ \t]*```[a-zA-Z0-9_-]*[ \t]*"
        r"(?:\r?\n)?(.*?)(?:\r?\n)?^[ \t]*```[ \t]*$"
    )
    for match in heading_pattern.finditer(text):
        path_text = match.group(1) or match.group(2)
        if (
            match.group(2)
            and not _is_plain_markdown_file_path(path_text)
        ):
            continue
        path = _safe_embedded_workspace_path(path_text)
        if path is None:
            continue
        files[path] = match.group(3).rstrip() + "\n"
    comment_pattern = re.compile(
        r"(?ms)^```[a-zA-Z0-9_-]*[ \t]*\r?\n[ \t]*(?://|#)\s*([A-Za-z0-9._/-]+)\s*\r?\n"
        r"(.*?)(?:\r?\n)?^[ \t]*```[ \t]*$"
    )
    for match in comment_pattern.finditer(text):
        path_text = match.group(1)
        if not _is_plain_markdown_file_path(path_text):
            continue
        path = _safe_embedded_workspace_path(path_text)
        if path is None or path in files:
            continue
        files[path] = match.group(2).rstrip() + "\n"
    return files


def _is_plain_markdown_file_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return "/" in path or "." in name or name in {"Dockerfile", "Makefile", "README", "LICENSE"}


def _safe_embedded_workspace_path(value: str) -> str | None:
    candidate = value.replace("\\", "/")
    if candidate.startswith("/") or "\x00" in candidate:
        return None
    path = candidate.strip("/")
    if not path:
        return None
    parts = [part for part in path.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    normalized = "/".join(parts)
    if len(normalized) > 512:
        return None
    return normalized


def _validate_mode_control(
    response: dict[str, object] | None,
    *,
    requested_body: dict[str, object],
    validation_focus: Sequence[str],
) -> list[str]:
    if (
        "mode_control" not in validation_focus
        and "no_silent_downgrade" not in validation_focus
    ):
        return []
    requested = requested_body.get("mode")
    if not isinstance(requested, str) or not requested:
        return ["mode_control: request missing mode"]
    if response is None:
        return ["mode_control: response missing mode"]
    actual = response.get("mode")
    if actual != requested:
        got = actual if isinstance(actual, str) and actual else "missing"
        return [f"mode_control: requested {requested} got {got}"]
    return []


def _validate_run_details_scope(details: dict[str, object], run_id: str) -> None:
    actual = details.get("id")
    if actual != run_id:
        got = actual if isinstance(actual, str) and actual else "missing"
        raise RuntimeError(f"run details scope mismatch: id expected {run_id} got {got}")


def _approve_pending_capability(
    client: AcceptanceClient,
    *,
    run_id: str,
    details: dict[str, object],
    approved_capabilities: set[str],
    errors: list[str],
) -> str | None:
    if details.get("status") != "waiting_approval":
        return None
    approval = _capability_approval_request(client, run_id=run_id, details=details)
    if approval is None:
        return None
    approval_id, version = approval
    if approval_id in approved_capabilities:
        return None
    approved_capabilities.add(approval_id)
    try:
        response = client.request_json(
            "POST",
            f"/api/v1/runs/{quote(run_id)}/approve-capability",
            body={"approval_id": approval_id, "version": version},
        )
    except RuntimeError as error:
        errors.append(f"capability_approval: {error}")
        return None
    if not isinstance(response, dict):
        errors.append("capability_approval: approve-capability returned non-object JSON")
        return None
    return _string_value(response.get("status"))


def _capability_approval_request(
    client: AcceptanceClient,
    *,
    run_id: str,
    details: dict[str, object],
) -> tuple[str, int] | None:
    approval = _capability_approval_from_mapping(details)
    if approval is not None:
        return approval
    try:
        admin_response = client.request_json("GET", f"/api/v1/admin/runs/{quote(run_id)}")
    except RuntimeError:
        return None
    if not isinstance(admin_response, dict):
        return None
    return _capability_approval_from_mapping(admin_response)


def _capability_approval_from_mapping(payload: Mapping[str, object]) -> tuple[str, int] | None:
    version = payload.get("version")
    approval_id = payload.get("approval_id")
    explicit_details = payload.get("explicit_details")
    if isinstance(explicit_details, Mapping):
        approval_id = approval_id or explicit_details.get("approval_id")
        raw_version = explicit_details.get("version")
        if isinstance(raw_version, str) and raw_version.isdigit():
            version = int(raw_version)
    if not isinstance(approval_id, str) or not approval_id:
        return None
    if not isinstance(version, int) or version <= 0:
        return None
    return approval_id, version


def _self_repair_acceptance_body(
    client: AcceptanceClient,
    *,
    run_id: str,
    details: Mapping[str, object],
) -> dict[str, object] | None:
    direct = _self_repair_acceptance_body_from_mapping(details)
    if direct is not None:
        return direct
    try:
        admin_response = client.request_json("GET", f"/api/v1/admin/runs/{quote(run_id)}")
    except RuntimeError:
        return None
    if not isinstance(admin_response, dict):
        return None
    return _self_repair_acceptance_body_from_mapping(admin_response)


def _self_repair_acceptance_body_from_mapping(
    mapping: Mapping[str, object],
) -> dict[str, object] | None:
    proposal = mapping.get("repair_proposal")
    if not isinstance(proposal, Mapping):
        return None
    decision_token = mapping.get("decision_token")
    version = mapping.get("version")
    if not isinstance(decision_token, str) or not decision_token:
        return None
    if not isinstance(version, int) or version <= 0:
        return None
    return {"decision_token": decision_token, "version": version}


def _validate_run_events_scope(events: list[object], run_id: str) -> list[str]:
    for event in events:
        if not isinstance(event, dict):
            continue
        field, actual = _event_run_reference(event)
        if actual is None:
            continue
        if actual != run_id:
            return [f"run events scope mismatch: {field} expected {run_id} got {actual}"]
    return []


def _event_run_reference(event: dict[object, object]) -> tuple[str, str | None]:
    for field in ("run_id", "runId"):
        value = event.get(field)
        if isinstance(value, str) and value:
            return field, value
    run = event.get("run")
    if isinstance(run, dict):
        value = run.get("id")
        if isinstance(value, str) and value:
            return "run.id", value
    return "run_id", None


def _idempotency_key(case_id: str, index: int, *, execution_id: str | None = None) -> str:
    safe_case = case_id.replace(":", "-").replace("_", "-")
    key = f"project-scale-{safe_case}-{index}"
    if execution_id is not None:
        key = f"{key}-{_safe_idempotency_token(execution_id)}"
    return key[:90]


def _scoped_execution_body(
    body: dict[str, object],
    *,
    execution_id: str | None,
) -> dict[str, object]:
    if execution_id is None:
        return body
    scoped = dict(body)
    session_id = _string_value(scoped.get("workspace_session_id"))
    if session_id:
        scoped["workspace_session_id"] = (
            f"{session_id}-{_safe_idempotency_token(execution_id)}"
        )[:120]
    return scoped


def _deliverable_repair_idempotency_key(
    case_id: str,
    index: int,
    *,
    execution_id: str | None = None,
) -> str:
    return f"{_idempotency_key(case_id, index, execution_id=execution_id)}-deliverable-repair"[:90]


def _deliverable_repair_body(
    body: dict[str, object],
    case_id: str,
    *,
    failed_reasons: Sequence[str] = (),
    benchmark_kind: str = "fixture",
) -> dict[str, object]:
    repair_body = dict(body)
    original_message = body.get("message")
    if benchmark_kind == "capability":
        original = original_message if isinstance(original_message, str) else ""
        guidance = (
            "Repair same project; preserve requirements. Fix defects. Return full bundle: "
            "source/tests/package config, README, PROJECT_REQUIREMENTS.md, "
            "IMPLEMENTATION_PLAN.md, VERIFICATION.md, constraints_reading_evidence.json. "
            "Report only executed checks.\n"
        )
        reasons = _format_failed_reasons(failed_reasons)
        prefix = guidance + reasons + "\nOriginal request:\n"
        available = 2_000 - len(" ".join(prefix.split())) - 1
        bounded_original = original[: max(available, 0)]
        repair_body["message"] = _bounded_role_planning_task_text(prefix + bounded_original)
        repair_body["skip_evolution_proposal"] = True
        return repair_body
    reason_text = _format_failed_reasons(failed_reasons)
    direct_guidance = (
        " This is a direct run: do not call tools, do not emit DSML/tool-call syntax, and "
        "do not describe commands as if they were executed. Return a machine-verifiable "
        "deliverable inline as strict JSON with workspace_bundle.files mapping safe relative "
        "paths to complete file contents, or as Markdown file blocks headed exactly like "
        "### `path/to/file` followed by a fenced code block. Include README or requirements, "
        "source files, tests or build scripts, implementation plan, and verification report "
        "with reproducible build, test, and interaction evidence. Avoid credential-like terms "
        "and avoid package, file, variable, or fixture names that contain the sk- prefix so "
        "public evidence stays visible."
        if body.get("mode") == "direct" or case_id.endswith(":direct")
        else ""
    )
    repair_message = (
        f"Project-scale deliverable repair for {case_id}: the previous generated project "
        "failed acceptance quality. Diagnose the mismatches against the original request, "
        "repair the implementation in the same workspace, rerun build/test/interaction checks, "
        "remove placeholders or stub-only output, and record deliverable_quality with "
        "requirements_satisfied, build_passed, tests_passed, interactive_checks_passed, "
        "no_placeholders, and artifact_integrity all true. Also record "
        "agent_standard_verification with constraints_read, plan_before_implementation, "
        "reproducible_verification, and root_cause_repair all true, and keep implementation "
        "plan plus verification notes in the workspace. Include constraints_reading_evidence.json "
        "or an implementation-plan section that names the AGENTS/HANDOFF/requirements and "
        "skill or rule sources read before implementation. When the run uses dispatch, hybrid, "
        "multi-agent, discussion, or repair coordination, record discussion_trace with "
        "participants, member statements, disagreements, verification steps, and final decision "
        "so the workbench can show the scheduling debate. For plugin flows, also record "
        "plugin_contract with manifest discovery, adapter contracts, sandbox and policy "
        f"boundaries, and failure recovery behavior.{direct_guidance}{reason_text} "
        "Original request:\n"
        f"{original_message if isinstance(original_message, str) else ''}"
    )
    repair_body["message"] = _bounded_role_planning_task_text(repair_message)
    repair_body["skip_evolution_proposal"] = True
    return repair_body


def _bounded_role_planning_task_text(value: str, *, max_chars: int = 2_000) -> str:
    text = " ".join(value.split())
    if len(text) <= max_chars:
        return text
    marker = "Original request:"
    marker_index = text.find(marker)
    if marker_index > 0:
        prefix = text[: marker_index + len(marker)].strip()
        remaining = max_chars - len(prefix) - len(" ...") - 1
        if remaining > 0:
            return f"{prefix} {text[marker_index + len(marker):][:remaining].strip()} ...".strip()
    return text[: max_chars - 4].rstrip() + " ..."


def _format_failed_reasons(reasons: Sequence[str]) -> str:
    unique: dict[str, None] = {}
    for reason in reasons:
        if reason:
            unique.setdefault(reason, None)
    if not unique:
        return ""
    return "\nPrevious failed evidence:\n" + "\n".join(f"- {reason}" for reason in unique) + "\n"


def _default_execution_id() -> str:
    return _safe_idempotency_token(f"{int(time.time())}-{os.getpid()}")


def _safe_idempotency_token(value: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in "._:-" else "-"
        for character in value.strip()
    ).strip("-")
    return (safe or "run")[:48]


def _case_requires_project_preflight(case_id: str) -> bool:
    scale, _, _flow = case_id.partition(":")
    return scale in {"large", "ultra"}


def _case_requires_discussion_trace(case_id: str) -> bool:
    _scale, _, flow = case_id.partition(":")
    return flow in _DISCUSSION_TRACE_FLOWS


def _case_requires_plugin_contract(case_id: str) -> bool:
    _scale, _, flow = case_id.partition(":")
    return flow == "plugin"


def _project_preflight_approval_body(response: dict[str, object]) -> dict[str, object]:
    if response.get("status") != "waiting_approval":
        raise RuntimeError("project preflight run did not wait for approval")
    token = response.get("decision_token")
    version = response.get("version")
    if not isinstance(token, str) or not token:
        raise RuntimeError("project preflight run missing decision_token")
    if not isinstance(version, int) or version <= 0:
        raise RuntimeError("project preflight run missing version")
    return {"decision_token": token, "version": version}


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _is_terminal_status(status: str | None) -> bool:
    return status in _TERMINAL_STATUSES


def _has_final_artifacts(details: dict[str, object]) -> bool:
    artifacts = details.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        return True
    final_artifacts = details.get("final_artifacts")
    if isinstance(final_artifacts, list) and final_artifacts:
        return True
    artifact_ids = details.get("artifact_ids")
    if isinstance(artifact_ids, list) and artifact_ids:
        return True
    artifact_count = details.get("artifact_count")
    return isinstance(artifact_count, int) and artifact_count > 0


def _has_deliverable_quality(
    details: dict[str, object] | None,
    events: list[object] | None,
    workspace_bundle: bytes | None,
) -> bool:
    return _evaluate_deliverable_quality(details, events, workspace_bundle).passed


def _has_agent_standard_verification(
    details: dict[str, object] | None,
    events: list[object] | None,
    workspace_bundle: bytes | None,
    *,
    benchmark_kind: ProjectScaleBenchmarkKind,
) -> bool:
    return _evaluate_agent_standard_verification(
        details, events, workspace_bundle, benchmark_kind=benchmark_kind
    ).passed


def _evaluate_deliverable_quality(
    details: dict[str, object] | None,
    events: list[object] | None,
    workspace_bundle: bytes | None,
) -> _EvidenceCheck:
    reasons: list[str] = []
    if not (
        _has_quality_payload(details, events)
        or _workspace_bundle_has_quality_payload(workspace_bundle)
        or _workspace_bundle_has_project_quality(workspace_bundle)
    ):
        reasons.append("deliverable_quality: missing or incomplete structured quality flags")
    reasons.extend(_workspace_bundle_project_quality_reasons(workspace_bundle))
    return _EvidenceCheck(passed=not reasons, reasons=tuple(reasons))


def _evaluate_agent_standard_verification(
    details: dict[str, object] | None,
    events: list[object] | None,
    workspace_bundle: bytes | None,
    *,
    benchmark_kind: ProjectScaleBenchmarkKind,
) -> _EvidenceCheck:
    if benchmark_kind == "capability":
        reasons: list[str] = []
        if not _has_trusted_agent_standard_event(events):
            reasons.append(
                "agent_standard_verification: trusted runtime context/plan evidence unavailable"
            )
        if not _workspace_bundle_has_agent_standard_evidence(workspace_bundle):
            reasons.extend(_workspace_bundle_agent_standard_reasons(workspace_bundle))
        return _EvidenceCheck(passed=not reasons, reasons=tuple(reasons))
    reasons = []
    has_structured_evidence = _has_agent_standard_payload(
        details, events
    ) or _workspace_bundle_has_agent_standard_payload(workspace_bundle)
    has_workspace_evidence = _workspace_bundle_has_agent_standard_evidence(workspace_bundle)
    if not (has_structured_evidence or has_workspace_evidence):
        reasons.append(
            "agent_standard_verification: missing or incomplete Codex/Claude standard flags"
        )
    if not has_structured_evidence:
        reasons.extend(_workspace_bundle_agent_standard_reasons(workspace_bundle))
    return _EvidenceCheck(passed=not reasons, reasons=tuple(reasons))


def _has_trusted_agent_standard_event(events: list[object] | None) -> bool:
    if not isinstance(events, list):
        return False
    return any(_event_has_trusted_agent_standard_payload(event) for event in events)


def _event_has_trusted_agent_standard_payload(event: object) -> bool:
    if not isinstance(event, Mapping):
        return False
    if not _mapping_has_agent_standard_payload(event):
        return False
    kind = _string_value(event.get("kind") or event.get("event") or event.get("type"))
    tool_name = _string_value(event.get("tool_name") or event.get("toolName"))
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        tool_name = tool_name or _string_value(payload.get("tool_name") or payload.get("toolName"))
    if tool_name in {"project.generate_zip", "workspace.generate_zip"}:
        return True
    if kind is None:
        return False
    normalized = kind.replace("_", ".").casefold()
    return normalized in {
        "artifact.created",
        "runtime.completed",
        "tool.completed",
        "tool.result",
    }


def _evaluate_discussion_trace(
    details: dict[str, object] | None,
    events: list[object] | None,
    *,
    case_id: str,
) -> _EvidenceCheck:
    if not _case_requires_discussion_trace(case_id):
        return _EvidenceCheck(passed=False, reasons=())
    if _has_discussion_trace_payload(details, events):
        return _EvidenceCheck(passed=True, reasons=())
    return _EvidenceCheck(
        passed=False,
        reasons=("discussion_trace: missing hybrid/discussion process evidence",),
    )


def _evaluate_plugin_contract(
    details: dict[str, object] | None,
    events: list[object] | None,
    *,
    case_id: str,
) -> _EvidenceCheck:
    if not _case_requires_plugin_contract(case_id):
        return _EvidenceCheck(passed=False, reasons=())
    if _has_plugin_contract_payload(details, events):
        return _EvidenceCheck(passed=True, reasons=())
    return _EvidenceCheck(
        passed=False,
        reasons=("plugin_contract: missing or incomplete plugin capability contract evidence",),
    )


def _has_quality_payload(details: dict[str, object] | None, events: list[object] | None) -> bool:
    if details is not None and _mapping_has_quality_payload(details):
        return True
    if events is None:
        return False
    return any(isinstance(event, dict) and _mapping_has_quality_payload(event) for event in events)


def _mapping_has_quality_payload(mapping: Mapping[str, object]) -> bool:
    for key in _QUALITY_PAYLOAD_KEYS:
        value = mapping.get(key)
        if _quality_payload_passes(value):
            return True
        payload = mapping.get("payload")
        if isinstance(payload, Mapping) and _quality_payload_passes(payload.get(key)):
            return True
    for embedded in _embedded_structured_mappings(mapping):
        if _mapping_has_quality_payload(embedded):
            return True
    return False


def _has_agent_standard_payload(
    details: dict[str, object] | None,
    events: list[object] | None,
) -> bool:
    if details is not None and _mapping_has_agent_standard_payload(details):
        return True
    if events is None:
        return False
    return any(
        isinstance(event, dict) and _mapping_has_agent_standard_payload(event) for event in events
    )


def _mapping_has_agent_standard_payload(mapping: Mapping[str, object]) -> bool:
    for key in _AGENT_STANDARD_PAYLOAD_KEYS:
        value = mapping.get(key)
        if _agent_standard_payload_passes(value):
            return True
        payload = mapping.get("payload")
        if isinstance(payload, Mapping) and _agent_standard_payload_passes(payload.get(key)):
            return True
    for embedded in _embedded_structured_mappings(mapping):
        if _mapping_has_agent_standard_payload(embedded):
            return True
    return False


def _embedded_structured_mappings(mapping: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    values: list[object] = []
    content = mapping.get("content")
    if isinstance(content, Mapping):
        values.append(content.get("text"))
    artifact = mapping.get("artifact")
    if isinstance(artifact, Mapping):
        artifact_content = artifact.get("content")
        if isinstance(artifact_content, Mapping):
            values.append(artifact_content.get("text"))
    payload = mapping.get("payload")
    if isinstance(payload, Mapping):
        values.extend(payload.get(key) for key in ("text", "output", "result", "summary"))
    values.extend(mapping.get(key) for key in ("text", "output", "result", "summary"))
    parsed: list[Mapping[str, object]] = []
    for value in values:
        item = _json_mapping_from_text(value)
        if item is not None:
            parsed.append(item)
    return tuple(parsed)


def _has_discussion_trace_payload(
    details: dict[str, object] | None,
    events: list[object] | None,
) -> bool:
    if details is not None and _mapping_has_discussion_trace_payload(details):
        return True
    if events is None:
        return False
    return any(
        isinstance(event, dict) and _mapping_has_discussion_trace_payload(event)
        for event in events
    )


def _mapping_has_discussion_trace_payload(mapping: Mapping[str, object]) -> bool:
    for key in _DISCUSSION_TRACE_PAYLOAD_KEYS:
        value = mapping.get(key)
        if _discussion_trace_payload_passes(value):
            return True
        payload = mapping.get("payload")
        if isinstance(payload, Mapping) and _discussion_trace_payload_passes(payload.get(key)):
            return True
    return False


def _has_plugin_contract_payload(
    details: dict[str, object] | None,
    events: list[object] | None,
) -> bool:
    if details is not None and _mapping_has_plugin_contract_payload(details):
        return True
    if events is None:
        return False
    return any(
        isinstance(event, dict) and _mapping_has_plugin_contract_payload(event)
        for event in events
    )


def _mapping_has_plugin_contract_payload(mapping: Mapping[str, object]) -> bool:
    for key in _PLUGIN_CONTRACT_PAYLOAD_KEYS:
        value = mapping.get(key)
        if _plugin_contract_payload_passes(value):
            return True
        payload = mapping.get("payload")
        if isinstance(payload, Mapping) and _plugin_contract_payload_passes(payload.get(key)):
            return True
    return False


def _agent_standard_payload_passes(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return all(value.get(key) is True for key in _AGENT_STANDARD_KEYS)


def _quality_payload_passes(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return all(value.get(key) is True for key in _QUALITY_KEYS)


def _discussion_trace_payload_passes(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        _non_empty_text_sequence(value.get("participants"))
        and _member_statements_pass(value.get("member_statements"))
        and _has_present_field(value, ("disagreements", "disagreement_summary"))
        and _non_empty_text_sequence(value.get("verification_steps"))
        and _non_empty_text(value.get("final_decision"))
    )


def _plugin_contract_payload_passes(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return all(value.get(key) is True for key in _PLUGIN_CONTRACT_KEYS) and all(
        _has_present_field(value, keys) for keys in _PLUGIN_CONTRACT_DETAIL_KEYS
    )


def _non_empty_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes) and bool(value)


def _non_empty_text_sequence(value: object) -> bool:
    if not _non_empty_sequence(value):
        return False
    items = cast(Sequence[object], value)
    return all(_non_empty_text(item) for item in items)


def _member_statements_pass(value: object) -> bool:
    if not _non_empty_sequence(value):
        return False
    items = cast(Sequence[object], value)
    for item in items:
        if not isinstance(item, Mapping):
            return False
        if not _has_present_field(item, ("member", "agent", "role", "name")):
            return False
        if not _has_present_field(
            item,
            ("position", "statement", "opinion", "message", "text", "summary"),
        ):
            return False
    return True


def _non_empty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_present_field(mapping: Mapping[str, object], keys: Sequence[str]) -> bool:
    for key in keys:
        if key not in mapping:
            continue
        if _value_has_present_text(mapping[key]):
            return True
    return False


def _value_has_present_text(value: object) -> bool:
    if _non_empty_text(value):
        return True
    if isinstance(value, Mapping):
        return any(_value_has_present_text(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return any(_value_has_present_text(item) for item in value)
    return False


def _workspace_bundle_has_project_quality(workspace_bundle: bytes | None) -> bool:
    return not _workspace_bundle_project_quality_reasons(workspace_bundle)


def _executed_capability_quality(
    workspace_bundle: bytes | None, validation: _EvidenceCheck
) -> _EvidenceCheck:
    if not validation.passed:
        return validation
    # Actual build and independent HTTP checks supersede author-written pass claims.
    claimed_evidence = {
        "workspace_bundle: missing build/test execution evidence",
        "workspace_bundle: missing interaction execution evidence",
        "workspace_bundle: contains placeholder or stub markers",
    }
    reasons = [
        reason for reason in _workspace_bundle_project_quality_reasons(workspace_bundle)
        if reason not in claimed_evidence
    ]
    if workspace_bundle is not None:
        try:
            with zipfile.ZipFile(BytesIO(workspace_bundle)) as archive:
                source = _workspace_bundle_source_text(archive, archive.namelist()).lower()
            if any(marker in source for marker in _PLACEHOLDER_MARKERS):
                reasons.append("workspace_bundle: source contains placeholder or stub markers")
        except (OSError, zipfile.BadZipFile):
            pass  # The structural check above already reports invalid archives.
    return _EvidenceCheck(passed=not reasons, reasons=tuple(reasons))


def _workspace_bundle_project_quality_reasons(workspace_bundle: bytes | None) -> tuple[str, ...]:
    if not workspace_bundle:
        return ("workspace_bundle: missing project bundle",)
    try:
        with zipfile.ZipFile(BytesIO(workspace_bundle)) as archive:
            names = tuple(name for name in archive.namelist() if not name.endswith("/"))
            lowered = tuple(name.lower() for name in names)
            if not lowered:
                return ("workspace_bundle: empty project bundle",)
            text = _workspace_bundle_text(archive, names)
            verification_text = _workspace_bundle_verification_text(archive, names)
            test_text = _workspace_bundle_test_text(archive, names)
            source_text = _workspace_bundle_source_text(archive, names)
    except (OSError, zipfile.BadZipFile):
        return ("workspace_bundle: invalid or unreadable zip bundle",)

    reasons: list[str] = []
    if not _bundle_has_requirement_document(lowered):
        reasons.append("workspace_bundle: missing requirements or README artifact")
    if not _bundle_has_source_files(lowered):
        reasons.append("workspace_bundle: missing source files")
    elif not _source_text_has_meaningful_implementation(source_text):
        reasons.append("workspace_bundle: missing meaningful source implementation")
    if not _bundle_has_verification_file_path(lowered):
        reasons.append("workspace_bundle: missing test or verification file path")
    if not _test_text_has_meaningful_assertions(test_text):
        reasons.append("workspace_bundle: missing meaningful test assertions")
    if not _bundle_has_build_test_execution_evidence(verification_text):
        reasons.append("workspace_bundle: missing build/test execution evidence")
    if not _bundle_has_interaction_execution_evidence(verification_text):
        reasons.append("workspace_bundle: missing interaction execution evidence")
    if any(marker in text.lower() for marker in _PLACEHOLDER_MARKERS):
        reasons.append("workspace_bundle: contains placeholder or stub markers")
    return tuple(reasons)


def _workspace_bundle_has_agent_standard_evidence(workspace_bundle: bytes | None) -> bool:
    return not _workspace_bundle_agent_standard_reasons(workspace_bundle)


def _workspace_bundle_agent_standard_reasons(workspace_bundle: bytes | None) -> tuple[str, ...]:
    if not workspace_bundle:
        return ("workspace_bundle: missing project bundle",)
    try:
        with zipfile.ZipFile(BytesIO(workspace_bundle)) as archive:
            names = tuple(name for name in archive.namelist() if not name.endswith("/"))
            lowered = tuple(name.lower() for name in names)
            plan_text = _workspace_bundle_named_text(archive, names, _IMPLEMENTATION_PLAN_BASENAMES)
            reading_evidence = _workspace_bundle_has_reading_evidence(archive, names)
    except (OSError, zipfile.BadZipFile):
        return ("workspace_bundle: invalid or unreadable zip bundle",)
    basenames = {name.rsplit("/", 1)[-1] for name in lowered}
    reasons: list[str] = []
    if not (basenames & _IMPLEMENTATION_PLAN_BASENAMES):
        reasons.append("workspace_bundle: missing implementation plan artifact")
    elif not (reading_evidence or _implementation_plan_has_reading_evidence(plan_text)):
        reasons.append(
            "workspace_bundle: missing constraints and skill/rule reading evidence in implementation plan"
        )
    if not (basenames & _VERIFICATION_REPORT_BASENAMES):
        reasons.append("workspace_bundle: missing verification report artifact")
    return tuple(reasons)


def _workspace_bundle_named_text(
    archive: zipfile.ZipFile,
    names: Sequence[str],
    basenames: frozenset[str],
) -> str:
    chunks: list[str] = []
    for name in names:
        if name.lower().rsplit("/", 1)[-1] not in basenames:
            continue
        try:
            chunks.append(archive.read(name, pwd=None).decode("utf-8", errors="ignore")[:120_000])
        except (KeyError, RuntimeError, OSError):
            continue
    return "\n".join(chunks)


def _workspace_bundle_has_reading_evidence(
    archive: zipfile.ZipFile,
    names: Sequence[str],
) -> bool:
    for name in names:
        if name.lower().rsplit("/", 1)[-1] not in _READING_EVIDENCE_BASENAMES:
            continue
        try:
            raw = archive.read(name, pwd=None).decode("utf-8", errors="ignore")[:120_000]
        except (KeyError, RuntimeError, OSError):
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if _reading_evidence_payload_passes(parsed):
            return True
    return False


def _reading_evidence_payload_passes(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    read_before_plan = value.get("read_before_plan") is True or value.get(
        "read_before_implementation"
    ) is True
    return (
        read_before_plan
        and _reading_evidence_has_required_constraint_sources(
            value,
            ("constraints", "constraint_sources", "files_read", "documents_read", "sources"),
        )
        and _reading_evidence_has_required_skill_sources(
            value,
            ("skills", "skill_rules", "rules", "skill_sources"),
        )
    )


def _reading_evidence_has_required_constraint_sources(
    mapping: Mapping[str, object],
    keys: Sequence[str],
) -> bool:
    text = _present_field_text(mapping, keys)
    lowered = text.casefold()
    return (
        _has_marker(lowered, ("agents.md", "workspace rules", "项目规则", "工作区规则"))
        and _has_marker(lowered, ("handoff", "交接"))
        and _has_marker(
            lowered,
            (
                "project_requirements",
                "project requirements",
                "requirements.md",
                "requirements",
                "需求",
            ),
        )
    )


def _reading_evidence_has_required_skill_sources(
    mapping: Mapping[str, object],
    keys: Sequence[str],
) -> bool:
    lowered = _present_field_text(mapping, keys).casefold()
    return _has_marker(
        lowered,
        (
            "skill.md",
            "skill inventory",
            "applicable skill",
            "project-specific skill",
            "project specific skill",
            "agent-standard rules",
            "workspace rules",
            "技能",
            "规则",
        ),
    )


def _present_field_text(mapping: Mapping[str, object], keys: Sequence[str]) -> str:
    values = [mapping[key] for key in keys if key in mapping]
    return " ".join(_flatten_present_text(value) for value in values)


def _flatten_present_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return " ".join(_flatten_present_text(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return " ".join(_flatten_present_text(item) for item in value)
    return ""


def _implementation_plan_has_reading_evidence(plan_text: str) -> bool:
    lowered = plan_text.casefold()
    before_markers = (
        "before implementation",
        "before build",
        "before coding",
        "before execution",
        "read_before_plan",
        "先读",
        "先读取",
    )
    return (
        _plan_text_has_required_constraint_sources(lowered)
        and _plan_text_has_required_skill_sources(lowered)
        and _has_marker(lowered, before_markers)
    )


def _plan_text_has_required_constraint_sources(lowered: str) -> bool:
    return (
        _has_marker(lowered, ("agents.md", "workspace rules", "项目规则", "工作区规则"))
        and _has_marker(lowered, ("handoff", "交接"))
        and _has_marker(
            lowered,
            (
                "project_requirements",
                "project requirements",
                "requirements.md",
                "requirements",
                "需求",
            ),
        )
    )


def _plan_text_has_required_skill_sources(lowered: str) -> bool:
    return _has_marker(
        lowered,
        (
            "skill.md",
            "skill inventory",
            "applicable skill",
            "project-specific skill",
            "project specific skill",
            "agent-standard rules",
            "workspace rules",
            "技能",
            "规则",
        ),
    )


def _workspace_bundle_text(archive: zipfile.ZipFile, names: Sequence[str]) -> str:
    chunks: list[str] = []
    for name in names:
        if not _is_text_candidate(name):
            continue
        try:
            chunks.append(archive.read(name, pwd=None).decode("utf-8", errors="ignore")[:120_000])
        except (KeyError, RuntimeError, OSError):
            continue
    return "\n".join(chunks)


def _workspace_bundle_verification_text(archive: zipfile.ZipFile, names: Sequence[str]) -> str:
    chunks: list[str] = []
    for name in names:
        if name.lower().rsplit("/", 1)[-1] not in _VERIFICATION_REPORT_BASENAMES:
            continue
        try:
            chunks.append(archive.read(name, pwd=None).decode("utf-8", errors="ignore")[:120_000])
        except (KeyError, RuntimeError, OSError):
            continue
    return "\n".join(chunks)


def _workspace_bundle_test_text(archive: zipfile.ZipFile, names: Sequence[str]) -> str:
    chunks: list[str] = []
    for name in names:
        if not _is_test_file_name(name.lower()):
            continue
        try:
            chunks.append(archive.read(name, pwd=None).decode("utf-8", errors="ignore")[:120_000])
        except (KeyError, RuntimeError, OSError):
            continue
    return "\n".join(chunks)


def _workspace_bundle_source_text(archive: zipfile.ZipFile, names: Sequence[str]) -> str:
    chunks: list[str] = []
    for name in names:
        lowered = name.lower()
        if not _is_project_source_file(lowered) and lowered not in {"main.py", "index.html"}:
            continue
        try:
            chunks.append(archive.read(name, pwd=None).decode("utf-8", errors="ignore")[:120_000])
        except (KeyError, RuntimeError, OSError):
            continue
    return "\n".join(chunks)


def _workspace_bundle_json_mappings(workspace_bundle: bytes | None) -> tuple[Mapping[str, object], ...]:
    if not workspace_bundle:
        return ()
    try:
        with zipfile.ZipFile(BytesIO(workspace_bundle)) as archive:
            names = tuple(name for name in archive.namelist() if not name.endswith("/"))
            mappings: list[Mapping[str, object]] = []
            for name in names:
                if not name.lower().endswith(".json"):
                    continue
                try:
                    raw = archive.read(name, pwd=None).decode("utf-8", errors="ignore")[:120_000]
                except (KeyError, RuntimeError, OSError):
                    continue
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, Mapping):
                    mappings.append(parsed)
    except (OSError, zipfile.BadZipFile):
        return ()
    return tuple(mappings)


def _workspace_bundle_has_quality_payload(workspace_bundle: bytes | None) -> bool:
    return any(
        _mapping_has_quality_payload(mapping)
        for mapping in _workspace_bundle_json_mappings(workspace_bundle)
    )


def _workspace_bundle_has_agent_standard_payload(workspace_bundle: bytes | None) -> bool:
    return any(
        _mapping_has_agent_standard_payload(mapping)
        for mapping in _workspace_bundle_json_mappings(workspace_bundle)
    )


def _read_bundle_file(archive: zipfile.ZipFile, names: Sequence[str], filename: str) -> str:
    for name in names:
        if name.lower().rsplit("/", 1)[-1] != filename:
            continue
        try:
            return archive.read(name, pwd=None).decode("utf-8", errors="ignore")[:120_000]
        except (KeyError, RuntimeError, OSError):
            return ""
    return ""


def _is_text_candidate(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(
        (
            ".css",
            ".html",
            ".js",
            ".json",
            ".jsx",
            ".md",
            ".py",
            ".ts",
            ".tsx",
            ".txt",
            ".yaml",
            ".yml",
        )
    )


def _bundle_has_requirement_document(lowered_names: Sequence[str]) -> bool:
    basenames = {name.rsplit("/", 1)[-1] for name in lowered_names}
    return bool(
        basenames
        & {
            "readme.md",
            "project_requirements.md",
            "requirements.md",
            "spec.md",
            "acceptance.md",
        }
    )


def _bundle_has_source_files(lowered_names: Sequence[str]) -> bool:
    return any(
        name.startswith(("src/", "app/", "pages/"))
        or name.endswith(("/main.py", "/main.ts", "/main.tsx", "/index.html"))
        or _is_project_source_file(name)
        or name in {"main.py", "index.html"}
        for name in lowered_names
    )


def _is_project_source_file(name: str) -> bool:
    if "/" not in name:
        return False
    if name.startswith(("tests/", "test/", "scripts/", "docs/", "quality/")):
        return False
    if "/tests/" in name or "/test/" in name:
        return False
    return name.endswith((".py", ".js", ".jsx", ".ts", ".tsx"))


def _bundle_has_verification_file_path(
    lowered_names: Sequence[str],
) -> bool:
    has_test_path = any(_is_test_file_name(name) for name in lowered_names)
    return has_test_path


def _is_test_file_name(name: str) -> bool:
    return (
        name.startswith(("tests/", "test/"))
        or "/tests/" in name
        or name.endswith(
            (
                ".test.js",
                ".test.jsx",
                ".test.py",
                ".test.ts",
                ".test.tsx",
                ".spec.js",
                ".spec.jsx",
                ".spec.py",
                ".spec.ts",
                ".spec.tsx",
            )
        )
    )


def _test_text_has_meaningful_assertions(test_text: str) -> bool:
    lowered = test_text.lower()
    return any(
        marker in lowered
        for marker in (
            "assert ",
            "assert(",
            "assert.",
            "expect(",
            ".tobe(",
            ".toequal(",
            "self.assert",
            "pytest.",
            "unittest.",
        )
    )


def _source_text_has_meaningful_implementation(source_text: str) -> bool:
    stripped = _source_text_without_comments(source_text)
    lowered = stripped.lower()
    if len(re.sub(r"\s+", "", stripped)) < 40:
        return False
    if any(marker in lowered for marker in ("hello world", "dummy implementation")):
        return False
    return any(
        re.search(pattern, stripped, flags=re.IGNORECASE)
        for pattern in (
            r"\bfunction\s+\w+\s*\(",
            r"\bclass\s+\w+",
            r"=>",
            r"\bdef\s+\w+\s*\(",
            r"\b(if|for|while|switch|try|catch)\b",
            r"\b(addEventListener|querySelector|fetch|map|filter|reduce|setState)\s*\(",
            r"\bexport\s+(?:async\s+)?function\s+\w+\s*\(",
        )
    )


def _source_text_without_comments(source_text: str) -> str:
    without_block_comments = re.sub(r"/\*.*?\*/", "", source_text, flags=re.DOTALL)
    lines: list[str] = []
    for line in without_block_comments.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "//")):
            continue
        lines.append(line)
    return "\n".join(lines)


def _bundle_has_build_test_execution_evidence(verification_text: str) -> bool:
    lowered = verification_text.lower()
    has_success_summary = any(
        marker in lowered
        for marker in (
            "all checks passed",
            "all tests passed",
            "all checks pass",
            "all tests pass",
            "\nok\n",
        )
    )
    return (
        (
            _line_has_execution_pass(
                lowered,
                ("build", "npm run build", "pnpm build", "yarn build"),
            )
            or _nearby_block_has_execution_pass(
                lowered,
                ("build", "npm run build", "pnpm build", "yarn build", "compileall"),
            )
            or (has_success_summary and _has_marker(lowered, ("build", "compileall")))
        )
        and (
            _line_has_execution_pass(lowered, ("test", "npm test", "npm run test", "pytest"))
            or _nearby_block_has_execution_pass(
                lowered,
                ("test", "npm test", "npm run test", "pytest", "unittest"),
            )
            or (has_success_summary and _has_marker(lowered, ("test", "pytest", "unittest")))
        )
    )


def _bundle_has_interaction_execution_evidence(verification_text: str) -> bool:
    lowered = verification_text.lower()
    interaction_markers = (
        "interaction",
        "interactive",
        "smoke",
        "e2e",
        "playwright",
        "cypress",
        "browser",
        "manual check",
        "manual verification",
    )
    lines = [line.strip() for line in lowered.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if not any(marker in line for marker in interaction_markers):
            continue
        block = "\n".join(lines[index : index + 6])
        if _has_interaction_pass_marker(block):
            return True
    return False


def _has_interaction_pass_marker(text: str) -> bool:
    return (
        _EXECUTION_PASS_RE.search(text) is not None
        or "interactive_checks_passed: true" in text
        or "interaction_checks_passed: true" in text
        or "smoke: true" in text
        or "e2e: true" in text
    )


def _has_marker(text: str, markers: Sequence[str]) -> bool:
    return any(marker in text for marker in markers)


def _line_has_execution_pass(text: str, command_markers: Sequence[str]) -> bool:
    for line in text.splitlines():
        if not any(marker in line for marker in command_markers):
            continue
        if _has_execution_pass_marker(line):
            return True
    return False


def _nearby_block_has_execution_pass(text: str, command_markers: Sequence[str]) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if not any(marker in line for marker in command_markers):
            continue
        block = "\n".join(lines[index : index + 6])
        if _has_execution_pass_marker(block):
            return True
        if "ran " in block and " tests" in block:
            return True
    return False


def _has_execution_pass_marker(text: str) -> bool:
    return _EXECUTION_PASS_RE.search(text) is not None and _has_execution_detail_marker(text)


def _has_execution_detail_marker(text: str) -> bool:
    return (
        re.search(r"\bexit(?:\s+code)?\s*[:=]?\s*0\b", text) is not None
        or re.search(r"\bran\s+\d+\s+tests?\b", text) is not None
        or re.search(r"\b\d+\s+(?:tests?\s+)?passed\b", text) is not None
        or "0 failed" in text
        or "compileall" in text
        or "byte-compile" in text
        or "node --check" in text
        or "tsc -p" in text
        or "vite build" in text
        or "vitest" in text
        or "pytest" in text
        or "unittest" in text
    )


def _should_attempt_deliverable_repair(
    *,
    status: str | None,
    evidence: dict[str, bool],
    case_id: str,
    benchmark_kind: ProjectScaleBenchmarkKind,
) -> bool:
    return (
        status in {"completed", "failed"}
        and evidence.get("final_artifacts") is True
        and (
            evidence.get("workspace_bundle") is not True
            or evidence.get("deliverable_quality") is not True
            or evidence.get("agent_standard_verification") is not True
            or (
                _case_requires_discussion_trace(case_id)
                and evidence.get("discussion_trace") is not True
            )
            or (
                _case_requires_plugin_contract(case_id)
                and evidence.get("plugin_contract") is not True
            )
            or evidence.get("generated_project_validation") is False
        )
    )


def _has_deliverable_repair_trace(events: list[object] | None) -> bool:
    if not isinstance(events, list):
        return False
    return any(_event_has_deliverable_repair_marker(event) for event in events)


def _event_has_deliverable_repair_marker(event: object) -> bool:
    if not isinstance(event, Mapping):
        return False
    for key in ("kind", "event", "type", "action"):
        marker = event.get(key)
        if not isinstance(marker, str):
            continue
        normalized = marker.lower()
        if normalized.startswith(("deliverable.repair.", "repair.deliverable.")):
            return True
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        return _event_has_deliverable_repair_marker(payload)
    return False


def _has_self_repair_trace(events: object) -> bool:
    if not isinstance(events, list):
        return False
    return any(_event_has_self_repair_marker(event) for event in events)


def _event_has_self_repair_marker(event: object) -> bool:
    if not isinstance(event, Mapping):
        return False
    for key in ("kind", "event", "type", "action"):
        marker = event.get(key)
        if not isinstance(marker, str):
            continue
        normalized = marker.lower()
        if (
            normalized.startswith("repair.")
            or normalized.endswith(".self_repair")
            or ".self_repair." in normalized
        ):
            return True
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        return _event_has_self_repair_marker(payload)
    return False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
