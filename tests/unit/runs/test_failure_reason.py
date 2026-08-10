from __future__ import annotations

from agent_hub.runs.service import _runtime_failure_reason


def test_runtime_failure_reason_preserves_safe_runtime_message() -> None:
    assert _runtime_failure_reason(RuntimeError("model gateway failed")) == "model gateway failed"


def test_runtime_failure_reason_redacts_sensitive_provider_message() -> None:
    assert (
        _runtime_failure_reason(RuntimeError("Authorization: Bearer sk-secret-token failed"))
        == "runtime_failed"
    )
