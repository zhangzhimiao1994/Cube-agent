from __future__ import annotations

import pytest

from agent_hub.previews.bridge import preview_fetch_shim


def test_bridge_has_only_owned_identity_not_main_credentials() -> None:
    script = preview_fetch_shim("12345678-1234-4234-9234-123456789abc")
    assert script.startswith('<script data-agent-preview-fetch-shim>')
    assert "agent-preview-hello" in script
    assert "12345678-1234-4234-9234-123456789abc" in script
    assert "window.fetch" in script
    assert "body_base64" in script
    assert "allow-same-origin" not in script
    assert "Authorization" not in script
    assert "agent_hub_access_token" not in script


@pytest.mark.parametrize("value", ["</script><script>", "", "../bad", "\n", "1234"])
def test_bridge_rejects_script_injection_and_invalid_identity(value: str) -> None:
    with pytest.raises(ValueError):
        preview_fetch_shim(value)
