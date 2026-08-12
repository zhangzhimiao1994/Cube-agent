from __future__ import annotations

from agent_hub.config.schema import PlatformConfig
from agent_hub.runs.temporary_agents import _CAPABILITIES, _choose_recommended_model


def test_temporary_agent_policy_prefers_code_capable_model_for_engineering_gap() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "general": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-v4-flash",
                            "credential_ref": "secret://deepseek",
                            "quota_scope_id": "general",
                            "capabilities": ["text"],
                        }
                    ]
                },
                "claude_code": {
                    "deployments": [
                        {
                            "provider": "anthropic-compatible",
                            "model": "claude-sonnet-5",
                            "credential_ref": "secret://claude",
                            "quota_scope_id": "claude-code",
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
            },
            "agents": [],
        }
    )

    engineering = next(
        spec for spec in _CAPABILITIES if spec.capability == "software_engineering"
    )

    assert _choose_recommended_model(config, engineering) == "claude_code"


def test_temporary_agent_policy_returns_none_when_no_text_model_exists() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "vision_only": {
                    "deployments": [
                        {
                            "provider": "minimax",
                            "model": "abab-vision",
                            "credential_ref": "secret://vision",
                            "quota_scope_id": "vision",
                            "capabilities": ["vision"],
                        }
                    ]
                }
            },
            "agents": [],
        }
    )
    copywriting = next(spec for spec in _CAPABILITIES if spec.capability == "copywriting")

    assert _choose_recommended_model(config, copywriting) is None
