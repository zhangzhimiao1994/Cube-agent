from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from agent_hub.auth.models import AuthenticatedPrincipal, Authorizer, PermissionDenied
from agent_hub.domain.runs import TaskMode


class FeishuCommandKind(StrEnum):
    MODE = "mode"
    RUN = "run"
    CONFIG = "config"
    SKILL = "skill"
    MCP = "mcp"


@dataclass(frozen=True, slots=True)
class FeishuCommand:
    kind: FeishuCommandKind
    name: str
    arguments: tuple[str, ...] = ()
    mode: TaskMode | None = None


@dataclass(frozen=True, slots=True)
class FeishuCommandResult:
    command: FeishuCommand | None = None
    error_code: str | None = None

    @property
    def accepted(self) -> bool:
        return self.command is not None and self.error_code is None


class FeishuCommandParser:
    """Parse a strict, explicit Feishu bot command grammar."""

    _MODE_COMMANDS: ClassVar[dict[str, TaskMode]] = {
        "/auto": TaskMode.AUTO,
        "/direct": TaskMode.DIRECT,
        "/dispatch": TaskMode.DISPATCH,
        "/discuss": TaskMode.DISCUSS,
        "/hybrid": TaskMode.HYBRID,
    }
    _RUN_PERMISSIONS: ClassVar[dict[str, str]] = {
        "/status": "run:read",
        "/details": "run:read",
        "/pause": "run:pause",
        "/resume": "run:resume",
        "/cancel": "run:cancel",
    }
    _CONFIG_PERMISSIONS: ClassVar[dict[str, str]] = {
        "show": "config:read",
        "get": "config:read",
        "test": "config:read",
        "publish": "config:publish",
        "rollback": "config:publish",
    }
    _SKILL_PERMISSIONS: ClassVar[dict[str, str]] = {
        "list": "skill:read",
        "show": "skill:read",
        "install": "skill:write",
        "upload": "skill:write",
        "approve": "skill:approve",
        "disable": "skill:write",
    }
    _MCP_PERMISSIONS: ClassVar[dict[str, str]] = {
        "list": "mcp:read",
        "show": "mcp:read",
        "enable": "mcp:write",
        "disable": "mcp:write",
        "test": "mcp:read",
    }
    _SAFE_ARGUMENT: ClassVar[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_./-]{1,128}$")

    def __init__(self, authorizer: Authorizer | None = None) -> None:
        self._authorizer = authorizer or Authorizer()

    def parse(self, text: str, principal: AuthenticatedPrincipal) -> FeishuCommandResult:
        try:
            tokens = _split_command(text)
        except ValueError:
            return FeishuCommandResult(error_code="invalid_syntax")
        if not tokens:
            return FeishuCommandResult(error_code="empty_command")
        head = tokens[0].lower()
        if any(token.startswith("--") for token in tokens[1:]):
            return FeishuCommandResult(error_code="unknown_flag")
        if head in self._MODE_COMMANDS:
            return self._mode_command(head, tokens[1:], principal)
        if head in self._RUN_PERMISSIONS:
            return self._run_command(head, tokens[1:], principal)
        if head == "/config":
            return self._structured_command(
                FeishuCommandKind.CONFIG,
                tokens[1:],
                principal,
                self._CONFIG_PERMISSIONS,
            )
        if head == "/skill":
            return self._structured_command(
                FeishuCommandKind.SKILL,
                tokens[1:],
                principal,
                self._SKILL_PERMISSIONS,
            )
        if head == "/mcp":
            return self._structured_command(
                FeishuCommandKind.MCP,
                tokens[1:],
                principal,
                self._MCP_PERMISSIONS,
            )
        return FeishuCommandResult(error_code="unknown_command")

    def _mode_command(
        self,
        head: str,
        arguments: list[str],
        principal: AuthenticatedPrincipal,
    ) -> FeishuCommandResult:
        if not self._allowed(principal, "run:create"):
            return FeishuCommandResult(error_code="permission_denied")
        if not _safe_arguments(arguments):
            return FeishuCommandResult(error_code="invalid_argument")
        return FeishuCommandResult(
            command=FeishuCommand(
                kind=FeishuCommandKind.MODE,
                name=head.removeprefix("/"),
                arguments=tuple(arguments),
                mode=self._MODE_COMMANDS[head],
            )
        )

    def _run_command(
        self,
        head: str,
        arguments: list[str],
        principal: AuthenticatedPrincipal,
    ) -> FeishuCommandResult:
        if not self._allowed(principal, self._RUN_PERMISSIONS[head]):
            return FeishuCommandResult(error_code="permission_denied")
        if len(arguments) > 1 or not _safe_arguments(arguments):
            return FeishuCommandResult(error_code="invalid_argument")
        return FeishuCommandResult(
            command=FeishuCommand(
                kind=FeishuCommandKind.RUN,
                name=head.removeprefix("/"),
                arguments=tuple(arguments),
            )
        )

    def _structured_command(
        self,
        kind: FeishuCommandKind,
        arguments: list[str],
        principal: AuthenticatedPrincipal,
        permissions: dict[str, str],
    ) -> FeishuCommandResult:
        if not arguments:
            return FeishuCommandResult(error_code="missing_subcommand")
        subcommand = arguments[0].lower()
        permission = permissions.get(subcommand)
        if permission is None:
            return FeishuCommandResult(error_code="unknown_subcommand")
        if not self._allowed(principal, permission):
            return FeishuCommandResult(error_code="permission_denied")
        rest = arguments[1:]
        if len(rest) > 3 or not _safe_arguments(rest):
            return FeishuCommandResult(error_code="invalid_argument")
        return FeishuCommandResult(
            command=FeishuCommand(
                kind=kind,
                name=subcommand,
                arguments=tuple(rest),
            )
        )

    def _allowed(self, principal: AuthenticatedPrincipal, permission: str) -> bool:
        try:
            self._authorizer.require(principal, permission)
        except PermissionDenied:
            return False
        return True


def _split_command(text: str) -> list[str]:
    if len(text.encode()) > 2048:
        raise ValueError("command too long")
    return shlex.split(text, comments=False, posix=True)


def _safe_arguments(arguments: list[str]) -> bool:
    return all(FeishuCommandParser._SAFE_ARGUMENT.fullmatch(item) is not None for item in arguments)


__all__ = [
    "FeishuCommand",
    "FeishuCommandKind",
    "FeishuCommandParser",
    "FeishuCommandResult",
]
