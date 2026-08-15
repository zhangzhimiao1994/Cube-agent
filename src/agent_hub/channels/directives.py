from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from agent_hub.domain.runs import TaskMode

_DIRECTIVE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_LEGACY_MODES = {
    "/auto": TaskMode.AUTO,
    "/direct": TaskMode.DIRECT,
    "/dispatch": TaskMode.DISPATCH,
    "/discuss": TaskMode.DISCUSS,
    "/hybrid": TaskMode.HYBRID,
}
_CHANNEL_LANGUAGE_MODES = {
    "//auto": TaskMode.AUTO,
    "//automatic": TaskMode.AUTO,
    "//自动": TaskMode.AUTO,
    "//direct": TaskMode.DIRECT,
    "//directly": TaskMode.DIRECT,
    "//直连": TaskMode.DIRECT,
    "//直接": TaskMode.DIRECT,
    "//dispatch": TaskMode.DISPATCH,
    "//route": TaskMode.DISPATCH,
    "//派单": TaskMode.DISPATCH,
    "//分派": TaskMode.DISPATCH,
    "//discuss": TaskMode.DISCUSS,
    "//discussion": TaskMode.DISCUSS,
    "//讨论": TaskMode.DISCUSS,
    "//辩论": TaskMode.DISCUSS,
    "//hybrid": TaskMode.HYBRID,
    "//mix": TaskMode.HYBRID,
    "//mixed": TaskMode.HYBRID,
    "//混合": TaskMode.HYBRID,
}
_VIBE_CODING_DIRECTIVES = {
    "//vi",
    "//vibe",
    "//vibecoding",
    "//vibe-coding",
    "//code",
    "//coding",
    "//代码",
    "//编程",
    "//代码协作",
}
_HELP_DIRECTIVES = {
    "help",
    "帮助",
    "菜单",
    "指令",
    "//help",
    "//帮助",
    "//菜单",
    "//指令",
}


@dataclass(frozen=True, slots=True)
class ChannelDirectives:
    mode: TaskMode | None
    task_text: str
    plugins: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    vibe_coding: bool = False
    invalid_reason: str | None = None


class ChannelDirectiveError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def parse_channel_directives(
    text: str, aliases: Mapping[str, str] | None = None
) -> ChannelDirectives:
    text = apply_channel_command_aliases(text, aliases)
    stripped = text.strip()
    if not stripped:
        return ChannelDirectives(mode=None, task_text="", invalid_reason="empty_message")
    tokens = stripped.split()
    mode: TaskMode | None = None
    plugins: list[str] = []
    mcp_servers: list[str] = []
    skills: list[str] = []
    consumed = 0
    for token in tokens:
        parsed = _parse_token(token)
        if parsed is None:
            if _looks_like_channel_directive(token):
                return ChannelDirectives(
                    mode=None,
                    task_text=" ".join(tokens[consumed:]).strip(),
                    plugins=tuple(dict.fromkeys(plugins)),
                    mcp_servers=tuple(dict.fromkeys(mcp_servers)),
                    skills=tuple(dict.fromkeys(skills)),
                    invalid_reason="invalid_directive",
                )
            break
        kind, value = parsed
        consumed += 1
        if kind == "mode":
            next_mode = _mode_from_directive(value)
            if mode is not None and mode is not next_mode:
                return ChannelDirectives(
                    mode=None,
                    task_text=" ".join(tokens[consumed:]).strip(),
                    plugins=tuple(dict.fromkeys(plugins)),
                    mcp_servers=tuple(dict.fromkeys(mcp_servers)),
                    skills=tuple(dict.fromkeys(skills)),
                    invalid_reason="conflicting_modes",
                )
            mode = next_mode
        elif kind == "plugin":
            plugins.append(value)
        elif kind == "mcp":
            mcp_servers.append(value)
        elif kind == "skill":
            skills.append(value)
        elif kind == "vibe_coding":
            pass

    task_text = " ".join(tokens[consumed:]).strip() if consumed else text
    vibe_coding = any(token.casefold() in _VIBE_CODING_DIRECTIVES for token in tokens[:consumed])
    if not task_text:
        return ChannelDirectives(
            mode=mode,
            task_text="",
            plugins=tuple(dict.fromkeys(plugins)),
            mcp_servers=tuple(dict.fromkeys(mcp_servers)),
            skills=tuple(dict.fromkeys(skills)),
            vibe_coding=vibe_coding,
            invalid_reason="missing_task_text",
        )
    return ChannelDirectives(
        mode=mode,
        task_text=task_text,
        plugins=tuple(dict.fromkeys(plugins)),
        mcp_servers=tuple(dict.fromkeys(mcp_servers)),
        skills=tuple(dict.fromkeys(skills)),
        vibe_coding=vibe_coding,
    )


def parse_command_aliases(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    aliases: dict[str, str] = {}
    for chunk in re.split(r"[\n,;，；]+", value):
        item = chunk.strip()
        if not item:
            continue
        separator = "=" if "=" in item else ":" if ":" in item else None
        if separator is None:
            continue
        alias, target = (part.strip() for part in item.split(separator, 1))
        if not alias or re.search(r"\s", alias):
            continue
        normalized_target = _normalize_alias_target(target)
        if normalized_target is None:
            continue
        aliases[alias] = normalized_target
    return aliases


def apply_channel_command_aliases(text: str, aliases: Mapping[str, str] | None = None) -> str:
    if not aliases:
        return text
    stripped = text.strip()
    if not stripped:
        return text
    parts = stripped.split(maxsplit=1)
    alias_lookup = {alias.casefold(): target for alias, target in aliases.items()}
    replacement = alias_lookup.get(parts[0].casefold())
    if replacement is None:
        return text
    if len(parts) == 1:
        return replacement
    return f"{replacement} {parts[1]}"


def is_channel_help_request(text: str, aliases: Mapping[str, str] | None = None) -> bool:
    normalized = apply_channel_command_aliases(text, aliases).strip().casefold()
    return normalized in _HELP_DIRECTIVES


def command_help_text(aliases: Mapping[str, str] | None = None) -> str:
    alias_items = [] if not aliases else [f"{alias}={target}" for alias, target in sorted(aliases.items())]
    lines = [
        "飞书交互指令：先写指令，再写任务正文。",
        "模式：//自动、//直连、//派单、//讨论、//混合。",
        "能力：//vi 开启 Vibe Coding；&skill 指定 Skill；@plugin 指定插件；/#mcp 指定 MCP。",
        "帮助：发送 帮助、菜单、指令 或 //帮助，会返回这份菜单且不会创建任务。",
    ]
    if alias_items:
        lines.append("当前自定义指令：" + "，".join(alias_items[:12]))
        if len(alias_items) > 12:
            lines.append(f"还有 {len(alias_items) - 12} 条别名未展示，可在通道配置中查看。")
    else:
        lines.append("自定义指令：在通道配置里填写“别名=标准指令”，例如：方案=//派单。")
    return "\n".join(lines)


def directive_summary(
    directives: ChannelDirectives, aliases: Mapping[str, str] | None = None
) -> str:
    if directives.invalid_reason is not None:
        return "通道指令有误：" + _reason_text(directives.invalid_reason)
    mode = _mode_label(directives.mode)
    lines = [f"已收到，解析到本次通道指令：模式={mode}"]
    if directives.skills:
        lines.append("Skills: " + ", ".join(directives.skills))
    if directives.mcp_servers:
        lines.append("MCP: " + ", ".join(directives.mcp_servers))
    if directives.plugins:
        lines.append("Plugins: " + ", ".join(directives.plugins))
    if directives.vibe_coding:
        lines.append("Vibe Coding: enabled")
    lines.append("我会按这些选择执行；若未指定模式，将由主 Agent 自动判断。")
    lines.append(command_help_text(aliases))
    return "\n".join(lines)


def _mode_label(mode: TaskMode | None) -> str:
    if mode is None or mode is TaskMode.AUTO:
        return "自动判断"
    labels = {
        TaskMode.DIRECT: "直接回答（direct）",
        TaskMode.DISPATCH: "派单模式（dispatch）",
        TaskMode.DISCUSS: "讨论模式（discuss）",
        TaskMode.HYBRID: "混合模式（hybrid）",
    }
    return labels.get(mode, mode.value)


def _reason_text(reason: str) -> str:
    if reason == "conflicting_modes":
        return "检测到多个运行模式，请只选择一个，例如 /dispatch 或 /discuss。"
    if reason == "missing_task_text":
        return "缺少任务正文，请在模式、插件、MCP 或 Skill 指令后写清楚要做什么。"
    if reason == "empty_message":
        return "消息内容为空。"
    if reason == "invalid_directive":
        return (
            "Invalid channel directive format. Use /#name for MCP, &name for Skill, "
            "and @name for plugin; names may contain letters, numbers, dot, underscore, and dash."
        )
    return reason


def _parse_token(token: str) -> tuple[str, str] | None:
    lowered = token.casefold()
    if token in _LEGACY_MODES or lowered in _CHANNEL_LANGUAGE_MODES:
        return ("mode", token)
    if lowered in _VIBE_CODING_DIRECTIVES:
        return ("vibe_coding", token)
    if token.startswith("/#"):
        value = token[2:]
        return ("mcp", value) if _DIRECTIVE_RE.fullmatch(value) else None
    if token.startswith("@"):
        value = token[1:]
        return ("plugin", value) if _DIRECTIVE_RE.fullmatch(value) else None
    if token.startswith("&"):
        value = token[1:]
        return ("skill", value) if _DIRECTIVE_RE.fullmatch(value) else None
    return None


def _looks_like_channel_directive(token: str) -> bool:
    return token.startswith(("//", "/#", "@", "&"))


def _normalize_alias_target(target: str) -> str | None:
    stripped = target.strip()
    lowered = stripped.casefold()
    shorthand = {
        "auto": "//auto",
        "自动": "//自动",
        "direct": "//direct",
        "直连": "//直连",
        "直接": "//直接",
        "dispatch": "//dispatch",
        "派单": "//派单",
        "分派": "//分派",
        "discuss": "//discuss",
        "讨论": "//讨论",
        "hybrid": "//hybrid",
        "混合": "//混合",
        "vibe": "//vi",
        "vi": "//vi",
        "代码": "//vi",
        "help": "//help",
        "帮助": "//帮助",
        "菜单": "//帮助",
        "指令": "//帮助",
    }
    stripped = shorthand.get(lowered, stripped)
    if stripped.casefold() in _HELP_DIRECTIVES:
        return stripped
    if _parse_token(stripped) is None:
        return None
    return stripped


def _mode_from_directive(token: str) -> TaskMode:
    if token in _LEGACY_MODES:
        return _LEGACY_MODES[token]
    return _CHANNEL_LANGUAGE_MODES[token.casefold()]


__all__ = [
    "ChannelDirectiveError",
    "ChannelDirectives",
    "apply_channel_command_aliases",
    "command_help_text",
    "directive_summary",
    "is_channel_help_request",
    "parse_channel_directives",
    "parse_command_aliases",
]
