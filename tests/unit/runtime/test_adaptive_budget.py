from __future__ import annotations

import pytest

from agent_hub.runtime.adaptive_budget import AdaptiveDeadline


def test_adaptive_deadline_extends_only_for_new_progress() -> None:
    budget = AdaptiveDeadline.create(
        now=100.0,
        initial_seconds=30.0,
        soft_seconds=20.0,
        absolute_seconds=90.0,
        complexity_units=4,
    )

    assert budget.deadline == pytest.approx(130.0)
    assert budget.observe(progress_units=1, now=105.0) is True
    assert budget.deadline == pytest.approx(135.0)
    assert budget.observe(progress_units=1, now=110.0) is False
    assert budget.deadline == pytest.approx(135.0)


def test_adaptive_deadline_never_exceeds_absolute_fuse() -> None:
    budget = AdaptiveDeadline.create(
        now=10.0,
        initial_seconds=40.0,
        soft_seconds=40.0,
        absolute_seconds=70.0,
        complexity_units=2,
    )

    assert budget.observe(progress_units=10, now=20.0) is True
    assert budget.deadline == pytest.approx(80.0)
    assert budget.absolute_deadline == pytest.approx(80.0)


def test_adaptive_deadline_restore_preserves_absolute_remaining_budget() -> None:
    budget = AdaptiveDeadline.create(
        now=200.0,
        initial_seconds=12.0,
        soft_seconds=20.0,
        absolute_seconds=90.0,
        complexity_units=4,
        restored_absolute_seconds=25.0,
        progress_units=3,
    )

    assert budget.absolute_deadline == pytest.approx(225.0)
    assert budget.observe(progress_units=8, now=205.0) is True
    assert budget.deadline == pytest.approx(225.0)
    assert budget.remaining(now=210.0) == pytest.approx(15.0)
    assert budget.absolute_remaining(now=210.0) == pytest.approx(15.0)
