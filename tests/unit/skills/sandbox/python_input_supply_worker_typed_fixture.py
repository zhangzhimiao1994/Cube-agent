"""Typed subprocess boundary and real DTOs for the offline supply worker tests."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, TypedDict, TypeGuard, Unpack
from uuid import UUID

from agent_hub.capabilities.python_project_inputs import PythonInterpreterIdentity
from agent_hub.skills.sandbox.base import ProcessLimits, SkillResult
from agent_hub.skills.sandbox.broker import BrokerPolicy
from agent_hub.skills.sandbox.project import OriginalProjectReference
from agent_hub.skills.sandbox.python_input_fetch import WheelDownload
from agent_hub.skills.sandbox.python_input_supply_catalog import RootSupplyConfig
from agent_hub.skills.sandbox.python_input_supply_protocol import (
    PythonInputSupplyArtifact,
    PythonInputSupplyPlan,
    PythonSupplyInterpreter,
)
from agent_hub.skills.sandbox.python_project_backend import RootConfig
from agent_hub.skills.sandbox.python_project_cache import PythonProjectCachePolicy
from agent_hub.skills.sandbox.python_project_protocol import (
    PythonProjectProbeInvocation,
    PythonProjectScope,
)


class RunOptions(TypedDict, total=False):
    stdin_payload: bytes | None
    process_started: Callable[[asyncio.subprocess.Process], None] | None
    on_forced_terminate: Callable[[], Awaitable[None]] | None
    termination_grace_seconds: float


class SubprocessRunner(Protocol):
    async def __call__(
        self, command: tuple[str, ...], limits: ProcessLimits, **kwargs: Unpack[RunOptions]
    ) -> SkillResult: ...


type RecordedCalls = list[tuple[tuple[str, ...], ProcessLimits, RunOptions]]
type SubprocessFixture = tuple[RecordedCalls, SubprocessRunner]


def entries() -> tuple[WheelDownload, ...]:
    return tuple(
        WheelDownload(
            filename=name + "-1.0-py3-none-any.whl", name=name, version="1.0",
            sha256="a" * 64, size_bytes=3,
            url="https://files.pythonhosted.org/packages/aa/bb/" + "c" * 60 + "/" + name + "-1.0-py3-none-any.whl",
        )
        for name in ("alpha", "beta")
    )


def supply_configuration(*, max_total_bytes: int = 1024) -> RootSupplyConfig:
    return RootSupplyConfig(
        state_root=Path("/var/lib/agent-hub-python-input-supply"), catalog=entries(),
        max_total_bytes=max_total_bytes, min_free_bytes=0, fetch_timeout_seconds=20,
    )


def configuration() -> RootConfig:
    return RootConfig(
        runtime_root=Path("/opt/python-runtime"),
        interpreter=PythonInterpreterIdentity(
            implementation="cpython", version="3.12.14", abi="cp312", platform="linux_x86_64",
            binary_sha256="a" * 64, base_runtime_sha256="b" * 64,
        ),
        wheel_root=Path("/var/lib/wheels"), environment_root=Path("/var/lib/env"),
        stage_root=BrokerPolicy().python_project_runtime_root,
        cache_policy=PythonProjectCachePolicy(
            max_bytes=16 * 1024**2, max_entries=4, disk_reserve_bytes=0, retention_seconds=3600,
        ),
        runtime_max_bytes=32 * 1024**2, wheel_max_bytes=32 * 1024**2,
        max_environment_bytes=1024**2,
    )


def invocation() -> PythonProjectProbeInvocation:
    return PythonProjectProbeInvocation(
        execution_id="supply_" + "1" * 32, tenant_id=UUID(int=1), run_id=UUID(int=2),
        project_id="project", session_id="session", sandbox_profile="workspace_write", timeout_seconds=30,
        original_reference=OriginalProjectReference(
            schema_version=1, input_source_sha256="1" * 64, contract_sha256="2" * 64, file_count=6,
        ),
    )


def plan() -> PythonInputSupplyPlan:
    probe = invocation()
    return PythonInputSupplyPlan(
        scope=PythonProjectScope(
            tenant_id=probe.tenant_id, run_id=probe.run_id, project_id=probe.project_id,
            session_id=probe.session_id, original_reference=probe.original_reference,
        ),
        declaration_sha256="5" * 64, manifest_sha256="6" * 64, lock_sha256="7" * 64,
        catalog_sha256="8" * 64,
        interpreter=PythonSupplyInterpreter(
            implementation="cpython", version="3.12.14", abi="cp312", platform="linux_x86_64",
            binary_sha256="9" * 64, base_runtime_sha256="a" * 64,
        ),
        artifacts=(PythonInputSupplyArtifact(
            filename="packaging-26.0-py3-none-any.whl", name="packaging", version="26.0",
            sha256="4" * 64, size_bytes=123,
        ),),
    )


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def json_object(raw: str | bytes) -> dict[str, object]:
    value: object = json.loads(raw)
    assert _is_object(value)
    return value
