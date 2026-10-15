"""Publisher-observed member budgets do not change bounded journal writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.plugins.test_handover_stage_journal import (
    TENANT,
    events,
    journal,
    private_directory,
)


@pytest.mark.parametrize("members", [2049, 4097])
def test_trusted_publisher_can_bind_observed_member_budget(tmp_path: Path, members: int) -> None:
    store = tmp_path / "store"
    private_directory(store)
    private_directory(store / str(TENANT))
    private_directory(store / str(TENANT) / "example.plugin")
    with journal(store, reserved_members=members, member_budget=members):
        pass
    first = events(store)[0]
    assert first["reserved_members"] == members
    assert first["member_budget"] == members
    assert len(events(store)) == 1


@pytest.mark.parametrize("budget", [0, -1, True, 2.5])
def test_invalid_member_budget_is_rejected_before_ledger(tmp_path: Path, budget: object) -> None:
    store = tmp_path / "store"
    private_directory(store)
    private_directory(store / str(TENANT))
    private_directory(store / str(TENANT) / "example.plugin")
    with pytest.raises(ValueError):
        journal(store, member_budget=budget)
    assert not (store / ".stage-ownership").exists()
