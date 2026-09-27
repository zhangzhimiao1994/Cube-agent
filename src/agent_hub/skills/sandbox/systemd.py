from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_hub.skills.sandbox.base import SkillInvocation, SkillResult
from agent_hub.skills.sandbox.broker import (
    BROKER_SOCKET_PATH,
    BrokerRequest,
    request_systemd_broker,
)


@dataclass(frozen=True, slots=True)
class SystemdSandboxSettings:
    socket_path: Path = BROKER_SOCKET_PATH


class SystemdSkillSandbox:
    def __init__(self, settings: SystemdSandboxSettings | None = None) -> None:
        self._settings = settings or SystemdSandboxSettings()

    async def run(self, invocation: SkillInvocation) -> SkillResult:
        response = await request_systemd_broker(
            BrokerRequest.run(invocation),
            socket_path=self._settings.socket_path,
        )
        if not response.ok or response.result is None:
            raise OSError(response.error or "systemd Skill broker rejected execution")
        return response.result

    async def terminate(self, execution_id: str) -> None:
        response = await request_systemd_broker(
            BrokerRequest.terminate(execution_id),
            socket_path=self._settings.socket_path,
        )
        if not response.ok:
            raise OSError(response.error or "systemd Skill broker rejected termination")
