from __future__ import annotations

from agent_hub.observability.logging import redact


def test_secret_values_are_redacted_from_nested_payloads() -> None:
    payload = {
        "authorization": "Bearer SECRET_TOKEN",
        "safe": "value",
        "nested": {"api_key": "sk-secret"},
    }

    redacted = redact(payload)

    assert str(redacted).count("[REDACTED]") == 2
    assert "SECRET_TOKEN" not in str(redacted)
    assert "sk-secret" not in str(redacted)

