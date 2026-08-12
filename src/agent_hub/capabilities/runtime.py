from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import UUID

from agent_hub.capabilities.tools.calculator import Calculator
from agent_hub.capabilities.tools.workspace_read import WorkspaceReader
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.base import SkillInvocation, SkillSandbox
from agent_hub.skills.sandbox.systemd import SystemdSkillSandbox

_SAFE_CAPABILITY_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_REPLAY_SAFE = frozenset({
    "calculator",
    "calculator_evaluate",
    "read_context",
    "workspace_read",
})


class RuntimeCapabilityError(RuntimeError):
    """Stable runtime capability failure."""


class RuntimeCapabilityGateway:
    """Production capability executor for non-dangerous built-ins and approved skills."""

    def __init__(
        self,
        *,
        skill_store_dir: Path,
        workspace_root: Path | None = None,
        skill_sandbox: SkillSandbox | None = None,
        calculator: Calculator | None = None,
    ) -> None:
        self._skill_store_dir = skill_store_dir
        self._workspace_root = workspace_root
        self._skill_sandbox = skill_sandbox or SystemdSkillSandbox()
        self._calculator = calculator or Calculator()

    def is_replay_safe(self, name: str) -> bool:
        return name in _REPLAY_SAFE

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        if name in _REPLAY_SAFE:
            return True
        if _SAFE_CAPABILITY_NAME.fullmatch(name) is None:
            return False
        return self._skill_package_path(tenant_id, name).is_file()

    async def execute(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        _require_safe("actor", actor)
        _require_safe("capability name", name)
        _require_safe("idempotency key", idempotency_key, max_length=160)
        if name in {"calculator", "calculator_evaluate"}:
            return self._execute_calculator(arguments)
        if name == "read_context":
            return self._execute_read_context(arguments)
        if name == "workspace_read":
            return self._execute_workspace_read(arguments)
        return await self._execute_skill(
            tenant_id=tenant_id,
            run_id=run_id,
            actor=actor,
            skill_id=name,
            arguments=arguments,
            idempotency_key=idempotency_key,
        )

    def _execute_calculator(self, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        expression = arguments.get("expression")
        if not isinstance(expression, str):
            raise RuntimeCapabilityError("calculator requires expression")
        result = self._calculator.evaluate(expression)
        return {"value": str(result.value)}

    def _execute_read_context(self, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        path = arguments.get("path")
        if isinstance(path, str):
            return self._execute_workspace_read(arguments)
        query = arguments.get("query")
        if query is None:
            query = arguments.get("text")
        if query is not None and (not isinstance(query, str) or not query.strip()):
            raise RuntimeCapabilityError("read_context query must be a nonblank string")
        return {
            "query": query.strip() if isinstance(query, str) else None,
            "matches": (),
            "summary": "No additional runtime context is available for this query.",
            "truncated": False,
        }

    def _execute_workspace_read(self, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        if self._workspace_root is None:
            raise RuntimeCapabilityError("workspace reader is not configured")
        path = arguments.get("path")
        if not isinstance(path, str):
            raise RuntimeCapabilityError("workspace reader requires path")
        result = WorkspaceReader(self._workspace_root).read(path)
        return {
            "path": result.relative_path,
            "text": result.text,
            "truncated": result.truncated,
        }

    async def _execute_skill(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        skill_id: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        package_path = self._skill_package_path(tenant_id, skill_id)
        if not package_path.is_file():
            raise RuntimeCapabilityError("skill is not installed or approved")
        archive_bytes = package_path.read_bytes()
        package_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        execution_id = _execution_id(actor, skill_id, idempotency_key)
        writable_tmp_path = self._skill_store_dir / str(tenant_id) / "tmp" / execution_id
        writable_tmp_path.mkdir(parents=True, exist_ok=True)
        try:
            result = await self._skill_sandbox.run(
                SkillInvocation(
                    execution_id=execution_id,
                    package_path=package_path,
                    package_sha256=package_sha256,
                    input={
                        "run_id": str(run_id),
                        "actor": actor,
                        "skill": skill_id,
                        "arguments": _json_dict(arguments),
                    },
                    timeout_seconds=300,
                    output_limit_bytes=1_000_000,
                    memory_limit_bytes=512 * 1024 * 1024,
                    cpu_quota_percent=100,
                    writable_tmp_path=writable_tmp_path,
                )
            )
        finally:
            shutil.rmtree(writable_tmp_path, ignore_errors=True)
        if result.timed_out:
            raise RuntimeCapabilityError("skill execution timed out")
        if result.exit_code != 0:
            raise RuntimeCapabilityError("skill execution failed")
        parsed = _parse_stdout(result.stdout)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "result": parsed,
        }

    def _skill_package_path(self, tenant_id: UUID, skill_id: str) -> Path:
        root = (self._skill_store_dir / str(tenant_id)).resolve()
        target = (root / f"{skill_id}.zip").resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise RuntimeCapabilityError("skill path is invalid") from None
        return target


def _require_safe(name: str, value: str, *, max_length: int = 128) -> None:
    if (
        not isinstance(value, str)
        or len(value) > max_length
        or _SAFE_CAPABILITY_NAME.fullmatch(value) is None
    ):
        raise RuntimeCapabilityError(f"{name} is invalid")


def _execution_id(actor: str, skill_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{actor}:{skill_id}:{idempotency_key}".encode()).hexdigest()[:24]
    return f"skill_{digest}"


def _json_dict(value: Mapping[str, JsonValue]) -> dict[str, object]:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):
        raise RuntimeCapabilityError("capability arguments are invalid")
    return parsed


def _parse_stdout(value: str) -> JsonValue:
    if not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    return cast(JsonValue, parsed)
