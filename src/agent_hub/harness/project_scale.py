from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProjectScaleTier = Literal["small", "medium", "large", "ultra"]
ProjectScaleFlow = Literal[
    "direct",
    "dispatch",
    "hybrid",
    "multi_agent",
    "plugin",
    "model_failure",
    "self_repair",
    "artifact_production",
    "capability_validation",
]
ProjectScaleRunMode = Literal["direct", "dispatch", "hybrid"]
ProjectScaleBenchmarkKind = Literal["fixture", "capability"]

PROJECT_SCALE_TIERS: tuple[ProjectScaleTier, ...] = ("small", "medium", "large", "ultra")
PROJECT_SCALE_FLOW_KINDS: tuple[ProjectScaleFlow, ...] = (
    "direct",
    "dispatch",
    "hybrid",
    "multi_agent",
    "plugin",
    "model_failure",
    "self_repair",
    "artifact_production",
    "capability_validation",
)
PROJECT_SCALE_REQUIRED_EVIDENCE: tuple[str, ...] = (
    "run_details",
    "run_events",
    "workspace_bundle",
    "final_artifacts",
    "deliverable_quality",
    "agent_standard_verification",
    "discussion_trace",
    "project_preflight_approval",
    "self_repair_trace",
    "plugin_contract",
)
PROJECT_SCALE_CLEANUP_ACTIONS: tuple[str, ...] = (
    "cancel_or_archive_probe_runs",
    "delete_workspace",
    "remove_release_packages",
)

_LONG_RUNNING_SCALES = frozenset({"large", "ultra"})
_PREFLIGHT_SCALES = frozenset({"large", "ultra"})
_FAILURE_FLOWS = frozenset({"model_failure", "self_repair"})
_ARTIFACT_FLOWS = frozenset({"artifact_production", "plugin", "multi_agent"})
_CAPABILITY_VALIDATION_FLOWS = frozenset({"capability_validation"})
_FLOW_RUN_MODES: dict[ProjectScaleFlow, ProjectScaleRunMode] = {
    "direct": "direct",
    "dispatch": "dispatch",
    "hybrid": "hybrid",
    "multi_agent": "dispatch",
    "plugin": "dispatch",
    "model_failure": "hybrid",
    "self_repair": "hybrid",
    "artifact_production": "hybrid",
    "capability_validation": "hybrid",
}


@dataclass(frozen=True, slots=True)
class ProjectScaleCase:
    scale: ProjectScaleTier
    flow: ProjectScaleFlow
    requires_bearer_token: bool
    requires_explicit_server_profile: bool
    expected_preflight: bool
    validation_focus: tuple[str, ...]

    @property
    def id(self) -> str:
        return f"{self.scale}:{self.flow}"


@dataclass(frozen=True, slots=True)
class ProjectScaleRunRequest:
    case_id: str
    body: dict[str, object]
    validation_focus: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectScaleRunPlan:
    requests: tuple[ProjectScaleRunRequest, ...]
    required_evidence: tuple[str, ...] = PROJECT_SCALE_REQUIRED_EVIDENCE
    cleanup_actions: tuple[str, ...] = PROJECT_SCALE_CLEANUP_ACTIONS
    dry_run: bool = True
    execute: bool = False
    requires_bearer_token: bool = True
    benchmark_kind: ProjectScaleBenchmarkKind = "fixture"

    @property
    def case_count(self) -> int:
        return len(self.requests)

    def to_payload(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "execute": self.execute,
            "requires_bearer_token": self.requires_bearer_token,
            "benchmark_kind": self.benchmark_kind,
            "capability_verified": False,
            "case_count": self.case_count,
            "required_evidence": list(self.required_evidence),
            "cleanup_actions": list(self.cleanup_actions),
            "requests": [
                {
                    "case_id": request.case_id,
                    "validation_focus": list(request.validation_focus),
                    "body": dict(request.body),
                }
                for request in self.requests
            ],
        }


@dataclass(frozen=True, slots=True)
class ProjectScaleMatrix:
    cases: tuple[ProjectScaleCase, ...]
    required_evidence: tuple[str, ...] = PROJECT_SCALE_REQUIRED_EVIDENCE
    cleanup_actions: tuple[str, ...] = PROJECT_SCALE_CLEANUP_ACTIONS
    requires_isolated_workspace: bool = True

    @classmethod
    def default(cls) -> ProjectScaleMatrix:
        cases = tuple(
            _build_case(scale=scale, flow=flow)
            for scale in PROJECT_SCALE_TIERS
            for flow in PROJECT_SCALE_FLOW_KINDS
        )
        return cls(cases=cases)

    @property
    def scale_tiers(self) -> tuple[ProjectScaleTier, ...]:
        return PROJECT_SCALE_TIERS

    @property
    def flow_kinds(self) -> tuple[ProjectScaleFlow, ...]:
        return PROJECT_SCALE_FLOW_KINDS

    @property
    def case_count(self) -> int:
        return len(self.cases)

    def validate(self) -> None:
        expected = {
            (scale, flow)
            for scale in PROJECT_SCALE_TIERS
            for flow in PROJECT_SCALE_FLOW_KINDS
        }
        actual = {(case.scale, case.flow) for case in self.cases}
        missing = expected - actual
        extra = actual - expected
        if missing:
            raise ValueError(f"project scale matrix is missing cases: {sorted(missing)!r}")
        if extra:
            raise ValueError(f"project scale matrix contains unknown cases: {sorted(extra)!r}")
        if len(actual) != len(self.cases):
            raise ValueError("project scale matrix contains duplicate cases")
        for evidence in PROJECT_SCALE_REQUIRED_EVIDENCE:
            if evidence not in self.required_evidence:
                raise ValueError(f"project scale matrix is missing evidence: {evidence}")
        for action in PROJECT_SCALE_CLEANUP_ACTIONS:
            if action not in self.cleanup_actions:
                raise ValueError(f"project scale matrix is missing cleanup action: {action}")
        if not self.requires_isolated_workspace:
            raise ValueError("project scale matrix must require an isolated workspace")


def describe_project_scale_matrix(matrix: ProjectScaleMatrix | None = None) -> str:
    checked = matrix or ProjectScaleMatrix.default()
    checked.validate()
    scales = ",".join(checked.scale_tiers)
    flows = ",".join(checked.flow_kinds)
    evidence = ",".join(checked.required_evidence)
    cleanup = ",".join(checked.cleanup_actions)
    return (
        f"cases={checked.case_count} scales={scales} flows={flows} "
        f"evidence={evidence} cleanup={cleanup}"
    )


def build_project_scale_run_request(
    case: ProjectScaleCase,
    *,
    benchmark_kind: ProjectScaleBenchmarkKind = "fixture",
) -> ProjectScaleRunRequest:
    mode = _FLOW_RUN_MODES[case.flow]
    session_id = f"project-scale-{case.scale}-{case.flow}"
    body: dict[str, object] = {
        "message": _request_message(case, benchmark_kind=benchmark_kind),
        "mode": mode,
        "project_id": "project-scale-acceptance",
        "workspace_session_id": session_id,
        "sandbox_profile": "workspace_write",
        "requested_permissions": ["workspace.read", "workspace.write", "command.run"],
        "skip_evolution_proposal": True,
        "runtime_timeout_seconds": _runtime_timeout_seconds(case),
    }
    return ProjectScaleRunRequest(case_id=case.id, body=body, validation_focus=case.validation_focus)


def build_project_scale_run_plan(
    *,
    scales: tuple[str, ...] | None = None,
    flows: tuple[str, ...] | None = None,
    execute: bool = False,
    benchmark_kind: ProjectScaleBenchmarkKind = "fixture",
) -> ProjectScaleRunPlan:
    if benchmark_kind not in ("fixture", "capability"):
        raise ValueError(f"unknown project scale benchmark kind: {benchmark_kind}")
    selected_scales = _validated_filter(
        values=scales,
        allowed=PROJECT_SCALE_TIERS,
        label="project scale",
    )
    selected_flows = _validated_filter(
        values=flows,
        allowed=PROJECT_SCALE_FLOW_KINDS,
        label="project scale flow",
    )
    requests = tuple(
        build_project_scale_run_request(case, benchmark_kind=benchmark_kind)
        for case in ProjectScaleMatrix.default().cases
        if case.scale in selected_scales and case.flow in selected_flows
    )
    return ProjectScaleRunPlan(
        requests=requests,
        dry_run=not execute,
        execute=execute,
        benchmark_kind=benchmark_kind,
    )


def _build_case(*, scale: ProjectScaleTier, flow: ProjectScaleFlow) -> ProjectScaleCase:
    focus = [
        "interaction_stability",
        "final_result",
        "deliverable_quality",
        "agent_standard_verification",
    ]
    if scale in _LONG_RUNNING_SCALES:
        focus.append("long_running_control")
    if scale in _PREFLIGHT_SCALES:
        focus.append("project_preflight")
    if flow in _FAILURE_FLOWS:
        focus.extend(("fault_injection", "self_repair"))
    if flow in _ARTIFACT_FLOWS:
        focus.append("artifact_integrity")
    if flow == "plugin":
        focus.extend(("plugin_contract", "capability_matrix", "sandbox_policy", "failure_recovery"))
    if flow in _CAPABILITY_VALIDATION_FLOWS:
        focus.extend(("capability_matrix", "mode_control", "no_silent_downgrade"))
    return ProjectScaleCase(
        scale=scale,
        flow=flow,
        requires_bearer_token=True,
        requires_explicit_server_profile=scale in _LONG_RUNNING_SCALES,
        expected_preflight=scale in _PREFLIGHT_SCALES,
        validation_focus=tuple(dict.fromkeys(focus)),
    )


def _runtime_timeout_seconds(case: ProjectScaleCase) -> int:
    return {
        "small": 900,
        "medium": 1200,
        "large": 1800,
        "ultra": 3600,
    }[case.scale]


def _validated_filter(
    *,
    values: tuple[str, ...] | None,
    allowed: tuple[str, ...],
    label: str,
) -> tuple[str, ...]:
    if values is None:
        return allowed
    selected = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
    unknown = tuple(value for value in selected if value not in allowed)
    if unknown:
        raise ValueError(f"unknown {label}: {', '.join(unknown)}")
    if not selected:
        raise ValueError(f"{label} filter must not be empty")
    return selected


def _request_message(case: ProjectScaleCase, *, benchmark_kind: ProjectScaleBenchmarkKind) -> str:
    if benchmark_kind == "capability":
        return _capability_message(case)
    return _fixture_message(case)


def _fixture_message(case: ProjectScaleCase) -> str:
    scale_label = {
        "small": "small project",
        "medium": "medium project",
        "large": "large project",
        "ultra": "ultra-large project",
    }[case.scale]
    plugin_guidance = (
        " For plugin flow, record plugin_contract evidence covering manifest discovery, "
        "adapter contracts, sandbox and policy boundaries, and failure recovery behavior."
        if case.flow == "plugin"
        else ""
    )
    deliverable_guidance = (
        " Produce the final deliverable as either strict JSON with workspace_bundle.files mapping "
        "safe relative paths to complete file contents, or Markdown file blocks headed exactly "
        "like ### `path/to/file` followed by a fenced code block. Include README or requirements, "
        "source files, tests or build scripts, an implementation plan, and a verification report "
        "with reproducible build, test, and interaction evidence. Avoid credential-like terms and "
        "avoid package, file, variable, or fixture names that contain the sk- prefix so public "
        "evidence stays visible."
    )
    direct_guidance = (
        " For direct flow, do not call tools, do not emit DSML/tool-call syntax, and do not "
        "describe commands as if they were executed."
        if case.flow == "direct"
        else ""
    )
    return (
        f"Project-scale acceptance fixture: build a {scale_label} for scale={case.scale} "
        f"and flow={case.flow}. Read constraints first, keep interaction stable, use the "
        "approved workspace, produce final artifacts, satisfy the requested requirements, "
        "verify build/test/interaction behavior, avoid placeholder or stub-only output, record "
        "deliverable_quality and agent_standard_verification evidence, include a lightweight "
        "implementation plan and verification note in the workspace, and follow Codex/Claude "
        "Code verification standards: read constraints, plan before implementation, verify with "
        "reproducible evidence, and repair root causes instead of silently degrading."
        f"{plugin_guidance}{deliverable_guidance}{direct_guidance}"
    )


def _capability_message(case: ProjectScaleCase) -> str:
    requirements = {
        "small": (
            "Build a TypeScript/Node persistent task management API for a small team. "
            "Use file-backed JSON persistence, expose a documented HTTP interface, and "
            "implement exactly these endpoints: POST /tasks, GET /tasks, PATCH /tasks/:id, "
            "DELETE /tasks/:id, and POST /tasks/:id/restore. npm start must listen on the "
            "PORT environment variable, and DATA_DIR must choose the persistence directory "
            "so an independent black-box test can restart the process and verify tasks persist. "
            "Required response contracts: create returns 201 with {id,title,status,created_at}; "
            "GET /tasks returns 200 with {items:[...]}; PATCH /tasks/:id accepts todo|doing|done "
            "and returns the updated task; DELETE /tasks/:id marks the task deleted; "
            "POST /tasks/:id/restore restores it; missing ids return 404 with "
            "{error:{code,message}}."
        ),
        "medium": (
            "Build a TypeScript/Node tenant-aware CRM-lite service for accounts, contacts, "
            "opportunities, and follow-up reminders. Include validation, tenant isolation, "
            "search/filter endpoints, deterministic seed data, and integration tests that "
            "prove one tenant cannot read or mutate another tenant's records. npm start must "
            "listen on the PORT environment variable, and DATA_DIR must choose the persistence "
            "directory. Required HTTP contract: POST /tenants/:tenant_id/accounts and "
            "GET /tenants/:tenant_id/accounts?search=...; POST /tenants/:tenant_id/contacts "
            "and GET /tenants/:tenant_id/contacts?account_id=...; "
            "POST /tenants/:tenant_id/opportunities and PATCH "
            "/tenants/:tenant_id/opportunities/:id; POST /tenants/:tenant_id/reminders and "
            "GET /tenants/:tenant_id/reminders. Cross-tenant references and missing ids must "
            "return 404 with {error:{code,message}}."
        ),
        "large": (
            "Build a large project: a TypeScript/Node multi-service order operations platform "
            "with catalog, "
            "inventory reservation, order workflow, payment-state simulation, fulfillment "
            "queue, audit log, and admin reporting modules. Keep modules independently "
            "testable, document service boundaries, and include failure-path tests for "
            "stock conflicts, duplicate submissions, and cancelled fulfillment."
        ),
        "ultra": (
            "Build an ultra-large project: a TypeScript/Node enterprise project portfolio "
            "operating system with "
            "programs, projects, milestones, budgets, staffing, risk registers, dependency "
            "maps, approval workflows, analytics exports, and role-based access checks. "
            "Include architecture notes, migration-ready storage boundaries, end-to-end "
            "scenario tests, and load-oriented tests for high-volume portfolio reads."
        ),
    }[case.scale]
    flow_instruction = {
        "direct": "Use direct mode and return a complete project bundle without pretending to run tools.",
        "dispatch": "Use dispatch coordination and preserve clear assignment evidence.",
        "hybrid": "Use hybrid planning plus execution and preserve decision evidence.",
        "multi_agent": "Use multi-agent decomposition with explicit role ownership.",
        "plugin": "Use plugin-style integration boundaries where appropriate.",
        "model_failure": "Exercise failure recovery without hiding the failed attempt.",
        "self_repair": "Exercise self-repair when verification exposes a defect.",
        "artifact_production": "Produce inspectable source artifacts and generated files.",
        "capability_validation": "Validate mode control and capability fit without silent downgrade.",
    }[case.flow]
    return (
        f"Build a real {case.scale} business project for flow={case.flow}. {requirements} "
        "Include npm run build, npm test, source, tests, README, plan and verification instructions. "
        "The bundle must include IMPLEMENTATION_PLAN.md saying it read before implementation: "
        "AGENTS.md workspace rules, HANDOFF current-state index, PROJECT_REQUIREMENTS.md, and "
        "applicable SKILL.md or agent-standard rules. Include constraints_reading_evidence.json "
        "with read_before_implementation true and those constraints/skills. "
        "Return strict JSON workspace_bundle.files (relative paths to full content), or fenced "
        "file blocks headed ### `path/to/file`. Acceptance conditions: independently test API "
        "behavior and errors with reproducible verification evidence. Do not prefill pass records "
        f"or fabricate execution. {flow_instruction}"
    )


__all__ = [
    "PROJECT_SCALE_CLEANUP_ACTIONS",
    "PROJECT_SCALE_FLOW_KINDS",
    "PROJECT_SCALE_REQUIRED_EVIDENCE",
    "PROJECT_SCALE_TIERS",
    "ProjectScaleBenchmarkKind",
    "ProjectScaleCase",
    "ProjectScaleFlow",
    "ProjectScaleMatrix",
    "ProjectScaleRunMode",
    "ProjectScaleRunPlan",
    "ProjectScaleRunRequest",
    "ProjectScaleTier",
    "build_project_scale_run_plan",
    "build_project_scale_run_request",
    "describe_project_scale_matrix",
]
