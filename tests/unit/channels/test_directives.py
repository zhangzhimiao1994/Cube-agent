from agent_hub.channels.directives import (
    command_help_text,
    directive_summary,
    parse_channel_directives,
    parse_channel_resource_hints,
)


def test_resource_hints_only_parse_leading_selector_block() -> None:
    hints = parse_channel_resource_hints(
        "@github &deep-research #filesystem 请分析 @someone、#标题、C# 和 & 符号"
    )

    assert hints.plugins == ("github",)
    assert hints.skills == ("deep-research",)
    assert hints.mcp_servers == ("filesystem",)


def test_resource_hints_ignore_body_symbols_without_leading_selector() -> None:
    hints = parse_channel_resource_hints("请分析 @someone 的账号、#标题、C# 示例和 & 符号")

    assert hints.plugins == ()
    assert hints.skills == ()
    assert hints.mcp_servers == ()


def test_help_text_describes_resource_selectors_not_channel_commands() -> None:
    help_text = command_help_text()

    assert "资源选择器" in help_text
    assert "@plugin" in help_text
    assert "&skill" in help_text
    assert "#mcp" in help_text
    assert "飞书交互指令" not in help_text
    assert "通道指令" not in help_text

def test_invalid_selector_summary_uses_entry_hint_wording() -> None:
    summary = directive_summary(parse_channel_directives("@bad/name 请处理"))

    assert "入口提示有误" in summary
    assert "资源选择器格式不正确" in summary
    assert "channel directive" not in summary
