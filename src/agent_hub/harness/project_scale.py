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

    @property
    def case_count(self) -> int:
        return len(self.requests)

    def to_payload(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "execute": self.execute,
            "requires_bearer_token": self.requires_bearer_token,
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


def build_project_scale_run_request(case: ProjectScaleCase) -> ProjectScaleRunRequest:
    mode = _FLOW_RUN_MODES[case.flow]
    session_id = f"project-scale-{case.scale}-{case.flow}"
    body: dict[str, object] = {
        "message": _fixture_message(case),
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
) -> ProjectScaleRunPlan:
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
        build_project_scale_run_request(case)
        for case in ProjectScaleMatrix.default().cases
        if case.scale in selected_scales and case.flow in selected_flows
    )
    return ProjectScaleRunPlan(
        requests=requests,
        dry_run=not execute,
        execute=execute,
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
    direct_guidance = (
        " For direct flow, do not call tools, do not emit DSML/tool-call syntax, and do not "
        "describe commands as if they were executed. Produce the deliverable inline as either "
        "strict JSON with workspace_bundle.files mapping safe relative paths to complete file "
        "contents, or Markdown file blocks headed exactly like ### `path/to/file` followed by "
        "a fenced code block. Include README or requirements, source files, tests or build "
        "scripts, an implementation plan, and a verification report with reproducible build, "
        "test, and interaction evidence. Avoid credential-like terms and avoid package, file, "
        "variable, or fixture names that contain the sk- prefix so public evidence stays visible."
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
        f"{plugin_guidance}{direct_guidance}"
    )


__all__ = [
    "PROJECT_SCALE_CLEANUP_ACTIONS",
    "PROJECT_SCALE_FLOW_KINDS",
    "PROJECT_SCALE_REQUIRED_EVIDENCE",
    "PROJECT_SCALE_TIERS",
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
