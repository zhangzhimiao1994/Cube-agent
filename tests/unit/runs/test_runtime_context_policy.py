from __future__ import annotations

from agent_hub.domain.runs import TaskMode
from agent_hub.runs.service import _runtime_timeout_seconds, _runtime_token_budget


def test_runtime_timeout_policy_uses_configured_production_window_for_dispatch() -> None:
    assert _runtime_timeout_seconds(TaskMode.DISPATCH, configured_seconds=300.0) == 300.0


def test_runtime_timeout_policy_clamps_to_runtime_contract_limit() -> None:
    assert _runtime_timeout_seconds(TaskMode.HYBRID, configured_seconds=7200.0) == 3600.0


def test_runtime_token_budget_policy_uses_configured_complex_budget() -> None:
    assert _runtime_token_budget(TaskMode.HYBRID, configured_tokens=1_000_000) == 1_000_000


def test_runtime_token_budget_policy_clamps_to_runtime_contract_limit() -> None:
    assert _runtime_token_budget(TaskMode.DISPATCH, configured_tokens=99_000_000) == 10_000_000
