from agent_hub.config.schema import PlatformConfig
from agent_hub.models.routing_matrix import RoleModelRoutingRequest, rank_role_models


def test_role_model_routing_matrix_explains_creative_model_selection() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "deepseek": {
                    "deployments": [
                        {
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "api_base": "https://api.deepseek.com/v1",
                            "credential_ref": "secret://deepseek",
                            "quota_scope_id": "deepseek",
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
                "creative": {
                    "deployments": [
                        {
                            "provider": "kimi",
                            "model": "kimi-k2-latest",
                            "api_base": "https://api.moonshot.cn/v1",
                            "credential_ref": "secret://kimi",
                            "quota_scope_id": "kimi",
                            "capabilities": ["text"],
                        }
                    ]
                },
            },
            "agents": [],
        }
    )

    ranked = rank_role_models(
        RoleModelRoutingRequest(
            task="生成玄幻 AI 短剧提示词。",
            role_id="copywriter",
            role="文案生成",
            purpose="execute",
            mission="生成短视频口播脚本和即梦提示词。",
            skills=(),
            must_answer=("文案是什么？",),
            allowed_tools=(),
            preferred_model="deepseek",
            default_model="deepseek",
        ),
        config,
    )

    assert ranked[0].logical_model == "creative"
    assert ranked[0].selected is True
    assert "task:creative" in ranked[0].reasons
    assert "model_trait:creative" in ranked[0].reasons
    assert {candidate.logical_model for candidate in ranked} == {"creative", "deepseek"}


def test_role_model_routing_matrix_blocks_messages_endpoint_for_tool_roles() -> None:
    config = PlatformConfig.model_validate(
        {
            "models": {
                "sonnet": {
                    "deployments": [
                        {
                            "provider": "anthropic",
                            "model": "claude-sonnet-4-5",
                            "api_base": "https://api.anthropic.com/v1/messages",
                            "credential_ref": "secret://sonnet",
                            "quota_scope_id": "sonnet",
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
                "qwen": {
                    "deployments": [
                        {
                            "provider": "qwen",
                            "model": "qwen3-max",
                            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            "credential_ref": "secret://qwen",
                            "quota_scope_id": "qwen",
                            "capabilities": ["text", "tool_calling", "structured_output"],
                        }
                    ]
                },
            },
            "agents": [],
        }
    )

    ranked = rank_role_models(
        RoleModelRoutingRequest(
            task="生成一个计划并调用工具读取上下文。",
            role_id="planner",
            role="Planner",
            purpose="execute",
            mission="拆解任务、定义步骤和验收标准。",
            skills=(),
            must_answer=("步骤是什么？",),
            allowed_tools=("read_context",),
            preferred_model="sonnet",
            default_model="sonnet",
        ),
        config,
    )

    assert ranked[0].logical_model == "qwen"
    blocked = next(candidate for candidate in ranked if candidate.logical_model == "sonnet")
    assert blocked.eligible is False
    assert "capability:tool_role_unsupported_messages_endpoint" in blocked.reasons
    assert "capability:tool_role_supported" in ranked[0].reasons
