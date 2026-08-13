from __future__ import annotations

import re
from dataclasses import dataclass

from agent_hub.domain.runs import TaskMode

_DIRECTIVE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MODES = {
    "/auto": TaskMode.AUTO,
    "/direct": TaskMode.DIRECT,
    "/dispatch": TaskMode.DISPATCH,
    "/discuss": TaskMode.DISCUSS,
    "/hybrid": TaskMode.HYBRID,
}


@dataclass(frozen=True, slots=True)
class ChannelDirectives:
    mode: TaskMode | None
    task_text: str
    plugins: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    invalid_reason: str | None = None


class ChannelDirectiveError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def parse_channel_directives(text: str) -> ChannelDirectives:
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
            next_mode = _MODES[value]
            if mode is not None and mode is not next_mode:
                return ChannelDirectives(
                    mode=None,
                    task_text=" ".join(tokens[consumed:]).strip(),
                    invalid_reason="conflicting_modes",
                )
            mode = next_mode
        elif kind == "plugin":
            plugins.append(value)
        elif kind == "mcp":
            mcp_servers.append(value)
        elif kind == "skill":
            skills.append(value)

    task_text = " ".join(tokens[consumed:]).strip() if consumed else text
    if not task_text:
        return ChannelDirectives(
            mode=mode,
            task_text="",
            plugins=tuple(dict.fromkeys(plugins)),
            mcp_servers=tuple(dict.fromkeys(mcp_servers)),
            skills=tuple(dict.fromkeys(skills)),
            invalid_reason="missing_task_text",
        )
    return ChannelDirectives(
        mode=mode,
        task_text=task_text,
        plugins=tuple(dict.fromkeys(plugins)),
        mcp_servers=tuple(dict.fromkeys(mcp_servers)),
        skills=tuple(dict.fromkeys(skills)),
    )


def directive_summary(directives: ChannelDirectives) -> str:
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
    lines.append("我会按这些选择执行；若未指定模式，将由主 Agent 自动判断。")
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
    if token in _MODES:
        return ("mode", token)
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
    return token.startswith(("/#", "@", "&"))


__all__ = [
    "ChannelDirectiveError",
    "ChannelDirectives",
    "directive_summary",
    "parse_channel_directives",
]
