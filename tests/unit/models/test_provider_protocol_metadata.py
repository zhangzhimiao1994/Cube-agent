from typing import Any

import pytest

from agent_hub.models.types import ModelResponse


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
def test_provider_protocol_metadata_accepts_only_supported_protocols(protocol: str) -> None:
    metadata = {"api_protocol": protocol, "model": "test-model"}
    response = ModelResponse(text="ok", provider_metadata=metadata)
    assert response.provider_metadata["api_protocol"] == protocol
    metadata["api_protocol"] = "changed"
    assert response.provider_metadata["api_protocol"] == protocol
    with pytest.raises(TypeError):
        response.provider_metadata["api_protocol"] = "responses"  # type: ignore[index]


@pytest.mark.parametrize(
    "invalid",
    ["messages", "json_object", "RESPONSES", "responses ", "private-value", "", None, True, 1],
)
def test_provider_protocol_metadata_rejects_nonenum_values(invalid: Any) -> None:
    with pytest.raises(ValueError, match="api_protocol") as caught:
        ModelResponse(text="ok", provider_metadata={"api_protocol": invalid})
    assert "private-value" not in str(caught.value)


def test_provider_protocol_metadata_remains_optional() -> None:
    assert ModelResponse(text="ok").provider_metadata == {}
