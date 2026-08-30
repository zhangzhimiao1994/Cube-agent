from agent_hub.runs.repository import _public_event_payload


def test_public_event_payload_redacts_sensitive_values_under_safe_keys() -> None:
    payload = _public_event_payload(
        {
            "safe_summary": "failed after Bearer sk-private-token reached provider",
            "nested": {
                "safe_hint": "Authorization: Bearer sk-private-token",
                "ok": "retry with model fallback",
            },
            "items": ["private-token value", {"note": "normal"}],
            "credential_ref": "secret://must-drop",
        }
    )

    assert payload == {
        "safe_summary": "[redacted]",
        "nested": {
            "safe_hint": "[redacted]",
            "ok": "retry with model fallback",
        },
        "items": ["[redacted]", {"note": "normal"}],
    }
