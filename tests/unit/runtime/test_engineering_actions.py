from __future__ import annotations

from dataclasses import replace

import pytest

from agent_hub.runtime.engineering_actions import EngineeringProgress, required_engineering_action


def test_inspection_is_first_actual_milestone() -> None:
    assert required_engineering_action(EngineeringProgress()) == 'project.inspect'


def test_existing_runnable_project_is_verified_before_recovery_reads() -> None:
    assert required_engineering_action(EngineeringProgress(inspected=True, existing_project=True)) == 'project.verify'


def test_new_empty_workspace_can_generate_until_completion_proposal() -> None:
    progress = EngineeringProgress(inspected=True, has_writes=True)
    assert required_engineering_action(progress) is None
    assert required_engineering_action(replace(progress, completion_proposed=True)) == 'project.verify'
    assert required_engineering_action(replace(progress, recovering=True)) == 'project.verify'


def test_failed_unchanged_source_is_not_blindly_reverified() -> None:
    assert required_engineering_action(EngineeringProgress(inspected=True, existing_project=True,
        has_writes=True, verification_seen=True, verification_failed_current=True,
        completion_proposed=True, recovering=True)) is None


def test_verified_source_is_bundled_only_after_completion_proposal() -> None:
    progress = EngineeringProgress(inspected=True, existing_project=True, verification_seen=True,
        verification_current=True)
    assert required_engineering_action(progress) is None
    assert required_engineering_action(replace(progress, completion_proposed=True)) == 'workspace.bundle'
    assert required_engineering_action(replace(progress, completion_proposed=True, bundle_current=True)) is None


def test_uncertain_outcomes_never_authorize_a_new_action() -> None:
    assert required_engineering_action(EngineeringProgress(uncertain=True, recovering=True,
        completion_proposed=True, has_writes=True)) is None


def test_progress_flags_are_strict_booleans() -> None:
    with pytest.raises(ValueError, match='boolean'):
        EngineeringProgress(inspected=1)  # type: ignore[arg-type]
