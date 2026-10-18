"""Required engineering milestones, independent of orchestration and tool policy."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Literal

EngineeringAction = Literal['project.inspect', 'project.verify', 'workspace.bundle']


@dataclass(frozen=True, slots=True)
class EngineeringProgress:
    inspected: bool = False
    existing_project: bool = False
    has_writes: bool = False
    verification_seen: bool = False
    verification_current: bool = False
    verification_failed_current: bool = False
    bundle_current: bool = False
    uncertain: bool = False
    completion_proposed: bool = False
    recovering: bool = False

    def __post_init__(self) -> None:
        if any(type(getattr(self, field.name)) is not bool for field in fields(self)):
            raise ValueError('engineering progress requires boolean flags')


def required_engineering_action(progress: EngineeringProgress) -> EngineeringAction | None:
    """Choose a prerequisite, never grant permission or retry an uncertain action."""
    if progress.uncertain:
        return None
    if not progress.inspected:
        return 'project.inspect'
    if progress.verification_failed_current:
        return None
    if progress.existing_project and not progress.verification_seen:
        return 'project.verify'
    if ((progress.completion_proposed or progress.recovering) and progress.has_writes
            and not progress.verification_current):
        return 'project.verify'
    if (progress.completion_proposed and progress.verification_current
            and not progress.bundle_current):
        return 'workspace.bundle'
    return None
