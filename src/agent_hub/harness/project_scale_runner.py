from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.cookies import SimpleCookie
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
_REPAIR_CONTEXT_MAX_FILES = 24
_REPAIR_CONTEXT_MAX_SNIPPET_CHARS = 700
_REPAIR_CONTEXT_EXTENSIONS = frozenset(
    {
        ".cjs",
        ".css",
        ".html",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".mjs",
        ".mts",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
    }
)
_REPAIR_CONTEXT_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./\\-])([A-Za-z0-9_.@+:/\\-]+\.(?:cjs|css|html|js|json|jsx|md|mjs|mts|ts|tsx|yaml|yml))(?![A-Za-z0-9_./\\-])"
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
_DEFAULT_GENERATED_PROJECT_NPM_REGISTRY = "https://registry.npmmirror.com"
_GENERATED_PROJECT_OUTPUT_TAIL_CHARS = 2_000
_EXECUTE_QUEUE_GRACE_SECONDS = 900.0
_CAPABILITY_DELIVERABLE_REPAIR_SOFT_ATTEMPTS = 3
_CAPABILITY_DELIVERABLE_REPAIR_MAX_BY_SCALE = {
    "small": 5,
    "medium": 6,
    "large": 8,
    "ultra": 10,
}
_FIXTURE_DELIVERABLE_REPAIR_ATTEMPTS = 1
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
_AUTO_MODE_BY_PROJECT_SCALE = {
    "small": "dispatch",
    "medium": "dispatch",
    "large": "hybrid",
    "ultra": "hybrid",
}
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
    observed_mode: str | None = None
    final_observed_mode: str | None = None
    requested_mode: str | None = None
    route_reason: str | None = None
    mode_source: str | None = None
    effective_scale: str | None = None
    artifact_origin: str | None = None
    workspace_bundle_source: str | None = None
    participant_agent_ids: tuple[str, ...] = ()
    participant_event_kinds: tuple[str, ...] = ()
    participant_event_count: int = 0
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
        if _case_requires_multi_agent_participation(self.case_id):
            required = (*required, "multi_agent_participation")
        if _case_requires_plugin_contract(self.case_id):
            required = (*required, "plugin_contract")
        if _case_requires_self_repair_trace(self.case_id):
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
            "observed_mode": self.observed_mode,
            "initial_observed_mode": self.observed_mode,
            "final_observed_mode": self.final_observed_mode or self.observed_mode,
            "requested_mode": self.requested_mode,
            "route_reason": self.route_reason,
            "mode_source": self.mode_source,
            "effective_scale": self.effective_scale,
            "artifact_origin": self.artifact_origin,
            "workspace_bundle_source": self.workspace_bundle_source,
            "participant_agent_ids": list(self.participant_agent_ids),
            "participant_event_kinds": list(self.participant_event_kinds),
            "participant_event_count": self.participant_event_count,
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
        self._cookies: dict[str, str] = {}

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
        if self._cookies:
            headers = dict(headers)
            headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in sorted(self._cookies.items())
            )
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._timeout) as response:
                response_headers = getattr(response, "headers", None)
                if response_headers is not None:
                    self._capture_response_cookies(
                        response_headers.get_all("Set-Cookie") or ()
                    )
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

    def _capture_response_cookies(self, values: Sequence[str]) -> None:
        for value in values:
            parsed = SimpleCookie()
            parsed.load(value)
            for name, morsel in parsed.items():
                if not morsel.value or morsel["max-age"] == "0":
                    self._cookies.pop(name, None)
                else:
                    self._cookies[name] = morsel.value

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
    workspace_bundle_source: str | None
    artifact_origin: str | None


@dataclass(frozen=True, slots=True)
class _DownloadedWorkspaceBundle:
    content: bytes
    artifact_id: str | None


@dataclass(frozen=True, slots=True)
class _EvidenceCheck:
    passed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _DeliverableRepairProgress:
    deficits: tuple[str, ...]
    failure_fingerprints: tuple[str, ...]
    validation_stage: int = 0
    progress_metrics: tuple[tuple[str, int, int], ...] = ()

    @property
    def signature(
        self,
    ) -> tuple[
        tuple[str, ...],
        tuple[str, ...],
        int,
        tuple[tuple[str, int, int], ...],
    ]:
        return (
            self.deficits,
            self.failure_fingerprints,
            self.validation_stage,
            self.progress_metrics,
        )


def _report_progress(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


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
    progress: Callable[[str], None] | None = None,
    auto_approve_capability_requests: bool = False,
) -> ProjectScaleExecutionReport:
    validate_generated_project = validate_generated_project or plan.benchmark_kind == "capability"
    results: list[ProjectScaleCaseResult] = []
    for index, run_request in enumerate(plan.requests):
        case_label = f"case {index + 1}/{plan.case_count} {run_request.case_id}"
        request_body = _scoped_execution_body(run_request.body, execution_id=execution_id)
        evidence = {
            "run_details": False,
            "run_events": False,
            "terminal_status": False,
            "final_artifacts": False,
            "deliverable_quality": False,
            "agent_standard_verification": False,
            "discussion_trace": False,
            "multi_agent_participation": False,
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
        observed_mode: str | None = None
        final_observed_mode: str | None = None
        requested_mode = _string_value(request_body.get("mode"))
        route_reason: str | None = None
        mode_source: str | None = None
        effective_scale: str | None = None
        artifact_origin: str | None = None
        workspace_bundle_source: str | None = None
        participant_agent_ids: set[str] = set()
        participant_event_kinds: list[str] = []
        multi_agent_contract_reasons: tuple[str, ...] = ()
        case_deadline = time.monotonic() + max(wait_seconds, 0)
        try:
            _report_progress(progress, f"{case_label}: submitting run")
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
            mode_decision_context = dict(response)
            mode_choice_response = _choose_auto_mode_if_waiting(
                client,
                run_id=run_id,
                case_id=run_request.case_id,
                requested_body=request_body,
                current=response,
                fallback=mode_decision_context,
            )
            if mode_choice_response is not None:
                response = mode_choice_response
            response_reason, response_source, response_scale, response_requested = (
                _public_route_evidence(response, requested_body=request_body)
            )
            route_reason = response_reason or route_reason
            mode_source = response_source or mode_source
            effective_scale = response_scale or effective_scale
            requested_mode = response_requested or requested_mode
            status = _string_value(response.get("status"))
            observed_mode = _execution_mode(response) or observed_mode
            final_observed_mode = _execution_mode(response) or final_observed_mode
            _validate_run_submission_scope(response, request_body)
            _extend_unique(
                errors,
                _validate_mode_control(
                    response,
                    requested_body=request_body,
                    validation_focus=run_request.validation_focus,
                ),
            )
            approval_status = _approve_project_preflight_run(
                client,
                run_id=run_id,
                response=response,
                case_id=run_request.case_id,
                evidence=evidence,
                required=request_body.get("mode") != "auto",
            )
            status = approval_status or status

            _report_progress(progress, f"{case_label}: observing run {run_id}")
            observation = _collect_run_observation(
                client,
                run_id=run_id,
                body=request_body,
                wait_seconds=_remaining_wait_seconds(case_deadline, wait_seconds),
                poll_interval_seconds=poll_interval_seconds,
                current_status=status,
                evidence=evidence,
                errors=errors,
                case_id=run_request.case_id,
                mode_decision_context=mode_decision_context,
                defer_artifacts_until_terminal=plan.benchmark_kind == "capability",
                auto_approve_capability_requests=auto_approve_capability_requests,
            )
            status = observation.status
            observed_mode = observed_mode or _execution_mode(observation.details)
            final_observed_mode = _execution_mode(observation.details) or final_observed_mode
            observed_participants, observed_event_kinds = _multi_agent_participation(
                observation.events
            )
            participant_agent_ids.update(observed_participants)
            participant_event_kinds.extend(observed_event_kinds)
            multi_agent_contract_reasons = _multi_agent_contract_reasons(observation.events)
            evidence["multi_agent_participation"] = _multi_agent_evidence_passes(
                run_request.case_id,
                participant_agent_ids,
                participant_event_kinds,
                multi_agent_contract_reasons,
            )
            workspace_bundle_source = observation.workspace_bundle_source
            artifact_origin = observation.artifact_origin
            observed_reason, observed_source, observed_scale, observed_requested = (
                _public_route_evidence(observation.details, requested_body=request_body)
            )
            route_reason = observed_reason or route_reason
            mode_source = observed_source or mode_source
            effective_scale = observed_scale or effective_scale
            requested_mode = observed_requested or requested_mode
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
                    if not _has_remaining_repair_wait_budget(case_deadline, wait_seconds):
                        _extend_unique(
                            errors,
                            (
                                "self_repair: wait budget exhausted before repair run could be observed",
                            ),
                        )
                    else:
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
                        final_observed_mode = (
                            _execution_mode(repair_response) or final_observed_mode
                        )
                        evidence["deliverable_repair_trace"] = True
                        self_repair_observation = _collect_run_observation(
                            client,
                            run_id=run_id,
                            body=request_body,
                            wait_seconds=_remaining_wait_seconds(case_deadline, wait_seconds),
                            poll_interval_seconds=poll_interval_seconds,
                            current_status=status,
                            evidence=evidence,
                            errors=errors,
                            case_id=run_request.case_id,
                            mode_decision_context=mode_decision_context,
                            defer_artifacts_until_terminal=plan.benchmark_kind == "capability",
                            auto_approve_capability_requests=auto_approve_capability_requests,
                        )
                        observation = self_repair_observation
                        status = self_repair_observation.status
                        final_observed_mode = (
                            _execution_mode(self_repair_observation.details)
                            or final_observed_mode
                        )
                        repaired_participants, repaired_event_kinds = _multi_agent_participation(
                            self_repair_observation.events
                        )
                        participant_agent_ids = set(repaired_participants)
                        participant_event_kinds = list(repaired_event_kinds)
                        multi_agent_contract_reasons = _multi_agent_contract_reasons(
                            self_repair_observation.events
                        )
                        evidence["multi_agent_participation"] = _multi_agent_evidence_passes(
                            run_request.case_id,
                            participant_agent_ids,
                            participant_event_kinds,
                            multi_agent_contract_reasons,
                        )
                        workspace_bundle_source = self_repair_observation.workspace_bundle_source
                        artifact_origin = self_repair_observation.artifact_origin
                        repaired_reason, repaired_source, repaired_scale, repaired_requested = (
                            _public_route_evidence(
                                self_repair_observation.details,
                                requested_body=request_body,
                            )
                        )
                        route_reason = repaired_reason or route_reason
                        mode_source = repaired_source or mode_source
                        effective_scale = repaired_scale or effective_scale
                        requested_mode = repaired_requested or requested_mode
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
                _report_progress(progress, f"{case_label}: validating deliverable")
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
            if evidence["workspace_bundle"]:
                _drop_recovered_workspace_bundle_errors(errors)
            deliverable_repair_attempts = 0
            current_workspace_bundle = observation.workspace_bundle
            current_observation_events = observation.events
            max_deliverable_repair_attempts = _deliverable_repair_attempt_limit(
                run_request.case_id,
                benchmark_kind=plan.benchmark_kind,
            )
            soft_deliverable_repair_attempts = (
                _CAPABILITY_DELIVERABLE_REPAIR_SOFT_ATTEMPTS
                if plan.benchmark_kind == "capability"
                else _FIXTURE_DELIVERABLE_REPAIR_ATTEMPTS
            )
            repair_progress_state = _deliverable_repair_progress_state(
                evidence,
                case_id=run_request.case_id,
                generated_project_validation=generated_project_validation,
                failure_reasons=_deliverable_repair_failure_reasons(
                    deliverable_quality=deliverable_quality,
                    agent_standard_verification=agent_standard_verification,
                    discussion_trace=discussion_trace,
                    plugin_contract=plugin_contract,
                    evidence=evidence,
                    case_id=run_request.case_id,
                    multi_agent_contract_reasons=multi_agent_contract_reasons,
                    generated_project_validation=generated_project_validation,
                ),
            )
            deliverable_repair_safety_limit = max_deliverable_repair_attempts
            if plan.benchmark_kind == "capability":
                deliverable_repair_safety_limit = _deliverable_repair_safety_limit(
                    run_request.case_id,
                    evidence=evidence,
                )
            seen_repair_progress_signatures = {repair_progress_state.signature}
            repair_progress_observed = True
            if not _generated_project_validation_is_repairable(
                generated_project_validation
            ):
                max_deliverable_repair_attempts = 0
            while (
                deliverable_repair_attempts < max_deliverable_repair_attempts
                and _should_attempt_deliverable_repair(
                    status=status,
                    evidence=evidence,
                    case_id=run_request.case_id,
                    benchmark_kind=plan.benchmark_kind,
                )
            ):
                repair_mode = _deliverable_repair_mode(
                    request_body,
                    effective_mode=final_observed_mode,
                    status=status,
                    events=current_observation_events,
                )
                if (
                    deliverable_repair_attempts >= soft_deliverable_repair_attempts
                    and not repair_progress_observed
                    and repair_mode == final_observed_mode
                ):
                    break
                if deliverable_repair_attempts > 0 and not _has_followup_deliverable_repair_reason(
                    evidence,
                    case_id=run_request.case_id,
                ):
                    break
                case_deadline = _extend_repair_deadline(
                    case_deadline,
                    configured_wait_seconds=wait_seconds,
                    request_body=request_body,
                    benchmark_kind=plan.benchmark_kind,
                    generated_project_timeout_seconds=generated_project_timeout_seconds,
                    generated_project_command_count=len(
                        generated_project_commands or _DEFAULT_GENERATED_PROJECT_COMMANDS
                    ),
                )
                if not _has_remaining_repair_wait_budget(case_deadline, wait_seconds):
                    _extend_unique(
                        errors,
                        (
                            "deliverable_repair: wait budget exhausted before follow-up repair could be observed",
                        ),
                    )
                    break
                if run_id is not None and not _is_terminal_status(status):
                    try:
                        cleanup = client.request_json("POST", f"/api/v1/runs/{quote(run_id)}/cancel")
                        evidence["cleanup_cancel"] = isinstance(cleanup, dict)
                        if isinstance(cleanup, dict):
                            status = _string_value(cleanup.get("status")) or status
                    except Exception as error:  # noqa: BLE001 - repair can still supersede it.
                        errors.append(f"cleanup_cancel: {error}")
                deliverable_repair_attempts += 1
                _report_progress(
                    progress,
                    f"{case_label}: submitting deliverable repair {deliverable_repair_attempts}",
                )
                repair_response = client.request_json(
                    "POST",
                    "/api/v1/runs",
                    body=_deliverable_repair_body(
                        request_body,
                        run_request.case_id,
                        benchmark_kind=plan.benchmark_kind,
                        effective_mode=repair_mode,
                        source_workspace_bundle=current_workspace_bundle,
                        failed_reasons=(
                            *deliverable_quality.reasons,
                            *agent_standard_verification.reasons,
                            *discussion_trace.reasons,
                            *plugin_contract.reasons,
                            *_multi_agent_participation_reasons(
                                evidence,
                                case_id=run_request.case_id,
                                contract_reasons=multi_agent_contract_reasons,
                            ),
                            *generated_project_validation.reasons,
                            *_self_repair_trace_reasons(
                                evidence,
                                case_id=run_request.case_id,
                            ),
                        ),
                    ),
                    idempotency_key=_deliverable_repair_idempotency_key(
                        run_request.case_id,
                        index,
                        execution_id=execution_id,
                        repair_attempt=deliverable_repair_attempts,
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
                final_observed_mode = _execution_mode(repair_response) or final_observed_mode
                approval_status = _approve_project_preflight_run(
                    client,
                    run_id=run_id,
                    response=repair_response,
                    case_id=run_request.case_id,
                    evidence=evidence,
                    required=False,
                )
                status = approval_status or status
                _report_progress(
                    progress,
                    f"{case_label}: observing repair run {run_id}",
                )
                repair_observation = _collect_run_observation(
                    client,
                    run_id=run_id,
                    body=request_body,
                    wait_seconds=_remaining_wait_seconds(case_deadline, wait_seconds),
                    poll_interval_seconds=poll_interval_seconds,
                    current_status=status,
                    evidence=evidence,
                    errors=errors,
                    case_id=run_request.case_id,
                    mode_decision_context=mode_decision_context,
                    defer_artifacts_until_terminal=plan.benchmark_kind == "capability",
                    auto_approve_capability_requests=auto_approve_capability_requests,
                )
                status = repair_observation.status
                current_observation_events = repair_observation.events
                final_observed_mode = (
                    _execution_mode(repair_observation.details) or final_observed_mode
                )
                repaired_participants, repaired_event_kinds = _multi_agent_participation(
                    repair_observation.events
                )
                participant_agent_ids = set(repaired_participants)
                participant_event_kinds = list(repaired_event_kinds)
                multi_agent_contract_reasons = _multi_agent_contract_reasons(
                    repair_observation.events
                )
                evidence["multi_agent_participation"] = _multi_agent_evidence_passes(
                    run_request.case_id,
                    participant_agent_ids,
                    participant_event_kinds,
                    multi_agent_contract_reasons,
                )
                workspace_bundle_source = repair_observation.workspace_bundle_source
                artifact_origin = repair_observation.artifact_origin
                repaired_reason, repaired_source, repaired_scale, repaired_requested = (
                    _public_route_evidence(
                        repair_observation.details,
                        requested_body=request_body,
                    )
                )
                route_reason = repaired_reason or route_reason
                mode_source = repaired_source or mode_source
                effective_scale = repaired_scale or effective_scale
                requested_mode = repaired_requested or requested_mode
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
                    or (
                        _case_requires_self_repair_trace(run_request.case_id)
                        and evidence["deliverable_repair_trace"]
                    )
                )
                repair_workspace_bundle = _merged_workspace_bundle(
                    current_workspace_bundle,
                    repair_observation.workspace_bundle,
                )
                current_workspace_bundle = repair_workspace_bundle
                deliverable_quality = _evaluate_deliverable_quality(
                    repair_observation.details,
                    repair_observation.events,
                    repair_workspace_bundle,
                )
                evidence["deliverable_quality"] = deliverable_quality.passed
                agent_standard_verification = _evaluate_agent_standard_verification(
                    repair_observation.details,
                    repair_observation.events,
                    repair_workspace_bundle,
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
                    _report_progress(
                        progress,
                        (
                            f"{case_label}: validating repaired deliverable "
                            f"{deliverable_repair_attempts}"
                        ),
                    )
                    generated_project_validation = _validate_generated_project_bundle(
                        repair_workspace_bundle,
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
                            repair_workspace_bundle, generated_project_validation
                        )
                        evidence["deliverable_quality"] = deliverable_quality.passed
                    if not _generated_project_validation_is_repairable(
                        generated_project_validation
                    ):
                        break
                next_progress_state = _deliverable_repair_progress_state(
                    evidence,
                    case_id=run_request.case_id,
                    generated_project_validation=generated_project_validation,
                    failure_reasons=_deliverable_repair_failure_reasons(
                        deliverable_quality=deliverable_quality,
                        agent_standard_verification=agent_standard_verification,
                        discussion_trace=discussion_trace,
                        plugin_contract=plugin_contract,
                        evidence=evidence,
                        case_id=run_request.case_id,
                        multi_agent_contract_reasons=multi_agent_contract_reasons,
                        generated_project_validation=generated_project_validation,
                    ),
                )
                repair_progress_observed = _deliverable_repair_made_progress(
                    repair_progress_state,
                    next_progress_state,
                    seen_signatures=seen_repair_progress_signatures,
                )
                seen_repair_progress_signatures.add(next_progress_state.signature)
                repair_progress_state = next_progress_state
                if (
                    plan.benchmark_kind == "capability"
                    and repair_progress_observed
                    and deliverable_repair_attempts >= max_deliverable_repair_attempts
                    and max_deliverable_repair_attempts < deliverable_repair_safety_limit
                ):
                    max_deliverable_repair_attempts += 1
                if evidence["workspace_bundle"]:
                    _drop_recovered_workspace_bundle_errors(errors)
            if (
                plan.benchmark_kind == "capability"
                and deliverable_repair_attempts >= deliverable_repair_safety_limit
                and _should_attempt_deliverable_repair(
                    status=status,
                    evidence=evidence,
                    case_id=run_request.case_id,
                    benchmark_kind=plan.benchmark_kind,
                )
            ):
                _extend_unique(
                    errors,
                    (
                        f"deliverable_repair: dynamic safety limit exhausted after {deliverable_repair_attempts} attempts",
                    ),
                )
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
                        _case_requires_multi_agent_participation(run_request.case_id)
                        and not evidence["multi_agent_participation"]
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
                if (
                    _case_requires_multi_agent_participation(run_request.case_id)
                    and not evidence["multi_agent_participation"]
                ):
                    errors.extend(
                        _multi_agent_participation_reasons(
                            evidence,
                            case_id=run_request.case_id,
                            contract_reasons=multi_agent_contract_reasons,
                        )
                    )
                if validate_generated_project and not evidence["generated_project_validation"]:
                    errors.extend(generated_project_validation.reasons)
            if run_id is not None and not _is_terminal_status(status):
                try:
                    status = _refresh_run_terminal_status(
                        client,
                        run_id=run_id,
                        current_status=status,
                        evidence=evidence,
                    )
                except Exception as error:  # noqa: BLE001 - cleanup can still cancel stale runs.
                    errors.append(f"terminal_status_refresh: {error}")
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
                        if isinstance(cleanup, dict):
                            status = _string_value(cleanup.get("status")) or status
                    except Exception as error:  # noqa: BLE001 - cleanup failure is evidence.
                        errors.append(f"cleanup_cancel: {error}")
        if evidence["terminal_status"] and status != "completed":
            errors.append(f"terminal_status: {status or 'unknown'}")
        result = ProjectScaleCaseResult(
            case_id=run_request.case_id,
            run_id=run_id,
            status=status,
            evidence=evidence,
            observed_mode=observed_mode,
            final_observed_mode=final_observed_mode or observed_mode,
            requested_mode=requested_mode,
            route_reason=route_reason,
            mode_source=mode_source,
            effective_scale=effective_scale,
            artifact_origin=artifact_origin,
            workspace_bundle_source=workspace_bundle_source,
            participant_agent_ids=tuple(sorted(participant_agent_ids)),
            participant_event_kinds=tuple(participant_event_kinds),
            participant_event_count=len(participant_event_kinds),
            validation_focus=run_request.validation_focus,
            errors=tuple(errors),
        )
        _report_progress(
            progress,
            f"{case_label}: completed status={status or 'unknown'} ok={str(result.ok).lower()}",
        )
        results.append(result)
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


def _effective_execute_wait_seconds(
    plan: ProjectScaleRunPlan,
    configured_wait_seconds: float,
    *,
    generated_project_timeout_seconds: float = 0.0,
    generated_project_command_count: int = len(_DEFAULT_GENERATED_PROJECT_COMMANDS),
) -> float:
    wait_seconds = max(configured_wait_seconds, 0.0)
    runtime_timeouts: list[float] = []
    for request in plan.requests:
        value = request.body.get("runtime_timeout_seconds")
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            runtime_timeouts.append(float(value))
    if runtime_timeouts:
        runtime_wait_seconds = max(runtime_timeouts)
        validation_command_count = max(generated_project_command_count, 0) + (
            1 if plan.benchmark_kind == "capability" else 0
        )
        validation_budget_seconds = (
            max(generated_project_timeout_seconds, 0.0)
            * validation_command_count
        )
        wait_seconds = max(
            wait_seconds,
            runtime_wait_seconds
            + validation_budget_seconds
            + _EXECUTE_QUEUE_GRACE_SECONDS,
        )
    return wait_seconds


def _extend_repair_deadline(
    deadline: float,
    *,
    configured_wait_seconds: float,
    request_body: Mapping[str, object],
    benchmark_kind: ProjectScaleBenchmarkKind,
    generated_project_timeout_seconds: float,
    generated_project_command_count: int,
) -> float:
    if configured_wait_seconds <= 0:
        return deadline
    runtime_timeout = request_body.get("runtime_timeout_seconds")
    runtime_budget = (
        float(runtime_timeout)
        if isinstance(runtime_timeout, int | float)
        and not isinstance(runtime_timeout, bool)
        and runtime_timeout > 0
        else 0.0
    )
    validation_count = max(generated_project_command_count, 0) + (
        1 if benchmark_kind == "capability" else 0
    )
    validation_budget = max(generated_project_timeout_seconds, 0.0) * validation_count
    return (
        max(deadline, time.monotonic())
        + _EXECUTE_QUEUE_GRACE_SECONDS
        + runtime_budget
        + validation_budget
    )


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
            wait_seconds=_effective_execute_wait_seconds(
                plan,
                args.wait_seconds,
                generated_project_timeout_seconds=args.artifact_build_timeout,
                generated_project_command_count=len(_DEFAULT_GENERATED_PROJECT_COMMANDS),
            ),
            poll_interval_seconds=args.poll_interval,
            execution_id=args.execution_id or _default_execution_id(),
            validate_generated_project=args.validate_generated_project,
            generated_project_timeout_seconds=args.artifact_build_timeout,
            progress=lambda message: print(
                f"project-scale progress: {message}",
                file=sys.stderr,
                flush=True,
            ),
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
) -> _DownloadedWorkspaceBundle | None:
    for path in _artifact_download_paths(run_id=run_id, details=details, events=events):
        try:
            raw = client.request_bytes("GET", path)
        except Exception:  # noqa: BLE001 - try remaining artifacts before failing the bundle.
            raw = None
        if raw is None:
            continue
        bundle = _workspace_bundle_from_downloaded_artifact(raw)
        if bundle is not None:
            return _DownloadedWorkspaceBundle(
                content=bundle,
                artifact_id=_artifact_id_from_download_path(path),
            )
    return None


def _artifact_id_from_download_path(path: str) -> str | None:
    match = re.search(r"/artifacts/([^/]+)/download$", path)
    return match.group(1) if match is not None else None


def _workspace_bundle_from_downloaded_artifact(raw: bytes) -> bytes | None:
    if zipfile.is_zipfile(BytesIO(raw)):
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return _embedded_workspace_bundle_from_text(text)


def _merged_workspace_bundle(base: bytes | None, patch: bytes | None) -> bytes | None:
    if base is None:
        return patch
    if patch is None:
        return base
    base_files = _workspace_bundle_file_bytes(base)
    patch_files = _workspace_bundle_file_bytes(patch)
    if base_files is None:
        return patch
    if patch_files is None:
        return base
    base_files.update(patch_files)
    return _workspace_bundle_from_file_bytes(base_files)


def _workspace_bundle_file_bytes(workspace_bundle: bytes) -> dict[str, bytes] | None:
    try:
        with zipfile.ZipFile(BytesIO(workspace_bundle)) as archive:
            files: dict[str, bytes] = {}
            for info in archive.infolist():
                if info.is_dir():
                    continue
                path = str(_safe_zip_member_path(info.filename))
                files[path] = archive.read(info.filename)
            return files
    except (OSError, RuntimeError, zipfile.BadZipFile):
        return None


def _workspace_bundle_from_file_bytes(files: Mapping[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.writestr(path, files[path])
    return buffer.getvalue()


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
    if any(":" in part for part in path.parts):
        raise RuntimeError(f"workspace bundle has unsafe path: {value}")
    if path.parts[0].strip().upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
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
    if command[0].casefold() in {"node", "npm", "npm.cmd", "npx", "npx.cmd"} and not (
        _generated_project_validation_is_isolated()
    ):
        return (
            "generated_project_validation: isolated systemd validator is required "
            "for generated npm/node commands"
        )
    validation_home = cwd / ".agent-hub-validation-home"
    validation_home.mkdir(parents=True, exist_ok=True)
    safe_env = _generated_project_command_env()
    safe_env.update(
        {
            "HOME": str(validation_home),
            "USERPROFILE": str(validation_home),
            "NPM_CONFIG_CACHE": str(validation_home / ".npm"),
        }
    )
    executable = shutil.which(command[0], path=safe_env.get("PATH")) or command[0]
    resolved_command = [executable, *command[1:]]
    try:
        completed = subprocess.run(
            resolved_command,
            cwd=cwd,
            env=safe_env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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


def _generated_project_validation_is_isolated() -> bool:
    if os.name != "posix":
        return False
    if os.environ.get("AGENT_HUB_GENERATED_PROJECT_VALIDATION_SANDBOX") != "systemd":
        return False
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False
    return "agent-hub-acceptance-" in cgroup


def _generated_project_validation_is_repairable(result: _EvidenceCheck) -> bool:
    if result.passed:
        return True
    return not any(
        reason.startswith(
            "generated_project_validation: isolated systemd validator is required"
        )
        for reason in result.reasons
    )


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
    env = {key: value for key, value in os.environ.items() if key.upper() in keep_keys}
    registry = (
        os.environ.get("AGENT_HUB_GENERATED_PROJECT_NPM_REGISTRY")
        or env.get("NPM_CONFIG_REGISTRY")
        or _DEFAULT_GENERATED_PROJECT_NPM_REGISTRY
    )
    env["NPM_CONFIG_REGISTRY"] = registry
    env.setdefault("NPM_CONFIG_AUDIT", "false")
    env.setdefault("NPM_CONFIG_FUND", "false")
    env.setdefault("NPM_CONFIG_UPDATE_NOTIFIER", "false")
    env.setdefault("NPM_CONFIG_FETCH_RETRIES", "2")
    env.setdefault("NPM_CONFIG_FETCH_RETRY_MINTIMEOUT", "1000")
    env.setdefault("NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT", "10000")
    return env


def _format_command(command: Sequence[str]) -> str:
    return " ".join(command)


def _remaining_wait_seconds(deadline: float, configured_wait_seconds: float) -> float:
    if configured_wait_seconds <= 0:
        return 0
    return max(deadline - time.monotonic(), 0)


def _has_remaining_repair_wait_budget(deadline: float, configured_wait_seconds: float) -> bool:
    return configured_wait_seconds <= 0 or _remaining_wait_seconds(
        deadline,
        configured_wait_seconds,
    ) > 0


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
    case_id: str,
    mode_decision_context: Mapping[str, object],
    defer_artifacts_until_terminal: bool = False,
    auto_approve_capability_requests: bool = False,
) -> _RunObservation:
    status = current_status
    details: dict[str, object] | None = None
    events: list[object] | None = None
    workspace_bundle: bytes | None = None
    workspace_bundle_source: str | None = None
    workspace_bundle_artifact_id: str | None = None
    approved_capabilities: set[str] = set()

    deadline = time.monotonic() + max(wait_seconds, 0)
    while True:
        details_response = client.request_json("GET", f"/api/v1/runs/{quote(run_id)}/details")
        evidence["run_details"] = isinstance(details_response, dict)
        if isinstance(details_response, dict):
            details = details_response
            _validate_run_details_scope(details, run_id)
            status = _string_value(details.get("status")) or status
            mode_choice_response = _choose_auto_mode_if_waiting(
                client,
                run_id=run_id,
                case_id=case_id,
                requested_body=body,
                current=details,
                fallback=mode_decision_context,
            )
            if mode_choice_response is not None:
                _validate_run_submission_scope(mode_choice_response, body)
                details = {**details, **mode_choice_response}
                status = _string_value(mode_choice_response.get("status")) or status
                approval_status = _approve_project_preflight_run(
                    client,
                    run_id=run_id,
                    response=mode_choice_response,
                    case_id=case_id,
                    evidence=evidence,
                    required=False,
                )
                status = approval_status or status
                if approval_status is not None:
                    details["status"] = approval_status
            evidence["final_artifacts"] = bool(
                evidence.get("final_artifacts")
            ) or _has_final_artifacts(details)
            if auto_approve_capability_requests:
                approved_status = _approve_pending_capability(
                    client,
                    run_id=run_id,
                    details=details,
                    approved_capabilities=approved_capabilities,
                    evidence=evidence,
                    errors=errors,
                )
                if approved_status is not None:
                    status = approved_status
        if _is_terminal_status(status):
            evidence["terminal_status"] = True
            break
        remaining = deadline - time.monotonic()
        if wait_seconds <= 0 or remaining <= 0:
            break
        if poll_interval_seconds > 0:
            time.sleep(min(poll_interval_seconds, remaining))

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

    if defer_artifacts_until_terminal and not _is_terminal_status(status) and not evidence["final_artifacts"]:
        errors.append(
            f"run_observation: status {status or 'unknown'} before terminal artifact collection"
        )
        return _RunObservation(
            status=status,
            details=details,
            events=events,
            workspace_bundle=None,
            workspace_bundle_source=None,
            artifact_origin=None,
        )

    try:
        workspace_bundle = client.request_bytes("GET", _workspace_bundle_path(body))
        evidence["workspace_bundle"] = True
        workspace_bundle_source = "public_workspace_api"
    except Exception as error:  # noqa: BLE001 - acceptance reports must continue cleanup.
        workspace_bundle = _embedded_workspace_bundle_from_observation(details, events)
        if workspace_bundle is not None:
            workspace_bundle_source = "embedded_bundle"
        downloaded_bundle = _downloaded_workspace_bundle_from_artifacts(
            client,
            run_id=run_id,
            details=details,
            events=events,
        )
        if downloaded_bundle is not None:
            workspace_bundle = downloaded_bundle.content
            workspace_bundle_source = "artifact_download"
            workspace_bundle_artifact_id = downloaded_bundle.artifact_id
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
                        workspace_bundle = downloaded_bundle.content
                        workspace_bundle_source = "artifact_download"
                        workspace_bundle_artifact_id = downloaded_bundle.artifact_id
        if workspace_bundle is not None:
            evidence["workspace_bundle"] = True
        else:
            errors.append(f"workspace_bundle: {error}")

    return _RunObservation(
        status=status,
        details=details,
        events=events,
        workspace_bundle=workspace_bundle,
        workspace_bundle_source=workspace_bundle_source,
        artifact_origin=_artifact_origin(
            details,
            events,
            bundle_source=workspace_bundle_source,
            workspace_bundle=workspace_bundle,
            bundle_artifact_id=workspace_bundle_artifact_id,
        ),
    )


def _choose_auto_mode_if_waiting(
    client: AcceptanceClient,
    *,
    run_id: str,
    case_id: str,
    requested_body: Mapping[str, object],
    current: Mapping[str, object],
    fallback: Mapping[str, object],
) -> dict[str, object] | None:
    if requested_body.get("mode") != "auto" or current.get("status") != "waiting_user_mode":
        return None
    scale, _, _flow = case_id.partition(":")
    selected_mode = _AUTO_MODE_BY_PROJECT_SCALE.get(scale)
    if selected_mode is None:
        return None
    decision_token = current.get("decision_token")
    if not isinstance(decision_token, str) or not decision_token:
        decision_token = fallback.get("decision_token")
    version = current.get("version")
    if not isinstance(version, int) or version <= 0:
        version = fallback.get("version")
    if not isinstance(decision_token, str) or not decision_token:
        return None
    if not isinstance(version, int) or version <= 0:
        return None
    response = client.request_json(
        "POST",
        f"/api/v1/runs/{quote(run_id)}/choose-mode",
        body={
            "mode": selected_mode,
            "decision_token": decision_token,
            "version": version,
        },
    )
    if not isinstance(response, dict):
        raise TypeError("mode choice returned non-object JSON")
    return response


def _refresh_run_terminal_status(
    client: AcceptanceClient,
    *,
    run_id: str,
    current_status: str | None,
    evidence: dict[str, bool],
) -> str | None:
    details_response = client.request_json("GET", f"/api/v1/runs/{quote(run_id)}/details")
    evidence["run_details"] = isinstance(details_response, dict)
    if not isinstance(details_response, dict):
        return current_status
    _validate_run_details_scope(details_response, run_id)
    status = _string_value(details_response.get("status")) or current_status
    if _has_final_artifacts(details_response):
        evidence["final_artifacts"] = True
    if _is_terminal_status(status):
        evidence["terminal_status"] = True
    return status


def _validate_run_submission_scope(response: dict[str, object], body: dict[str, object]) -> None:
    fields = ["project_id", "workspace_session_id"]
    if isinstance(body.get("conversation_id"), str) and body["conversation_id"]:
        fields.append("conversation_id")
    for field in fields:
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


def _execution_mode(response: Mapping[str, object] | None) -> str | None:
    if response is None:
        return None
    mode = response.get("mode")
    if mode in {"direct", "dispatch", "hybrid"}:
        return mode
    return None


def _public_route_evidence(
    response: Mapping[str, object] | None,
    *,
    requested_body: Mapping[str, object],
) -> tuple[str | None, str | None, str | None, str | None]:
    del requested_body
    if response is None:
        return None, None, None, None
    requested_mode = _string_value(response.get("requested_mode"))
    effective_mode = _string_value(response.get("effective_mode"))
    effective_scale = _string_value(response.get("effective_scale"))
    route_reason = _string_value(response.get("route_reason"))
    mode_source = _string_value(response.get("mode_source"))
    actual_mode = _execution_mode(response)
    if effective_mode != actual_mode:
        return None, None, None, None
    if requested_mode not in {"auto", "direct", "dispatch", "discuss", "hybrid"}:
        requested_mode = None
    if effective_scale not in {"small", "medium", "large", "ultra"}:
        effective_scale = None
    return route_reason, mode_source, effective_scale, requested_mode


def _artifact_origin(
    details: Mapping[str, object] | None,
    events: list[object] | None,
    *,
    bundle_source: str | None,
    workspace_bundle: bytes | None,
    bundle_artifact_id: str | None = None,
) -> str | None:
    if bundle_source is None or workspace_bundle is None:
        return None
    bundle_manifest = _workspace_bundle_manifest(workspace_bundle)
    if bundle_manifest is None:
        return None
    candidates: list[Mapping[str, object]] = []
    if details is not None:
        candidates.append(details)
    candidates.extend(event for event in events or () if isinstance(event, Mapping))
    for mapping in candidates:
        if not _mapping_produced_workspace_bundle(
            mapping,
            bundle_source=bundle_source,
            bundle_manifest=bundle_manifest,
            bundle_artifact_id=bundle_artifact_id,
        ):
            continue
        markers = json.dumps(mapping, ensure_ascii=True, sort_keys=True).casefold()
        if "project_scale_artifact_preseed" in markers or "builtin_fixture" in markers:
            return "builtin_fixture"
        if "deterministic_recovery" in markers:
            return "deterministic_recovery"
        explicit = _nested_artifact_origin(mapping)
        if explicit is not None:
            return explicit
        if _mapping_contains_tool(mapping, "workspace.bundle"):
            return "incremental_workspace_delivery"
    return None


def _workspace_bundle_manifest(bundle: bytes) -> dict[str, tuple[int, str]] | None:
    files = _workspace_bundle_file_bytes(bundle)
    if files is None or not files:
        return None
    return {
        path: (len(content), hashlib.sha256(content).hexdigest())
        for path, content in files.items()
    }


def _mapping_produced_workspace_bundle(
    mapping: Mapping[str, object],
    *,
    bundle_source: str,
    bundle_manifest: Mapping[str, tuple[int, str]],
    bundle_artifact_id: str | None,
) -> bool:
    if bundle_source == "artifact_download":
        return bundle_artifact_id is not None and bundle_artifact_id in _mapping_artifact_ids(mapping)
    if bundle_source == "embedded_bundle":
        for nested in _nested_mappings(mapping):
            embedded = _embedded_workspace_bundle_from_payload(nested)
            if embedded is None:
                continue
            if _workspace_bundle_manifest(embedded) == bundle_manifest:
                return True
        return False
    if bundle_source == "public_workspace_api":
        return any(
            _workspace_files_manifest(nested.get("workspace_files")) == bundle_manifest
            for nested in _nested_mappings(mapping)
        )
    return False


def _nested_mappings(mapping: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    pending: list[Mapping[str, object]] = [mapping]
    found: list[Mapping[str, object]] = []
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        found.append(current)
        for value in current.values():
            if isinstance(value, Mapping):
                pending.append(value)
            elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
                pending.extend(item for item in value if isinstance(item, Mapping))
    return tuple(found)


def _workspace_files_manifest(value: object) -> dict[str, tuple[int, str]] | None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes) or not value:
        return None
    manifest: dict[str, tuple[int, str]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            return None
        path = _string_value(item.get("path")) or _string_value(item.get("relative_path"))
        sha256 = _string_value(item.get("sha256"))
        size = item.get("size_bytes", item.get("size"))
        if path is None or sha256 is None or type(size) is not int:
            return None
        manifest[path] = (size, sha256.casefold())
    return manifest


def _mapping_artifact_ids(mapping: Mapping[str, object]) -> set[str]:
    artifact_ids: set[str] = set()
    for nested in _nested_mappings(mapping):
        for key in ("id", "artifact_id"):
            value = _string_value(nested.get(key))
            if value is not None:
                artifact_ids.add(value)
    return artifact_ids


def _mapping_contains_tool(mapping: Mapping[str, object], tool_name: str) -> bool:
    return any(
        nested.get("tool_name") == tool_name or nested.get("name") == tool_name
        for nested in _nested_mappings(mapping)
    )


def _nested_artifact_origin(mapping: Mapping[str, object]) -> str | None:
    for current in _nested_mappings(mapping):
        value = current.get("artifact_origin")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


_MULTI_AGENT_CANONICAL_ALIASES = {
    "architecture": "architect",
    "architecture_agent": "architect",
    "architect": "architect",
    "implementation": "implementer",
    "implementation_agent": "implementer",
    "implementer": "implementer",
    "test_agent": "tester",
    "tester": "tester",
    "testing_agent": "tester",
    "final_synthesizer": "synthesizer",
    "synthesis_agent": "synthesizer",
    "synthesizer": "synthesizer",
}
_MULTI_AGENT_REQUIRED_IDS = ("architect", "implementer", "tester", "synthesizer")


def _normalized_agent_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")
    if not normalized:
        return None
    return _MULTI_AGENT_CANONICAL_ALIASES.get(normalized, normalized)


def _event_agent_id(event: Mapping[str, object], *, event_kind: str) -> str | None:
    for mapping in (event, event.get("payload")):
        if not isinstance(mapping, Mapping):
            continue
        normalized = _normalized_agent_id(mapping.get("agent_id"))
        if normalized is not None:
            return normalized
    if event_kind.startswith(("step.", "model.", "review.")):
        return _normalized_agent_id(event.get("actor"))
    return None


def _multi_agent_participation(
    events: Sequence[object] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return distinct participant identities and one kind entry per attributable event."""

    participants: set[str] = set()
    event_kinds: list[str] = []
    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        event_participants: set[str] = set()
        raw_event_kind = event.get("kind")
        event_kind = (
            raw_event_kind.strip()
            if isinstance(raw_event_kind, str) and raw_event_kind.strip()
            else "participant.event"
        )
        normalized_kind = event_kind.casefold()
        runtime_lifecycle_event = normalized_kind.startswith(("step.", "model.", "review."))
        agent_scoped_event = runtime_lifecycle_event or any(
            marker in event_kind.casefold()
            for marker in ("agent", "worker", "dispatch", "participant")
        )
        if not agent_scoped_event:
            continue
        agent_id = _event_agent_id(event, event_kind=normalized_kind)
        if agent_id is not None:
            event_participants.add(agent_id)
        if not event_participants:
            continue
        participants.update(event_participants)
        event_kinds.append(event_kind)
    return tuple(sorted(participants)), tuple(event_kinds)


def _multi_agent_contract_reasons(events: Sequence[object] | None) -> tuple[str, ...]:
    lifecycle: dict[str, dict[str, tuple[int, Mapping[str, object]]]] = {
        agent_id: {} for agent_id in _MULTI_AGENT_REQUIRED_IDS
    }
    for index, event in enumerate(events or ()):
        if not isinstance(event, Mapping):
            continue
        raw_kind = event.get("kind")
        if raw_kind not in {"step.started", "step.completed"}:
            continue
        agent_id = _event_agent_id(event, event_kind=raw_kind)
        if agent_id not in lifecycle:
            continue
        lifecycle[agent_id].setdefault(raw_kind, (index, event))

    reasons: list[str] = []
    for agent_id in _MULTI_AGENT_REQUIRED_IDS:
        for kind in ("step.started", "step.completed"):
            if kind not in lifecycle[agent_id]:
                reasons.append(f"multi_agent_contract: {agent_id} missing {kind}")
        started = lifecycle[agent_id].get("step.started")
        completed = lifecycle[agent_id].get("step.completed")
        if started is not None and completed is not None and started[0] >= completed[0]:
            reasons.append(f"multi_agent_contract: {agent_id} completed before started")

    chain = (("architect", "implementer"), ("implementer", "tester"), ("tester", "synthesizer"))
    for predecessor, successor in chain:
        completed = lifecycle[predecessor].get("step.completed")
        started = lifecycle[successor].get("step.started")
        if completed is not None and started is not None and completed[0] >= started[0]:
            reasons.append(
                f"multi_agent_contract: {successor} must start after {predecessor} completed"
            )

    step_ids = {
        agent_id: _event_step_id(lifecycle[agent_id].get("step.started"))
        for agent_id in _MULTI_AGENT_REQUIRED_IDS
    }
    expected_dependencies = {
        "implementer": ("architect",),
        "tester": ("implementer",),
        "synthesizer": ("architect", "implementer", "tester"),
    }
    for agent_id, predecessors in expected_dependencies.items():
        started = lifecycle[agent_id].get("step.started")
        if started is None:
            continue
        actual = _event_dependencies(started[1])
        expected = {step_ids[item] for item in predecessors if step_ids[item] is not None}
        if not expected <= actual:
            reasons.append(f"multi_agent_contract: {agent_id} missing required dependencies")

    for predecessor, successor in (("architect", "implementer"), ("implementer", "tester")):
        completed = lifecycle[predecessor].get("step.completed")
        started = lifecycle[successor].get("step.started")
        if completed is None or started is None:
            continue
        artifact_id = _event_artifact_id(completed)
        if artifact_id is None:
            reasons.append(f"multi_agent_contract: {predecessor} missing completed artifact")
        elif artifact_id not in _event_input_ids(started[1]):
            reasons.append(
                f"multi_agent_contract: {successor} did not consume {predecessor} artifact"
            )

    synthesis_started = lifecycle["synthesizer"].get("step.started")
    if synthesis_started is not None:
        expected_artifacts: set[str] = set()
        for agent_id in ("architect", "implementer", "tester"):
            artifact_id = _event_artifact_id(lifecycle[agent_id].get("step.completed"))
            if artifact_id is None:
                reasons.append(f"multi_agent_contract: {agent_id} missing completed artifact")
            else:
                expected_artifacts.add(artifact_id)
        if not expected_artifacts <= _event_input_ids(synthesis_started[1]) or len(
            expected_artifacts
        ) != 3:
            reasons.append("multi_agent_contract: synthesizer did not consume predecessor artifacts")
    return tuple(reasons)


def _event_step_id(
    located: tuple[int, Mapping[str, object]] | None,
) -> str | None:
    if located is None:
        return None
    value = located[1].get("step_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _event_dependencies(event: Mapping[str, object]) -> set[str]:
    payload = event.get("payload")
    raw = payload.get("depends_on") if isinstance(payload, Mapping) else None
    if not isinstance(raw, list | tuple):
        return set()
    return {item.strip() for item in raw if isinstance(item, str) and item.strip()}


def _event_artifact_id(
    located: tuple[int, Mapping[str, object]] | None,
) -> str | None:
    if located is None:
        return None
    payload = located[1].get("payload")
    value = payload.get("artifact_id") if isinstance(payload, Mapping) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _event_input_ids(event: Mapping[str, object]) -> set[str]:
    inputs = event.get("inputs")
    if not isinstance(inputs, list | tuple):
        return set()
    values: set[str] = set()
    for item in inputs:
        value = item.get("id") if isinstance(item, Mapping) else item
        if isinstance(value, str) and value.strip():
            values.add(value.strip())
    return values


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
    evidence: dict[str, bool],
    errors: list[str],
) -> str | None:
    if (
        details.get("status") != "waiting_approval"
        or details.get("clarification_reason") != "capability requires approval"
    ):
        return None
    approval = _capability_approval_request(
        client,
        run_id=run_id,
        details=details,
        errors=errors,
    )
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
    response_run_id = response.get("id")
    if response_run_id is not None and str(response_run_id) != run_id:
        errors.append(
            "capability_approval: approve-capability returned mismatched run id "
            f"{response_run_id}"
        )
        return None
    evidence["capability_approval"] = True
    return _string_value(response.get("status"))


def _capability_approval_request(
    client: AcceptanceClient,
    *,
    run_id: str,
    details: dict[str, object],
    errors: list[str],
) -> tuple[str, int] | None:
    approval = _capability_approval_from_mapping(details)
    if approval is not None:
        return approval
    try:
        events_response = client.request_json("GET", f"/api/v1/runs/{quote(run_id)}/events")
    except RuntimeError:
        return None
    events = _run_events_items(events_response)
    if not isinstance(events, list):
        return None
    scope_errors = _validate_run_events_scope(events, run_id)
    if scope_errors:
        _extend_unique(
            errors,
            tuple(f"capability_approval: {error}" for error in scope_errors),
        )
        return None
    return _capability_approval_from_events(events, version=details.get("version"))


def _capability_approval_from_events(
    events: Sequence[object],
    *,
    version: object,
) -> tuple[str, int] | None:
    if not isinstance(version, int) or version <= 0:
        return None
    resolved: set[str] = set()
    for event in reversed(events):
        if not isinstance(event, Mapping):
            continue
        approval_id = event.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            continue
        kind = event.get("kind")
        if kind == "approval.resolved":
            resolved.add(approval_id)
            continue
        if kind == "approval.requested" and approval_id not in resolved:
            return approval_id, version
    return None


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
        scoped["workspace_session_id"] = _safe_workspace_session_token(
            session_id,
            execution_id,
        )
    return scoped


def _deliverable_repair_idempotency_key(
    case_id: str,
    index: int,
    *,
    execution_id: str | None = None,
    repair_attempt: int = 1,
) -> str:
    suffix = "deliverable-repair"
    if repair_attempt > 1:
        suffix = f"{suffix}-{repair_attempt}"
    base = _idempotency_key(case_id, index, execution_id=execution_id)
    base = base[: 90 - len(suffix) - 1].rstrip("-")
    return f"{base}-{suffix}"


def _deliverable_repair_attempt_limit(
    case_id: str,
    *,
    benchmark_kind: str,
) -> int:
    if benchmark_kind != "capability":
        return _FIXTURE_DELIVERABLE_REPAIR_ATTEMPTS
    scale = case_id.partition(":")[0]
    try:
        return _CAPABILITY_DELIVERABLE_REPAIR_MAX_BY_SCALE[scale]
    except KeyError as error:
        raise ValueError(f"unknown project scale in case id: {case_id}") from error


def _deliverable_repair_safety_limit(
    case_id: str,
    *,
    evidence: Mapping[str, bool],
) -> int:
    initial_limit = _deliverable_repair_attempt_limit(
        case_id,
        benchmark_kind="capability",
    )
    deficit_count = len(_deliverable_repair_evidence_deficits(evidence, case_id=case_id))
    return initial_limit + max(3, deficit_count * 2)


def _deliverable_repair_progress_state(
    evidence: Mapping[str, bool],
    *,
    case_id: str,
    generated_project_validation: _EvidenceCheck,
    failure_reasons: Sequence[str] = (),
) -> _DeliverableRepairProgress:
    deficits = _deliverable_repair_evidence_deficits(evidence, case_id=case_id)
    combined_reasons = tuple(dict.fromkeys((*failure_reasons, *generated_project_validation.reasons)))
    return _DeliverableRepairProgress(
        deficits=deficits,
        failure_fingerprints=tuple(
            sorted({_normalize_repair_failure_reason(reason) for reason in combined_reasons})
        ),
        validation_stage=_generated_project_validation_stage(generated_project_validation),
        progress_metrics=_repair_progress_metrics(combined_reasons),
    )


def _generated_project_validation_stage(validation: _EvidenceCheck) -> int:
    if validation.passed:
        return 4
    reasons = " ".join(validation.reasons).casefold()
    command_match = re.search(r"\bcommand=(.*?)(?:\s+output_tail=|$)", reasons)
    command = command_match.group(1).strip(' "\'') if command_match is not None else reasons
    if any(marker in command for marker in ("npm install", "pnpm install", "yarn install")):
        return 0
    if any(marker in command for marker in ("npm run build", "pnpm build", "yarn build")):
        return 1
    if any(marker in command for marker in ("npm test", "npm run test", "pnpm test", "yarn test")):
        return 2
    if any(marker in reasons for marker in ("requirements", "http", "crud", "persistence")):
        return 3
    return 0


def _repair_progress_metrics(reasons: Sequence[str]) -> tuple[tuple[str, int, int], ...]:
    metrics: list[tuple[str, int, int]] = []
    for reason in reasons:
        for match in re.finditer(r"\b(\d+)\s*!==?\s*(\d+)\b", reason):
            actual = int(match.group(1))
            expected = int(match.group(2))
            context = _normalize_repair_failure_reason(reason[max(0, match.start() - 80) : match.start()])
            metrics.append((context, expected, abs(expected - actual)))
    return tuple(sorted(metrics))


def _normalize_repair_failure_reason(reason: str) -> str:
    normalized = reason.casefold()
    normalized = re.sub(
        r"\b\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}(?:\.\d+)?z\b",
        "<timestamp>",
        normalized,
    )
    normalized = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        "<uuid>",
        normalized,
    )
    normalized = re.sub(
        r"(?:[a-z]:[\\/](?:[^\s\"']+[\\/])*(?:tmp|temp)[\\/][^\s\"']*|/tmp/[^\s\"']*)",
        "<temp-path>",
        normalized,
    )
    normalized = re.sub(r"\(\d+,\d+\)", "(<line>,<column>)", normalized)
    normalized = re.sub(r"\b\d+\s*!==?\s*\d+\b", "<actual> != <expected>", normalized)
    normalized = re.sub(r"\b(?:port\s*[=:]?\s*)\d{2,5}\b", "port <number>", normalized)
    return " ".join(normalized.split())


def _deliverable_repair_made_progress(
    previous: _DeliverableRepairProgress,
    current: _DeliverableRepairProgress,
    *,
    seen_signatures: set[
        tuple[
            tuple[str, ...],
            tuple[str, ...],
            int,
            tuple[tuple[str, int, int], ...],
        ]
    ],
) -> bool:
    if current.signature in seen_signatures:
        return False
    previous_deficits = set(previous.deficits)
    current_deficits = set(current.deficits)
    if not current_deficits.issubset(previous_deficits):
        return False
    if current.validation_stage < previous.validation_stage:
        return False
    metric_progress = _repair_metrics_progressed(previous.progress_metrics, current.progress_metrics)
    if metric_progress is False:
        return False
    previous_failures = set(previous.failure_fingerprints)
    current_failures = set(current.failure_fingerprints)
    if current_failures > previous_failures:
        return False
    if current_deficits < previous_deficits:
        return True
    if current.validation_stage > previous.validation_stage:
        return True
    if metric_progress is True:
        return True
    if current_failures < previous_failures:
        return True
    return current_failures != previous_failures


def _repair_metrics_progressed(
    previous: tuple[tuple[str, int, int], ...],
    current: tuple[tuple[str, int, int], ...],
) -> bool | None:
    if not previous or not current or len(previous) != len(current):
        return None
    if tuple((label, expected) for label, expected, _gap in previous) != tuple(
        (label, expected) for label, expected, _gap in current
    ):
        return None
    previous_gaps = tuple(gap for _label, _expected, gap in previous)
    current_gaps = tuple(gap for _label, _expected, gap in current)
    if any(current_gap > previous_gap for previous_gap, current_gap in zip(previous_gaps, current_gaps)):
        return False
    return any(
        current_gap < previous_gap
        for previous_gap, current_gap in zip(previous_gaps, current_gaps)
    )


def _deliverable_repair_body(
    body: dict[str, object],
    case_id: str,
    *,
    failed_reasons: Sequence[str] = (),
    benchmark_kind: str = "fixture",
    effective_mode: str | None = None,
    source_workspace_bundle: bytes | None = None,
) -> dict[str, object]:
    repair_body = dict(body)
    if effective_mode in {"direct", "dispatch", "hybrid"}:
        repair_body["mode"] = effective_mode
    original_message = body.get("message")
    if benchmark_kind == "capability":
        original = original_message if isinstance(original_message, str) else ""
        scale, _, flow = case_id.partition(":")
        guidance = (
            f"Repair same project for case_id={case_id} project_scale={scale} flow={flow}; "
            "preserve requirements. Return full workspace_bundle.files "
            "or ### `path` fences: source/tests/README/PROJECT_REQUIREMENTS.md/"
            "IMPLEMENTATION_PLAN.md/VERIFICATION.md/constraints_reading_evidence.json. "
            "File keys must be safe relative paths, not endpoints/URLs/HTTP methods. "
            "constraints_reading_evidence.json must include read_before_implementation:true, "
            "constraints naming AGENTS.md workspace rules, HANDOFF, and PROJECT_REQUIREMENTS.md, "
            "and skills/rules naming applicable SKILL.md or agent-standard rules. "
        )
        medium_guidance = (
            "For medium CRM repairs, include GET /tenants/:tenant_id/opportunities returning "
            "{items:[...]} and verify created/patched opportunities persist after restart; "
            "POST create and PATCH responses must be the object itself with top-level id, "
            "never {item:...}, {data:...}, or any wrapper. Medium CRM body fields are exact: "
            "accounts {name}; contacts {account_id,name,email}; opportunities "
            "{account_id,name,amount,stage}; PATCH opportunities {stage}; reminders "
            "{contact_id,due_at,note}; stages exactly open, won, lost. "
            "Reference validation order is frozen: resolve account_id/contact_id inside "
            "the URL tenant before validating unrelated fields; a missing or foreign "
            "reference returns 404 NOT_FOUND even if email, due_at, note, amount, or stage "
            "is absent or invalid. "
            "Strict TypeScript must compile: when using Express, type route params "
            "with Request<{tenant_id:string,...}> or an equivalent explicit params type "
            "instead of reading tenant_id from default {} params. "
            "Generated tests must compile: validator helpers that require a field argument "
            "must be called with that field name, or define safe defaults before testing. "
        )
        small_guidance = (
            "For small file-backed task API repairs, use single-flight initialization and "
            "serialized read-modify-write transactions so concurrent creates, "
            "patches, deletes, and restores cannot overwrite each other. Atomic rename alone "
            "does not prevent lost updates; rerun the concurrency and restart-persistence tests. "
        )
        if case_id.startswith("small:"):
            guidance += small_guidance
        if case_id.startswith("medium:"):
            guidance += medium_guidance
        if case_id.endswith(":multi_agent"):
            guidance += (
                "Use normalized agent_id values architect, implementer, tester, and synthesizer for "
                "the Architecture Agent, Implementation Agent, Test Agent, and Synthesis Agent. "
                "Preserve mandatory architecture -> implementation "
                "-> independent test -> synthesis artifact dependencies, emit step.started and "
                "step.completed per agent_id, and record discussion_trace plus explicit handoffs. "
            )
        context = _workspace_repair_context(
            source_workspace_bundle,
            failed_reasons=failed_reasons,
        )
        guidance += (
            "package.json scripts: build, test, start. No ellipses or summaries in files. "
            "Report executed checks only.\n"
        )
        reasons = _format_failed_reasons(failed_reasons)
        prefix = guidance + reasons + "\nOriginal request:\n"
        suffix = f"\n{context}" if context else ""
        max_chars = 2_600 if context else 2_000
        available = max_chars - len(" ".join((prefix + suffix).split())) - 1
        bounded_original = original[: max(available, 0)]
        repair_body["message"] = _bounded_role_planning_task_text(
            prefix + bounded_original + suffix,
            max_chars=max_chars,
        )
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
        "public evidence stays visible. File keys must be safe relative paths, not endpoints."
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
    context = _workspace_repair_context(source_workspace_bundle, failed_reasons=failed_reasons)
    repair_body["message"] = _bounded_role_planning_task_text(
        f"{repair_message}\n{context}" if context else repair_message,
        max_chars=2_600 if context else 2_000,
    )
    repair_body["skip_evolution_proposal"] = True
    return repair_body


def _deliverable_repair_mode(
    body: Mapping[str, object],
    *,
    effective_mode: str | None,
    status: str | None,
    events: Sequence[object] | None,
) -> str | None:
    if effective_mode not in {"direct", "dispatch", "hybrid"}:
        return effective_mode
    if body.get("mode") != "auto" or effective_mode != "dispatch" or status != "failed":
        return effective_mode
    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        payload = event.get("payload")
        error_code = payload.get("error_code") if isinstance(payload, Mapping) else None
        if error_code == "model.structured_output_invalid":
            return "direct"
        if event.get("reason") == "structured output invalid":
            return "direct"
    return effective_mode


def _workspace_repair_context(
    workspace_bundle: bytes | None,
    *,
    failed_reasons: Sequence[str],
) -> str:
    if workspace_bundle is None:
        return ""
    files = _workspace_bundle_file_bytes(workspace_bundle)
    if not files:
        return ""
    paths = sorted(files)
    relevant_paths = _repair_context_relevant_paths(paths, failed_reasons)
    snippets = []
    for path in relevant_paths:
        raw = files.get(path)
        if raw is None:
            continue
        try:
            text = raw.decode("utf-8", errors="replace")
        except AttributeError:
            continue
        snippets.append(f"- {path}: {_compact_repair_snippet(text)}")
    inventory = ", ".join(paths[:_REPAIR_CONTEXT_MAX_FILES])
    if len(paths) > _REPAIR_CONTEXT_MAX_FILES:
        inventory += f", ... (+{len(paths) - _REPAIR_CONTEXT_MAX_FILES} more)"
    hints = _repair_context_failure_hints(failed_reasons)
    parts = [
        "Current workspace context for precise repair:",
        f"Files: {inventory}",
    ]
    if snippets:
        parts.append("Relevant file snippets:")
        parts.extend(snippets)
    if hints:
        parts.append(hints)
    return "\n".join(parts)


def _repair_context_relevant_paths(
    paths: Sequence[str],
    failed_reasons: Sequence[str],
) -> list[str]:
    path_set = set(paths)
    selected: list[str] = []

    def add(path: str) -> None:
        if path in path_set and path not in selected:
            selected.append(path)

    failed_text = "\n".join(failed_reasons)
    for match in _REPAIR_CONTEXT_PATH_RE.finditer(failed_text):
        candidate = re.sub(r"/+", "/", match.group(1).replace("\\", "/"))
        for path in paths:
            normalized_path = path.replace("\\", "/").lstrip("/")
            if candidate == normalized_path or candidate.endswith(f"/{normalized_path}"):
                add(path)
    for path in paths:
        basename = PurePosixPath(path).name.lower()
        if basename in {"package.json", "tsconfig.json"}:
            add(path)
    if "has no exported member" in failed_text or "TS2305" in failed_text:
        for path in paths:
            lowered = path.lower()
            if lowered.endswith(("/types.ts", "/types.tsx")) or lowered in {
                "src/types.ts",
                "src/types.tsx",
                "types.ts",
                "types.tsx",
            }:
                add(path)
    if not selected:
        for path in paths:
            suffix = PurePosixPath(path).suffix.lower()
            if suffix in _REPAIR_CONTEXT_EXTENSIONS:
                add(path)
            if len(selected) >= 4:
                break
    return selected[:8]


def _compact_repair_snippet(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= _REPAIR_CONTEXT_MAX_SNIPPET_CHARS:
        return compact
    return compact[: _REPAIR_CONTEXT_MAX_SNIPPET_CHARS - 4].rstrip() + " ..."


def _repair_context_failure_hints(failed_reasons: Sequence[str]) -> str:
    text = "\n".join(failed_reasons)
    if "TS2305" not in text and "has no exported member" not in text:
        return ""
    symbols = re.findall(r"has no exported member '([^']+)'", text)
    symbol_note = f" Missing export(s): {', '.join(dict.fromkeys(symbols))}." if symbols else ""
    return (
        "TypeScript import/export repair hint: TS2305 means the imported symbol must be "
        "exported by that module, or the import must be corrected/removed."
        f"{symbol_note}"
    )


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


def _safe_workspace_session_token(session_id: str, execution_id: str) -> str:
    token = re.sub(r"[^a-z0-9_-]+", "-", execution_id.casefold())
    token = re.sub(r"[-_]{2,}", "-", token).strip("-_")
    normalized_session = re.sub(r"[^a-z0-9_-]+", "-", session_id.casefold())
    normalized_session = re.sub(r"[-_]{2,}", "-", normalized_session).strip("-_")
    if token and (
        normalized_session == token or normalized_session.endswith(f"-{token}")
    ):
        return normalized_session[:64].rstrip("-_") or "project-scale-run"
    scoped = f"{session_id}-{token or 'run'}"
    scoped = re.sub(r"[^a-z0-9_-]+", "-", scoped.casefold())
    scoped = re.sub(r"[-_]{2,}", "-", scoped).strip("-_")
    return (scoped or "project-scale-run")[:64].rstrip("-_") or "project-scale-run"


def _case_requires_project_preflight(case_id: str) -> bool:
    scale, _, _flow = case_id.partition(":")
    return scale in {"large", "ultra"}


def _case_requires_discussion_trace(case_id: str) -> bool:
    _scale, _, flow = case_id.partition(":")
    return flow in _DISCUSSION_TRACE_FLOWS


def _case_requires_multi_agent_participation(case_id: str) -> bool:
    _scale, _, flow = case_id.partition(":")
    return flow == "multi_agent"


def _case_requires_plugin_contract(case_id: str) -> bool:
    _scale, _, flow = case_id.partition(":")
    return flow == "plugin"


def _approve_project_preflight_run(
    client: AcceptanceClient,
    *,
    run_id: str,
    response: dict[str, object],
    case_id: str,
    evidence: dict[str, bool],
    required: bool,
) -> str | None:
    if not _case_requires_project_preflight(case_id):
        return None
    if response.get("status") != "waiting_approval":
        if required:
            _project_preflight_approval_body(response)
        return None
    approval_body = _project_preflight_approval_body(response)
    approval = client.request_json(
        "POST",
        f"/api/v1/runs/{quote(run_id)}/approve-project-preflight",
        body=approval_body,
    )
    if not isinstance(approval, dict):
        raise TypeError("project preflight approval returned non-object JSON")
    evidence["project_preflight_approval"] = True
    return _string_value(approval.get("status"))


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
        chunks: list[str] = []
        for key, item in value.items():
            if isinstance(key, str):
                chunks.append(key)
            chunks.append(_flatten_present_text(item))
        return " ".join(chunks)
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
    has_final_artifact = evidence.get("final_artifacts") is True
    has_workspace_bundle = evidence.get("workspace_bundle") is True
    has_observable_deliverable = has_final_artifact and has_workspace_bundle
    has_capability_validation_failure = (
        benchmark_kind == "capability"
        and evidence.get("generated_project_validation") is False
    )
    return (
        (status in {"completed", "failed"} or has_observable_deliverable)
        and (has_final_artifact or has_workspace_bundle or has_capability_validation_failure)
        and bool(_deliverable_repair_evidence_deficits(evidence, case_id=case_id))
    )


def _has_followup_deliverable_repair_reason(
    evidence: dict[str, bool],
    *,
    case_id: str,
) -> bool:
    return bool(_deliverable_repair_evidence_deficits(evidence, case_id=case_id))


def _deliverable_repair_evidence_deficits(
    evidence: Mapping[str, bool],
    *,
    case_id: str,
) -> tuple[str, ...]:
    required = [
        "workspace_bundle",
        "deliverable_quality",
        "agent_standard_verification",
    ]
    if _case_requires_discussion_trace(case_id):
        required.append("discussion_trace")
    if _case_requires_plugin_contract(case_id):
        required.append("plugin_contract")
    if _case_requires_multi_agent_participation(case_id):
        required.append("multi_agent_participation")
    if _case_requires_self_repair_trace(case_id):
        required.append("self_repair_trace")
    if "generated_project_validation" in evidence:
        required.append("generated_project_validation")
    if "requirements_validation" in evidence:
        required.append("requirements_validation")
    return tuple(sorted(key for key in required if evidence.get(key) is not True))


def _deliverable_repair_failure_reasons(
    *,
    deliverable_quality: _EvidenceCheck,
    agent_standard_verification: _EvidenceCheck,
    discussion_trace: _EvidenceCheck,
    plugin_contract: _EvidenceCheck,
    evidence: Mapping[str, bool],
    case_id: str,
    multi_agent_contract_reasons: Sequence[str],
    generated_project_validation: _EvidenceCheck,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *deliverable_quality.reasons,
                *agent_standard_verification.reasons,
                *discussion_trace.reasons,
                *plugin_contract.reasons,
                *_multi_agent_participation_reasons(
                    evidence,
                    case_id=case_id,
                    contract_reasons=multi_agent_contract_reasons,
                ),
                *generated_project_validation.reasons,
                *_self_repair_trace_reasons(evidence, case_id=case_id),
            )
        )
    )


def _case_requires_self_repair_trace(case_id: str) -> bool:
    return "self_repair" in case_id or "model_failure" in case_id


def _multi_agent_participation_reasons(
    evidence: Mapping[str, bool],
    *,
    case_id: str,
    contract_reasons: Sequence[str] = (),
) -> tuple[str, ...]:
    if not _case_requires_multi_agent_participation(case_id):
        return ()
    if evidence.get("multi_agent_participation") is True:
        return ()
    if contract_reasons:
        return tuple(contract_reasons)
    return (
        "multi_agent_participation: required four-agent lifecycle evidence is missing",
    )


def _multi_agent_evidence_passes(
    case_id: str,
    participant_agent_ids: set[str],
    participant_event_kinds: Sequence[str],
    contract_reasons: Sequence[str],
) -> bool:
    if _case_requires_multi_agent_participation(case_id):
        return not contract_reasons
    return len(participant_agent_ids) >= 2 and len(participant_event_kinds) >= 2


def _self_repair_trace_reasons(
    evidence: Mapping[str, bool],
    *,
    case_id: str,
) -> tuple[str, ...]:
    if (
        _case_requires_self_repair_trace(case_id)
        and evidence.get("self_repair_trace") is not True
    ):
        return (
            "self_repair_trace: expected explicit fault-injection or repair evidence",
        )
    return ()


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
