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
]

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
)
PROJECT_SCALE_REQUIRED_EVIDENCE: tuple[str, ...] = (
    "run_details",
    "run_events",
    "workspace_bundle",
    "final_artifacts",
    "self_repair_trace",
    "release_health",
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


def _build_case(*, scale: ProjectScaleTier, flow: ProjectScaleFlow) -> ProjectScaleCase:
    focus = ["interaction_stability", "final_result"]
    if scale in _LONG_RUNNING_SCALES:
        focus.append("long_running_control")
    if scale in _PREFLIGHT_SCALES:
        focus.append("project_preflight")
    if flow in _FAILURE_FLOWS:
        focus.extend(("fault_injection", "self_repair"))
    if flow in _ARTIFACT_FLOWS:
        focus.append("artifact_integrity")
    return ProjectScaleCase(
        scale=scale,
        flow=flow,
        requires_bearer_token=True,
        requires_explicit_server_profile=scale in _LONG_RUNNING_SCALES,
        expected_preflight=scale in _PREFLIGHT_SCALES,
        validation_focus=tuple(dict.fromkeys(focus)),
    )


__all__ = [
    "PROJECT_SCALE_CLEANUP_ACTIONS",
    "PROJECT_SCALE_FLOW_KINDS",
    "PROJECT_SCALE_REQUIRED_EVIDENCE",
    "PROJECT_SCALE_TIERS",
    "ProjectScaleCase",
    "ProjectScaleFlow",
    "ProjectScaleMatrix",
    "ProjectScaleTier",
    "describe_project_scale_matrix",
]
