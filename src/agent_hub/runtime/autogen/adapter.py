"""Legacy AutoGen SelectorGroupChat behind Agent Hub gateway and durable contracts.

This adapter remains for explicit compatibility.  Default Discuss runtime
selection belongs in ``agent_hub.runtime.discussion`` and prefers MAF, then AG2.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import unicodedata
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

import autogen_agentchat
import autogen_core
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import ChatAgent, TaskResult, Team
from autogen_agentchat.messages import BaseChatMessage
from autogen_agentchat.teams import SelectorGroupChat
from autogen_core import CancellationToken, FunctionCall, SingleThreadedAgentRuntime
from autogen_core.models import (
    AssistantMessage,
    ChatCompletionClient,
    CreateResult,
    FunctionExecutionResultMessage,
    LLMMessage,
    ModelCapabilities,
    ModelFamily,
    ModelInfo,
    RequestUsage,
    SystemMessage,
    UserMessage,
)
from autogen_core.tools import BaseTool, Tool, ToolSchema
from opentelemetry.trace import NoOpTracerProvider
from pydantic import BaseModel, ConfigDict

from agent_hub.auth.models import Role
from agent_hub.domain.runs import TaskMode
from agent_hub.harness import HarnessToolGateway
from agent_hub.harness.types import HarnessToolCallRequest, HarnessToolCallResult
from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from agent_hub.models.types import ToolCall as GatewayToolCall
from agent_hub.runtime.artifacts import (
    ArtifactReference,
    ArtifactRepository,
    ArtifactRepositoryError,
    InMemoryArtifactRepository,
)
from agent_hub.runtime.autogen.termination import CompositeDiscussionTermination, DiscussionUsage
from agent_hub.runtime.contracts import (
    Artifact,
    EventKind,
    JsonValue,
    RunEvent,
    RuntimeCheckpoint,
    TaskContext,
)
from agent_hub.runtime.failure_reason import (
    safe_model_gateway_failure_reason,
    safe_runtime_failure_reason,
)

_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_RUNTIME_TYPE = "autogen"
_RUNTIME_VERSION = "1"
_AUTOGEN_VERSION = "0.7.5"
_MAX_MESSAGES = 128
_MAX_TOOL_CALLS_PER_RESPONSE = 16
_MAX_TOOL_PAYLOAD_BYTES = 65_536
_MAX_SOURCE_ARTIFACT_TEXT_BYTES = 4_096
_ARTIFACT_CLEANUP_GRACE_SECONDS = 0.5
_AUTOGEN_STREAM_POLL_SECONDS = 1.0
_AUTOGEN_SHUTDOWN_GRACE_SECONDS = 2.0


def _truncate_prompt_text(value: str, *, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    suffix = f"\n\n[truncated: original_bytes={len(encoded)}]"
    suffix_bytes = suffix.encode("utf-8")
    if max_bytes <= len(suffix_bytes):
        return suffix_bytes[:max_bytes].decode("utf-8", errors="ignore")
    prefix = encoded[: max_bytes - len(suffix_bytes)].decode("utf-8", errors="ignore")
    return f"{prefix}{suffix}"


def _mutable_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(item) for item in value]
    return value


def _bounded_prompt_json(value: object, *, max_text_bytes: int) -> object:
    if isinstance(value, Mapping):
        return {
            key: _bounded_prompt_json(item, max_text_bytes=max_text_bytes)
            for key, item in value.items()
        }
    if isinstance(value, tuple | list):
        return [
            _bounded_prompt_json(item, max_text_bytes=max_text_bytes) for item in value
        ]
    if type(value) is str:
        return _truncate_prompt_text(value, max_bytes=max_text_bytes)
    return value


def _artifact_prompt_content(artifact: Artifact) -> object:
    return _bounded_prompt_json(
        artifact.content, max_text_bytes=_MAX_SOURCE_ARTIFACT_TEXT_BYTES
    )


def _artifact_text_preview(artifact: Artifact, *, max_bytes: int = 2_000) -> str | None:
    text = artifact.content.get("text")
    if type(text) is not str:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    return _truncate_prompt_text(stripped, max_bytes=max_bytes)


class RuntimeExecutionError(RuntimeError):
    """Stable discussion runtime failure."""


class RuntimeBusy(RuntimeExecutionError):
    """A runtime instance has one active stream."""


class UnaccountedUsage(RuntimeExecutionError):
    """A paid model result lacked trusted pricing."""


class ModelOutcomeUncertain(RuntimeExecutionError):
    """A previous model call may have completed but has no durable result."""


class ToolOutcomeUncertain(RuntimeExecutionError):
    """A restricted capability call may have caused an unrecoverable side effect."""


type _LedgerEntry = dict[str, JsonValue]
type _CheckpointPublisher = Callable[
    [tuple[Artifact, ...], tuple[_LedgerEntry, ...], tuple[_LedgerEntry, ...]],
    Awaitable[None],
]
type _ArtifactStorer = Callable[[Artifact], Awaitable[UUID]]
type _ArtifactAborter = Callable[[UUID], Awaitable[bool]]
type _ArtifactFinalizer = Callable[[UUID], None]


class _DiscussionDurability:
    """Small write-ahead ledgers whose large outcomes live only in Artifacts."""

    def __init__(
        self,
        publish: _CheckpointPublisher,
        store: _ArtifactStorer,
        abort: _ArtifactAborter,
        finalize: _ArtifactFinalizer,
        *,
        run_id: UUID,
        model_entries: tuple[_LedgerEntry, ...] = (),
        tool_entries: tuple[_LedgerEntry, ...] = (),
        artifacts: tuple[Artifact, ...] = (),
    ) -> None:
        self._publish = publish
        self._store = store
        self._abort = abort
        self._finalize = finalize
        self._context_run_id = run_id
        self.model_entries = [dict(entry) for entry in model_entries]
        self.tool_entries = [dict(entry) for entry in tool_entries]
        self._artifacts = {str(artifact.id): artifact for artifact in artifacts}
        self._model_cursor = 0
        self._tool_cursor = 0
        self._model_lock = asyncio.Lock()
        self.failure_reason: str | None = None

    @property
    def artifacts(self) -> tuple[Artifact, ...]:
        return tuple(self._artifacts.values())

    async def _checkpoint(self) -> None:
        await self._publish(
            self.artifacts,
            tuple(self.model_entries),
            tuple(self.tool_entries),
        )

    @staticmethod
    def request_hash(request: ModelRequest) -> str:
        payload = {
            "logical_model": request.logical_model,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            "capabilities": sorted(item.value for item in request.required_capabilities),
            "max_output_tokens": request.max_output_tokens,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    async def before_model(self, request: ModelRequest) -> GatewayCompletion | None:
        await self._model_lock.acquire()
        request_hash = self.request_hash(request)
        try:
            if self._model_cursor < len(self.model_entries):
                entry = self.model_entries[self._model_cursor]
                if entry.get("request_hash") != request_hash:
                    raise RuntimeExecutionError("durable model request mismatch")
                status = entry.get("status")
                if status == "running":
                    self.failure_reason = "model_outcome_uncertain"
                    raise ModelOutcomeUncertain("model outcome is uncertain")
                if status != "succeeded":
                    raise RuntimeExecutionError("durable model ledger is invalid")
                artifact_id = cast(str, entry["artifact_id"])
                artifact = self._artifacts.get(artifact_id)
                if artifact is None or artifact.content_sha256 != entry.get("artifact_sha256"):
                    raise RuntimeExecutionError("durable model result is unavailable")
                self._model_cursor += 1
                completion = self._completion_from_artifact(artifact)
                self._model_lock.release()
                return completion
            self.model_entries.append({"status": "running", "request_hash": request_hash})
            await self._checkpoint()
            return None
        except BaseException:
            if self._model_lock.locked():
                self._model_lock.release()
            raise

    async def succeed_model(self, request: ModelRequest, completion: GatewayCompletion) -> Artifact:
        entry = self.model_entries[self._model_cursor]
        if entry.get("status") != "running" or entry.get("request_hash") != self.request_hash(
            request
        ):
            self.fail_model()
            raise RuntimeExecutionError("durable model ledger mismatch")
        artifact = self._completion_artifact(completion)
        try:
            write_id = await self._store(artifact)
        except BaseException:
            self.fail_model()
            raise
        previous = dict(entry)
        self._artifacts[str(artifact.id)] = artifact
        entry.update(
            status="succeeded",
            artifact_id=str(artifact.id),
            artifact_sha256=artifact.content_sha256,
        )
        self._model_cursor += 1
        try:
            await self._checkpoint()
        except BaseException:
            self._model_cursor -= 1
            entry.clear()
            entry.update(previous)
            self._artifacts.pop(str(artifact.id), None)
            await self._abort(write_id)
            self.fail_model()
            raise
        self._finalize(write_id)
        self._model_lock.release()
        return artifact

    def fail_model(self, reason: str | None = None) -> None:
        if reason and self.failure_reason is None:
            self.failure_reason = reason
        if self._model_lock.locked():
            self._model_lock.release()

    async def before_tool(
        self,
        *,
        actor: str,
        name: str,
        arguments_hash: str,
        replay_safe: bool,
    ) -> tuple[Mapping[str, JsonValue] | None, str]:
        position = self._tool_cursor
        idempotency_key = hashlib.sha256(
            f"{self._context_run_id}:{actor}:{position}:{name}:{arguments_hash}".encode()
        ).hexdigest()
        identity: _LedgerEntry = {
            "actor": actor,
            "position": position,
            "name": name,
            "arguments_hash": arguments_hash,
            "idempotency_key": idempotency_key,
        }
        if self._tool_cursor < len(self.tool_entries):
            entry = self.tool_entries[self._tool_cursor]
            if any(entry.get(key) != value for key, value in identity.items()):
                raise RuntimeExecutionError("durable capability request mismatch")
            status = entry.get("status")
            if status == "running":
                if not replay_safe:
                    self.failure_reason = "tool_outcome_uncertain"
                    raise ToolOutcomeUncertain("capability outcome is uncertain")
                return None, idempotency_key
            if status != "succeeded":
                raise RuntimeExecutionError("durable capability ledger is invalid")
            artifact = self._artifacts.get(cast(str, entry["artifact_id"]))
            if artifact is None or artifact.content_sha256 != entry.get("artifact_sha256"):
                raise RuntimeExecutionError("durable capability result is unavailable")
            result = artifact.content.get("result")
            if not isinstance(result, Mapping):
                raise RuntimeExecutionError("durable capability result is invalid")
            self._tool_cursor += 1
            return result, idempotency_key
        self.tool_entries.append({"status": "running", "replay_safe": replay_safe, **identity})
        await self._checkpoint()
        return None, idempotency_key

    async def succeed_tool(self, artifact: Artifact) -> None:
        entry = self.tool_entries[self._tool_cursor]
        write_id = await self._store(artifact)
        previous = dict(entry)
        self._artifacts[str(artifact.id)] = artifact
        entry.update(
            status="succeeded",
            artifact_id=str(artifact.id),
            artifact_sha256=artifact.content_sha256,
        )
        self._tool_cursor += 1
        try:
            await self._checkpoint()
        except BaseException:
            self._tool_cursor -= 1
            entry.clear()
            entry.update(previous)
            self._artifacts.pop(str(artifact.id), None)
            await self._abort(write_id)
            raise
        self._finalize(write_id)

    async def compact(self) -> None:
        async with self._model_lock:
            self.model_entries.clear()
            self.tool_entries.clear()
            self._artifacts = {
                artifact_id: artifact
                for artifact_id, artifact in self._artifacts.items()
                if artifact.type == "tool_result"
            }
            self._model_cursor = 0
            self._tool_cursor = 0

    @staticmethod
    def _completion_artifact(completion: GatewayCompletion) -> Artifact:
        response = completion.response
        usage = response.usage
        if usage is None:
            raise UnaccountedUsage("model usage is unaccounted")
        cost_usd = completion.cost_usd if completion.cost_usd is not None else Decimal(0)
        return Artifact(
            id=uuid4(),
            type="model_result",
            producer="agent_hub",
            content={
                "text": response.text,
                "tool_calls": tuple(
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": dict(call.arguments),
                    }
                    for call in response.tool_calls
                ),
                "usage": {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                },
                "deployment_id": completion.deployment_id,
                "logical_model": completion.logical_model,
                "provider_id": completion.provider_id,
                "provider_model": completion.provider_model,
                "cost_usd": str(cost_usd),
            },
        )

    @staticmethod
    def _completion_from_artifact(artifact: Artifact) -> GatewayCompletion:
        content = artifact.content
        usage = cast(Mapping[str, JsonValue], content["usage"])
        calls = cast(tuple[JsonValue, ...], content["tool_calls"])
        return GatewayCompletion(
            response=ModelResponse(
                text=cast(str | None, content["text"]),
                tool_calls=tuple(
                    GatewayToolCall(
                        id=cast(str, cast(Mapping[str, JsonValue], item)["id"]),
                        name=cast(str, cast(Mapping[str, JsonValue], item)["name"]),
                        arguments=cast(
                            Mapping[str, JsonValue],
                            cast(Mapping[str, JsonValue], item)["arguments"],
                        ),
                    )
                    for item in calls
                ),
                usage=TokenUsage(
                    prompt_tokens=cast(int, usage["prompt_tokens"]),
                    completion_tokens=cast(int, usage["completion_tokens"]),
                    total_tokens=cast(int, usage["total_tokens"]),
                ),
            ),
            deployment_id=cast(str, content["deployment_id"]),
            logical_model=cast(str, content["logical_model"]),
            provider_id=cast(str, content["provider_id"]),
            provider_model=cast(str, content["provider_model"]),
            cost_usd=Decimal(cast(str, content["cost_usd"])),
        )


class ModelGateway(Protocol):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion: ...


class CapabilityGateway(Protocol):
    async def execute(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]: ...

    def is_replay_safe(self, name: str) -> bool: ...


class HarnessToolInvoker(Protocol):
    async def invoke(
        self,
        tenant_id: UUID,
        request: HarnessToolCallRequest,
        *,
        user_id: UUID | None = None,
        role: Role | None = None,
    ) -> HarnessToolCallResult: ...


class _CapabilityHarnessBackend:
    def __init__(self, gateway: CapabilityGateway) -> None:
        self._gateway = gateway

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        available = getattr(self._gateway, "is_available", None)
        if callable(available):
            return bool(available(tenant_id, name))
        return True

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
        return await self._gateway.execute(
            tenant_id=tenant_id,
            run_id=run_id,
            actor=actor,
            name=name,
            arguments=arguments,
            idempotency_key=idempotency_key,
        )


def _tool_sandbox(name: str) -> str:
    if name in {"read_context", "workspace_read", "workspace.read"}:
        return "read_only"
    if name in {"calculator", "calculator_evaluate", "calculator.evaluate"}:
        return "none"
    return "restricted"


def _safe_id(value: str, name: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a safe identifier")
    return value


def _safe_text(value: str, name: str, maximum: int = 16_384) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or len(value.encode()) > maximum
        or unicodedata.normalize("NFC", value) != value
    ):
        raise ValueError(f"{name} must be bounded normalized text")
    return value


@dataclass(frozen=True, slots=True)
class DiscussionParticipant:
    id: str
    role: str
    goal: str = field(repr=False)
    logical_model: str
    allowed_tools: tuple[str, ...] = ()
    max_output_tokens: int = 4096

    def __post_init__(self) -> None:
        _safe_id(self.id, "participant id")
        _safe_text(self.role, "participant role", 512)
        _safe_text(self.goal, "participant goal")
        _safe_id(self.logical_model, "logical model")
        tools = tuple(self.allowed_tools)
        if len(tools) > 32 or len(set(tools)) != len(tools):
            raise ValueError("participant tools must be unique and bounded")
        for tool in tools:
            _safe_id(tool, "tool")
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 1_000_000:
            raise ValueError("participant output limit is invalid")
        object.__setattr__(self, "allowed_tools", tools)


@dataclass(frozen=True, slots=True)
class DiscussionPlan:
    participants: tuple[DiscussionParticipant, ...]
    selector_model: str
    selector_max_output_tokens: int = 512
    max_turns: int = 16
    wall_time_seconds: float = 300.0
    token_budget: int = 65_536
    cost_budget_usd: Decimal = Decimal(10)
    consensus_votes: int = 2

    def __post_init__(self) -> None:
        participants = tuple(self.participants)
        if not 2 <= len(participants) <= 8:
            raise ValueError("discussion requires 2 to 8 participants")
        if not all(type(item) is DiscussionParticipant for item in participants):
            raise ValueError("participants are invalid")
        ids = [item.id for item in participants]
        if len(set(ids)) != len(ids):
            raise ValueError("participant ids must be unique")
        _safe_id(self.selector_model, "selector model")
        if (
            type(self.selector_max_output_tokens) is not int
            or not 1 <= self.selector_max_output_tokens <= 1_000_000
        ):
            raise ValueError("selector output limit is invalid")
        if type(self.max_turns) is not int or not 1 <= self.max_turns <= 64:
            raise ValueError("max turns is invalid")
        if (
            isinstance(self.wall_time_seconds, bool)
            or not isinstance(self.wall_time_seconds, int | float)
            or not math.isfinite(self.wall_time_seconds)
            or not 0 < self.wall_time_seconds <= 3600
        ):
            raise ValueError("wall time is invalid")
        if type(self.token_budget) is not int or not 1 <= self.token_budget <= 10_000_000:
            raise ValueError("token budget is invalid")
        if (
            type(self.cost_budget_usd) is not Decimal
            or not self.cost_budget_usd.is_finite()
            or self.cost_budget_usd <= 0
        ):
            raise ValueError("cost budget is invalid")
        if type(self.consensus_votes) is not int or not 2 <= self.consensus_votes <= len(
            participants
        ):
            raise ValueError("consensus votes is invalid")
        object.__setattr__(self, "participants", participants)

    @property
    def digest(self) -> str:
        payload = {
            "participants": [
                {
                    "id": item.id,
                    "role": item.role,
                    "goal": item.goal,
                    "logical_model": item.logical_model,
                    "allowed_tools": list(item.allowed_tools),
                    "max_output_tokens": item.max_output_tokens,
                }
                for item in self.participants
            ],
            "selector_model": self.selector_model,
            "selector_max_output_tokens": self.selector_max_output_tokens,
            "max_turns": self.max_turns,
            "wall_time_seconds": self.wall_time_seconds,
            "token_budget": self.token_budget,
            "cost_budget_usd": str(self.cost_budget_usd),
            "consensus_votes": self.consensus_votes,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class _DynamicToolArguments(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)


class _DynamicToolResult(BaseModel):
    model_config = ConfigDict(strict=True)
    result: dict[str, object]


@dataclass(frozen=True, slots=True)
class _ToolRecord:
    kind: EventKind
    actor: str
    call_id: str
    name: str
    artifact: Artifact | None = None
    reason: str | None = None


class GatewayCapabilityTool(BaseTool[_DynamicToolArguments, _DynamicToolResult]):
    """AutoGen tool facade with no execution path outside HarnessToolGateway."""

    def __init__(
        self,
        gateway: CapabilityGateway,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        records: list[_ToolRecord],
        durability: _DiscussionDurability,
        harness_tool_gateway: HarnessToolInvoker,
        user_id: UUID | None = None,
        role: Role | None = None,
    ) -> None:
        super().__init__(
            _DynamicToolArguments,
            _DynamicToolResult,
            name=name,
            description=f"Approved Agent Hub capability {name}",
        )
        self._gateway = gateway
        self._tenant_id = tenant_id
        self._run_id = run_id
        self._actor = actor
        self._records = records
        self._durability = durability
        self._tool_gateway = harness_tool_gateway
        self._user_id = user_id
        self._role = role

    async def run(
        self, args: _DynamicToolArguments, cancellation_token: CancellationToken
    ) -> _DynamicToolResult:
        return await self._execute(args, cancellation_token, call_id=None)

    async def run_json(
        self,
        args: Mapping[str, Any],
        cancellation_token: CancellationToken,
        call_id: str | None = None,
    ) -> Any:
        validated = _DynamicToolArguments.model_validate(args)
        return await self._execute(validated, cancellation_token, call_id=call_id)

    async def _execute(
        self,
        args: _DynamicToolArguments,
        cancellation_token: CancellationToken,
        *,
        call_id: str | None,
    ) -> _DynamicToolResult:
        safe_call_id = (
            call_id
            or hashlib.sha256(f"{self._run_id}:{self._actor}:{self.name}".encode()).hexdigest()
        )
        _safe_id(safe_call_id, "tool call id")
        arguments = dict(args.model_extra or {})
        try:
            encoded = json.dumps(
                arguments,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        except (TypeError, ValueError):
            raise RuntimeExecutionError("capability arguments are invalid") from None
        if len(encoded) > _MAX_TOOL_PAYLOAD_BYTES:
            raise RuntimeExecutionError("capability arguments exceed limit")
        arguments_hash = hashlib.sha256(encoded).hexdigest()
        # Resolve replay policy before publishing a started boundary or causing I/O.
        replay_safe = self._gateway.is_replay_safe(self.name)
        if type(replay_safe) is not bool:
            raise RuntimeExecutionError("capability replay policy is invalid")
        replayed, idempotency_key = await self._durability.before_tool(
            actor=self._actor,
            name=self.name,
            arguments_hash=arguments_hash,
            replay_safe=replay_safe,
        )
        self._records.append(
            _ToolRecord(EventKind.TOOL_STARTED, self._actor, safe_call_id, self.name)
        )
        if replayed is not None:
            artifact = Artifact(
                id=uuid4(),
                type="tool_result",
                producer=self._actor,
                content={"result": replayed},
            )
            self._records.append(
                _ToolRecord(
                    EventKind.TOOL_COMPLETED,
                    self._actor,
                    safe_call_id,
                    self.name,
                    artifact=artifact,
                )
            )
            return _DynamicToolResult(result=cast(dict[str, object], dict(replayed)))
        try:
            tool_request = HarnessToolCallRequest(
                run_id=self._run_id,
                actor=self._actor,
                tool_name=self.name,
                arguments=cast(Mapping[str, JsonValue], arguments),
                approval_required=False,
                sandbox=_tool_sandbox(self.name),
                idempotency_key=idempotency_key,
                call_id=safe_call_id,
            )
        except (TypeError, ValueError):
            self._records.append(
                _ToolRecord(
                    EventKind.TOOL_FAILED,
                    self._actor,
                    safe_call_id,
                    self.name,
                    reason="capability execution failed",
                )
            )
            raise RuntimeExecutionError("capability execution failed") from None
        task = asyncio.create_task(
            self._tool_gateway.invoke(
                self._tenant_id,
                tool_request,
                user_id=self._user_id,
                role=self._role,
            )
        )
        cancellation_token.link_future(task)
        try:
            tool_result = await task
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - hostile capability boundary
            error.__traceback__ = None
            del error
            self._records.append(
                _ToolRecord(
                    EventKind.TOOL_FAILED,
                    self._actor,
                    safe_call_id,
                    self.name,
                    reason="capability execution failed",
                )
            )
            raise RuntimeExecutionError("capability outcome is uncertain") from None
        if tool_result.status != "succeeded":
            self._records.append(
                _ToolRecord(
                    EventKind.TOOL_FAILED,
                    self._actor,
                    safe_call_id,
                    self.name,
                    reason=tool_result.failure_reason or "capability execution failed",
                )
            )
            raise RuntimeExecutionError("capability execution failed") from None
        artifact = Artifact(
            id=uuid4(),
            type="tool_result",
            producer=self._actor,
            content={"result": tool_result.payload},
        )
        self._records.append(
            _ToolRecord(
                EventKind.TOOL_COMPLETED,
                self._actor,
                safe_call_id,
                self.name,
                artifact=artifact,
            )
        )
        await self._durability.succeed_tool(artifact)
        return _DynamicToolResult(result=cast(dict[str, object], dict(tool_result.payload)))


class GatewayChatCompletionClient(ChatCompletionClient):
    """Exact AutoGen 0.7.5 model interface backed only by ModelGateway."""

    def __init__(
        self,
        gateway: ModelGateway,
        logical_model: str,
        usage: DiscussionUsage,
        *,
        max_output_tokens: int = 4096,
        supports_tool_calls: bool = False,
        durability: _DiscussionDurability | None = None,
    ) -> None:
        self._gateway = gateway
        self._logical_model = logical_model
        self._usage = usage
        self._max_output_tokens = max_output_tokens
        self._supports_tool_calls = supports_tool_calls
        self._durability = durability
        self._actual = RequestUsage(prompt_tokens=0, completion_tokens=0)
        self._total = RequestUsage(prompt_tokens=0, completion_tokens=0)

    async def create(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = (),
        tool_choice: Tool | Literal["auto", "required", "none"] = "auto",
        json_output: bool | type[Any] | None = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: CancellationToken | None = None,
    ) -> CreateResult:
        del tool_choice, extra_create_args
        if json_output not in {None, False}:
            raise RuntimeExecutionError("unsupported AutoGen model request")
        normalized = tuple(self._message(message) for message in messages)
        if not normalized or len(normalized) > _MAX_MESSAGES:
            raise RuntimeExecutionError("AutoGen message history is invalid")
        request = ModelRequest(
            logical_model=self._logical_model,
            messages=normalized,
            required_capabilities=frozenset(
                {ModelCapability.TEXT, ModelCapability.TOOL_CALLING}
                if tools
                else {ModelCapability.TEXT}
            ),
            max_output_tokens=self._max_output_tokens,
        )
        completion = (
            await self._durability.before_model(request) if self._durability is not None else None
        )
        replayed = completion is not None
        if completion is None:
            task = asyncio.create_task(self._gateway.complete_with_context(request))
            if cancellation_token is not None:
                cancellation_token.link_future(task)
            try:
                completion = await task
            except asyncio.CancelledError:
                if self._durability is not None:
                    self._durability.fail_model()
                raise
            except Exception as error:
                if self._durability is not None:
                    self._durability.fail_model(safe_model_gateway_failure_reason(error))
                raise
        response = completion.response
        if response.usage is None:
            if not replayed and self._durability is not None:
                self._durability.fail_model("unaccounted_usage")
            raise UnaccountedUsage("model usage is unaccounted")
        cost_usd = completion.cost_usd if completion.cost_usd is not None else Decimal(0)
        if len(response.tool_calls) > _MAX_TOOL_CALLS_PER_RESPONSE:
            if not replayed and self._durability is not None:
                self._durability.fail_model("discussion_failed")
            raise RuntimeExecutionError("model tool call limit exceeded")
        if response.text is not None and response.tool_calls:
            if not replayed and self._durability is not None:
                self._durability.fail_model("discussion_failed")
            raise RuntimeExecutionError("model response is invalid")
        if response.text is None and not response.tool_calls:
            if not replayed and self._durability is not None:
                self._durability.fail_model("discussion_failed")
            raise RuntimeExecutionError("model response is invalid")
        usage = RequestUsage(
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
        )
        self._actual = usage
        self._total = RequestUsage(
            prompt_tokens=self._total.prompt_tokens + usage.prompt_tokens,
            completion_tokens=self._total.completion_tokens + usage.completion_tokens,
        )
        if not replayed:
            self._usage.tokens += response.usage.total_tokens
            self._usage.cost_usd += cost_usd
        available_tools = {
            item.name if isinstance(item, BaseTool) else cast(ToolSchema, item)["name"]
            for item in tools
        }
        if any(call.name not in available_tools for call in response.tool_calls):
            if not replayed and self._durability is not None:
                self._durability.fail_model("discussion_failed")
            raise RuntimeExecutionError("model requested an unavailable capability")
        if not replayed and self._durability is not None:
            await self._durability.succeed_model(request, completion)
        content: str | list[FunctionCall]
        finish_reason: Literal["stop", "function_calls"]
        if response.tool_calls:
            content = [
                FunctionCall(
                    id=call.id,
                    name=call.name,
                    arguments=json.dumps(
                        dict(call.arguments),
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                for call in response.tool_calls
            ]
            finish_reason = "function_calls"
        elif response.text is not None:
            content = response.text
            finish_reason = "stop"
        else:
            raise RuntimeExecutionError("model response is invalid")
        return CreateResult(
            finish_reason=finish_reason,
            content=content,
            usage=usage,
            cached=False,
            thought=None,
        )

    def create_stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = (),
        tool_choice: Tool | Literal["auto", "required", "none"] = "auto",
        json_output: bool | type[Any] | None = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: CancellationToken | None = None,
    ) -> AsyncGenerator[str | CreateResult, None]:
        async def generate() -> AsyncGenerator[str | CreateResult, None]:
            yield await self.create(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                json_output=json_output,
                extra_create_args=extra_create_args,
                cancellation_token=cancellation_token,
            )

        return generate()

    @staticmethod
    def _message(message: LLMMessage) -> ModelMessage:
        if isinstance(message, SystemMessage):
            return ModelMessage(role="system", content=message.content)
        if isinstance(message, UserMessage):
            if type(message.content) is not str:
                raise RuntimeExecutionError("multimodal discussion input is unsupported")
            return ModelMessage(role="user", content=message.content)
        if isinstance(message, AssistantMessage):
            if type(message.content) is str:
                return ModelMessage(role="assistant", content=message.content)
            framework_calls = cast(list[FunctionCall], message.content)
            calls = [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in framework_calls
            ]
            return ModelMessage(
                role="assistant",
                content="EXPLICIT_CAPABILITY_CALLS_JSON="
                + json.dumps(calls, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        if isinstance(message, FunctionExecutionResultMessage):
            results = [
                {
                    "call_id": result.call_id,
                    "name": result.name,
                    "content": result.content,
                    "is_error": result.is_error,
                }
                for result in message.content
            ]
            return ModelMessage(
                role="user",
                content="UNTRUSTED_CAPABILITY_RESULTS_JSON="
                + json.dumps(results, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        raise RuntimeExecutionError("unknown AutoGen message type")

    async def close(self) -> None:
        return None

    def actual_usage(self) -> RequestUsage:
        return self._actual

    def total_usage(self) -> RequestUsage:
        return self._total

    def count_tokens(
        self, messages: Sequence[LLMMessage], *, tools: Sequence[Tool | ToolSchema] = ()
    ) -> int:
        return min(
            1_000_000,
            sum(len(str(message.content).encode()) for message in messages) + len(tools) * 64,
        )

    def remaining_tokens(
        self, messages: Sequence[LLMMessage], *, tools: Sequence[Tool | ToolSchema] = ()
    ) -> int:
        return max(0, 1_000_000 - self.count_tokens(messages, tools=tools))

    @property
    def capabilities(self) -> ModelCapabilities:
        return {
            "vision": False,
            "function_calling": self._supports_tool_calls,
            "json_output": False,
        }

    @property
    def model_info(self) -> ModelInfo:
        return {
            "vision": False,
            "function_calling": self._supports_tool_calls,
            "json_output": False,
            "structured_output": False,
            "family": ModelFamily.UNKNOWN,
        }


class AutoGenDiscussionRuntime:
    mode = TaskMode.DISCUSS

    def __init__(
        self,
        gateway: ModelGateway,
        plan: DiscussionPlan,
        *,
        capability_gateway: CapabilityGateway | None = None,
        harness_tool_gateway: HarnessToolInvoker | None = None,
        artifact_repository: ArtifactRepository | None = None,
    ) -> None:
        if (
            autogen_agentchat.__version__ != _AUTOGEN_VERSION
            or autogen_core.__version__ != _AUTOGEN_VERSION
        ):
            raise RuntimeExecutionError("AutoGen runtime version is incompatible")
        self._gateway = gateway
        self._plan = plan
        if (
            any(participant.allowed_tools for participant in plan.participants)
            and capability_gateway is None
        ):
            raise ValueError("configured discussion tools require CapabilityGateway")
        self._capabilities = capability_gateway
        self._tool_gateway = harness_tool_gateway
        if self._tool_gateway is None and capability_gateway is not None:
            self._tool_gateway = HarnessToolGateway(
                _CapabilityHarnessBackend(capability_gateway),
                require_actor_identity=True,
                raise_backend_errors=True,
            )
        self._repository = artifact_repository or InMemoryArtifactRepository()
        self._active_task: asyncio.Task[Any] | None = None
        self._cancel_token: CancellationToken | None = None
        self._cancel_event: asyncio.Event | None = None
        self._last_checkpoint: RuntimeCheckpoint | None = None
        self._restored: RuntimeCheckpoint | None = None
        self._pending_artifact_writes: dict[UUID, ArtifactReference] = {}
        self._cleanup_tasks: set[asyncio.Task[Any]] = set()
        self.last_team: SelectorGroupChat | None = None

    @property
    def participant_ids(self) -> tuple[str, ...]:
        return tuple(participant.id for participant in self._plan.participants)

    def run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        if self._active_task is not None:
            raise RuntimeBusy("runtime is busy")
        if type(context) is not TaskContext or context.mode is not self.mode:
            raise RuntimeExecutionError("runtime context is invalid")
        return self._run(context)

    async def _run(self, context: TaskContext) -> AsyncIterator[RunEvent]:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeExecutionError("runtime task is unavailable")
        self._active_task = current
        self._cancel_token = CancellationToken()
        self._cancel_event = asyncio.Event()
        sequence = 1
        message_artifacts: list[Artifact] = []
        usage = DiscussionUsage()
        durability: _DiscussionDurability | None = None
        wall_expired = asyncio.Event()
        wall_handle: asyncio.TimerHandle | None = None
        try:
            restored = self._restored
            ledger_artifacts: tuple[Artifact, ...] = ()
            if restored is not None:
                self._validate_checkpoint(restored, context)
                if context.checkpoint is None or context.checkpoint.id != restored.id:
                    raise RuntimeExecutionError("runtime checkpoint mismatch")
                hydrated = await self._hydrate_checkpoint(restored, context)
                message_artifacts = [artifact for artifact in hydrated if artifact.type == "text"]
                ledger_artifacts = tuple(
                    artifact for artifact in hydrated if artifact.type != "text"
                )
                sequence = cast(int, restored.state["next_sequence"])
                if restored.state["terminal"] is True:
                    restored_reason = cast(str, restored.state["reason"])
                    yield RunEvent(
                        kind=EventKind.RUNTIME_COMPLETED,
                        sequence=sequence,
                        run_id=context.run_id,
                        reason=restored_reason,
                    )
                    return
            elif context.checkpoint is not None:
                raise RuntimeExecutionError("runtime checkpoint was not restored")

            yield RunEvent(
                kind=EventKind.DISCUSSION_STARTED,
                sequence=sequence,
                run_id=context.run_id,
                actor=self._plan.participants[0].id,
                session_id=str(context.run_id),
                participants=tuple(item.id for item in self._plan.participants),
                inputs=context.artifacts,
            )
            sequence += 1
            usage = self._restored_usage(restored)

            async def publish_ledger_checkpoint(
                durable_artifacts: tuple[Artifact, ...],
                model_entries: tuple[_LedgerEntry, ...],
                tool_entries: tuple[_LedgerEntry, ...],
            ) -> None:
                checkpoint = self._checkpoint(
                    context,
                    next_sequence=sequence,
                    terminal=False,
                    reason=None,
                    artifacts=tuple(message_artifacts) + durable_artifacts,
                    usage=usage,
                    model_ledger=model_entries,
                    tool_ledger=tool_entries,
                )
                self._last_checkpoint = checkpoint

            async def store_ledger_artifact(artifact: Artifact) -> UUID:
                return await self._store_artifact(context, artifact)

            async def abort_ledger_artifact(write_id: UUID) -> bool:
                return await self._abort_artifact_write(context, write_id)

            def finalize_ledger_artifact(write_id: UUID) -> None:
                self._pending_artifact_writes.pop(write_id, None)

            durability = _DiscussionDurability(
                publish_ledger_checkpoint,
                store_ledger_artifact,
                abort_ledger_artifact,
                finalize_ledger_artifact,
                run_id=context.run_id,
                model_entries=self._restored_ledger(restored, "model_ledger"),
                tool_entries=self._restored_ledger(restored, "tool_ledger"),
                artifacts=ledger_artifacts,
            )
            cancelled = lambda: (
                self._cancel_token is None
                or (self._cancel_token.is_cancelled() and not wall_expired.is_set())
            )
            termination = CompositeDiscussionTermination(
                usage=usage,
                max_turns=self._plan.max_turns,
                token_budget=min(self._plan.token_budget, context.token_budget),
                cost_budget_usd=self._plan.cost_budget_usd,
                wall_time_seconds=min(self._plan.wall_time_seconds, context.timeout_seconds),
                consensus_votes=self._plan.consensus_votes,
                cancelled=cancelled,
            )
            clients = {
                participant.id: GatewayChatCompletionClient(
                    self._gateway,
                    participant.logical_model,
                    usage,
                    max_output_tokens=participant.max_output_tokens,
                    supports_tool_calls=bool(participant.allowed_tools),
                    durability=durability,
                )
                for participant in self._plan.participants
            }
            participant_models = {
                participant.id: participant.logical_model
                for participant in self._plan.participants
            }
            selector = GatewayChatCompletionClient(
                self._gateway,
                self._plan.selector_model,
                usage,
                max_output_tokens=self._plan.selector_max_output_tokens,
                durability=durability,
            )
            tool_records: list[_ToolRecord] = []

            def drain_tool_records() -> tuple[RunEvent, ...]:
                nonlocal sequence
                events: list[RunEvent] = []
                while tool_records:
                    record = tool_records.pop(0)
                    events.append(
                        RunEvent(
                            kind=record.kind,
                            sequence=sequence,
                            run_id=context.run_id,
                            actor=record.actor,
                            tool_call_id=record.call_id,
                            tool_name=record.name,
                            artifact=record.artifact,
                            reason=record.reason,
                        )
                    )
                    sequence += 1
                return tuple(events)

            agents: list[ChatAgent | Team] = [
                cast(
                    ChatAgent,
                    AssistantAgent(
                        participant.id,
                        model_client=clients[participant.id],
                        tools=(
                            [
                                GatewayCapabilityTool(
                                    cast(CapabilityGateway, self._capabilities),
                                    tenant_id=context.tenant_id,
                                    run_id=context.run_id,
                                    actor=participant.id,
                                    name=name,
                                    records=tool_records,
                                    durability=durability,
                                    harness_tool_gateway=cast(
                                        HarnessToolInvoker, self._tool_gateway
                                    ),
                                    user_id=context.actor_id,
                                    role=context.actor_role,
                                )
                                for name in participant.allowed_tools
                            ]
                            or None
                        ),
                        description=f"{participant.role}: {participant.goal}",
                        system_message=(
                            f"Role: {participant.role}. Goal: {participant.goal}. "
                            "Return only explicit conclusions and citations. Never reveal hidden reasoning. "
                            "Use [CONSENSUS] to vote, or [COMPLETE] only for a finished answer."
                        ),
                    ),
                )
                for participant in self._plan.participants
            ]
            framework_runtime = SingleThreadedAgentRuntime(tracer_provider=NoOpTracerProvider())
            team = SelectorGroupChat(
                agents,
                selector,
                termination_condition=termination,
                max_turns=self._plan.max_turns,
                # Keep selection model-mediated even with two participants; policy still
                # constrains candidates through the published participant list.
                allow_repeated_speaker=True,
                runtime=framework_runtime,
            )
            self.last_team = team
            source_ids = tuple(str(item.id) for item in (*context.artifacts, *message_artifacts))
            reason: str | None = None
            wall_seconds = min(self._plan.wall_time_seconds, context.timeout_seconds)

            def expire_wall_time() -> None:
                wall_expired.set()
                if self._cancel_token is not None:
                    self._cancel_token.cancel()
                if self._cancel_event is not None:
                    self._cancel_event.set()

            wall_handle = asyncio.get_running_loop().call_later(wall_seconds, expire_wall_time)
            framework_runtime.start()
            framework_failed = False
            framework_timed_out = False
            try:
                stream = team.run_stream(
                    task=self._task_text(context, tuple(message_artifacts)),
                    cancellation_token=self._cancel_token,
                )
                deadline = asyncio.get_running_loop().time() + wall_seconds
                while True:
                    item_task: asyncio.Task[Any] = asyncio.create_task(anext(stream))
                    while not item_task.done():
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            framework_timed_out = True
                            expire_wall_time()
                            item_task.cancel()
                            done, pending = await asyncio.wait(
                                (item_task,),
                                timeout=_AUTOGEN_SHUTDOWN_GRACE_SECONDS,
                            )
                            if pending:
                                self._cleanup_tasks.add(item_task)
                                item_task.add_done_callback(self._finish_cleanup_task)
                            else:
                                for done_task in done:
                                    try:
                                        done_task.result()
                                    except BaseException as error:  # noqa: BLE001 - timeout identity wins
                                        error.__traceback__ = None
                                        del error
                            break
                        await asyncio.wait(
                            (item_task,),
                            timeout=min(_AUTOGEN_STREAM_POLL_SECONDS, remaining),
                        )
                    if framework_timed_out:
                        reason = "wall_time"
                        break
                    try:
                        item = item_task.result()
                    except StopAsyncIteration:
                        break
                    except RuntimeExecutionError:
                        for event in drain_tool_records():
                            yield event
                        raise
                    drained_tool_events = drain_tool_records()
                    for event in drained_tool_events:
                        yield event
                    if any(event.kind is EventKind.TOOL_FAILED for event in drained_tool_events):
                        raise RuntimeExecutionError("capability execution failed") from None
                    if isinstance(item, BaseChatMessage) and item.source != "user":
                        text = item.to_text()
                        artifact = Artifact(
                            id=uuid4(),
                            type="text",
                            producer=item.source,
                            content={"text": text},
                            source_ids=source_ids,
                        )
                        write_id = await self._store_artifact(context, artifact)
                        message_artifacts.append(artifact)
                        try:
                            await durability.compact()
                            checkpoint = self._checkpoint(
                                context,
                                next_sequence=sequence + 3,
                                terminal=False,
                                reason=None,
                                artifacts=tuple(message_artifacts) + durability.artifacts,
                                usage=usage,
                            )
                        except BaseException:
                            message_artifacts.pop()
                            await self._abort_artifact_write(context, write_id)
                            raise
                        self._last_checkpoint = checkpoint
                        self._pending_artifact_writes.pop(write_id, None)
                        yield RunEvent(
                            kind=EventKind.MESSAGE_CREATED,
                            sequence=sequence,
                            run_id=context.run_id,
                            actor=item.source,
                            session_id=str(context.run_id),
                            message=text,
                            inputs=context.artifacts,
                            payload={
                                "logical_model": participant_models.get(item.source, ""),
                                "role_message": _truncate_prompt_text(text, max_bytes=2_000),
                            },
                        )
                        sequence += 1
                        yield RunEvent(
                            kind=EventKind.ARTIFACT_CREATED,
                            sequence=sequence,
                            run_id=context.run_id,
                            artifact=artifact,
                        )
                        sequence += 1
                        yield RunEvent(
                            kind=EventKind.CHECKPOINT_SAVED,
                            sequence=sequence,
                            run_id=context.run_id,
                            checkpoint=checkpoint,
                        )
                        sequence += 1
                        if _discussion_has_enough_distinct_outputs(
                            message_artifacts,
                            participant_count=len(self._plan.participants),
                            consensus_votes=self._plan.consensus_votes,
                        ):
                            reason = "sufficient_discussion"
                            if self._cancel_token is not None:
                                self._cancel_token.cancel()
                            break
                    elif isinstance(item, TaskResult):
                        reason = termination.reason or self._normalize_stop_reason(item.stop_reason)
            except BaseException:
                framework_failed = True
                raise
            finally:
                cleanup_failed = False
                if not await self._await_autogen_cleanup(team.reset()):
                    cleanup_failed = True
                if not await self._await_autogen_cleanup(framework_runtime.stop_when_idle()):
                    cleanup_failed = True
                if not await self._await_autogen_cleanup(framework_runtime.close()):
                    cleanup_failed = True
                if _should_fail_on_autogen_cleanup(
                    cleanup_failed=cleanup_failed,
                    framework_failed=framework_failed,
                    framework_timed_out=framework_timed_out,
                    message_artifacts=message_artifacts,
                ):
                    raise RuntimeExecutionError("AutoGen runtime cleanup failed") from None
            if cleanup_failed and not framework_failed and not framework_timed_out:
                yield RunEvent(
                    kind="runtime.cleanup_degraded",
                    sequence=sequence,
                    run_id=context.run_id,
                    payload={
                        "phase": "autogen_shutdown",
                        "summary": "AutoGen runtime cleanup failed after usable discussion output.",
                    },
                )
                sequence += 1
            if wall_expired.is_set():
                reason = "wall_time"
            reason = reason or termination.reason or "max_turns"
            last_discussion = next(
                (
                    preview
                    for preview in (
                        _artifact_text_preview(artifact)
                        for artifact in reversed(message_artifacts)
                    )
                    if preview
                ),
                "讨论结束，但没有可公开的阶段摘要。",
            )
            yield RunEvent(
                kind="discussion.completed",
                sequence=sequence,
                run_id=context.run_id,
                payload={
                    "participants": tuple(participant.id for participant in self._plan.participants),
                    "participant_models": participant_models,
                    "summary": last_discussion,
                    "reason": reason,
                },
            )
            sequence += 1
            checkpoint = self._checkpoint(
                context,
                next_sequence=sequence + 1,
                terminal=True,
                reason=reason,
                artifacts=tuple(message_artifacts) + durability.artifacts,
                usage=usage,
                model_ledger=(tuple(durability.model_entries) if durability is not None else ()),
                tool_ledger=(tuple(durability.tool_entries) if durability is not None else ()),
            )
            self._last_checkpoint = checkpoint
            yield RunEvent(
                kind=EventKind.CHECKPOINT_SAVED,
                sequence=sequence,
                run_id=context.run_id,
                checkpoint=checkpoint,
            )
            sequence += 1
            yield RunEvent(
                kind=EventKind.RUNTIME_COMPLETED,
                sequence=sequence,
                run_id=context.run_id,
                reason=reason,
            )
        except UnaccountedUsage:
            yield RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=sequence,
                run_id=context.run_id,
                reason="unaccounted_usage",
            )
        except ModelOutcomeUncertain:
            yield RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=sequence,
                run_id=context.run_id,
                reason="model_outcome_uncertain",
            )
        except ToolOutcomeUncertain:
            yield RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=sequence,
                run_id=context.run_id,
                reason="tool_outcome_uncertain",
            )
        except asyncio.CancelledError:
            if wall_expired.is_set():
                checkpoint = self._checkpoint(
                    context,
                    next_sequence=sequence + 2,
                    terminal=True,
                    reason="wall_time",
                    artifacts=tuple(message_artifacts)
                    + (durability.artifacts if durability is not None else ()),
                    usage=usage,
                    model_ledger=(
                        tuple(durability.model_entries) if durability is not None else ()
                    ),
                    tool_ledger=(tuple(durability.tool_entries) if durability is not None else ()),
                )
                self._last_checkpoint = checkpoint
                yield RunEvent(
                    kind=EventKind.CHECKPOINT_SAVED,
                    sequence=sequence,
                    run_id=context.run_id,
                    checkpoint=checkpoint,
                )
                sequence += 1
                yield RunEvent(
                    kind=EventKind.RUNTIME_COMPLETED,
                    sequence=sequence,
                    run_id=context.run_id,
                    reason="wall_time",
                )
                return
            try:
                checkpoint = self._checkpoint(
                    context,
                    next_sequence=sequence + 2,
                    terminal=True,
                    reason="cancelled",
                    artifacts=tuple(message_artifacts)
                    + (durability.artifacts if durability is not None else ()),
                    usage=usage,
                    model_ledger=(
                        tuple(durability.model_entries) if durability is not None else ()
                    ),
                    tool_ledger=(tuple(durability.tool_entries) if durability is not None else ()),
                )
                self._last_checkpoint = checkpoint
                yield RunEvent(
                    kind=EventKind.CHECKPOINT_SAVED,
                    sequence=sequence,
                    run_id=context.run_id,
                    checkpoint=checkpoint,
                )
                sequence += 1
            except Exception as error:  # noqa: BLE001 - cancellation identity wins
                error.__traceback__ = None
                del error
            yield RunEvent(
                kind=EventKind.RUNTIME_CANCELLED,
                sequence=sequence,
                run_id=context.run_id,
            )
            raise
        except Exception as error:  # noqa: BLE001 - AutoGen wraps participant failures
            failure_reason = (
                durability.failure_reason
                if durability is not None and durability.failure_reason is not None
                else safe_runtime_failure_reason(error, fallback="discussion_failed")
            )
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
            del error
            if _can_complete_with_partial_discussion(message_artifacts, failure_reason):
                checkpoint = self._checkpoint(
                    context,
                    next_sequence=sequence + 2,
                    terminal=True,
                    reason="partial_discussion_after_model_failure",
                    artifacts=tuple(message_artifacts)
                    + (durability.artifacts if durability is not None else ()),
                    usage=usage,
                    model_ledger=(
                        tuple(durability.model_entries) if durability is not None else ()
                    ),
                    tool_ledger=(tuple(durability.tool_entries) if durability is not None else ()),
                )
                self._last_checkpoint = checkpoint
                yield RunEvent(
                    kind=EventKind.CHECKPOINT_SAVED,
                    sequence=sequence,
                    run_id=context.run_id,
                    checkpoint=checkpoint,
                )
                sequence += 1
                yield RunEvent(
                    kind=EventKind.RUNTIME_COMPLETED,
                    sequence=sequence,
                    run_id=context.run_id,
                    reason="partial_discussion_after_model_failure",
                )
                return
            yield RunEvent(
                kind=EventKind.RUNTIME_FAILED,
                sequence=sequence,
                run_id=context.run_id,
                reason=failure_reason,
            )
        finally:
            if wall_handle is not None:
                wall_handle.cancel()
            self._active_task = None
            self._cancel_token = None
            self._cancel_event = None

    @staticmethod
    def _task_text(context: TaskContext, transcript: tuple[Artifact, ...] = ()) -> str:
        artifacts = "\n".join(
            f"Artifact {item.id} ({item.type}, {item.producer}): {json.dumps(_artifact_prompt_content(item), ensure_ascii=False, sort_keys=True)}"
            for item in context.artifacts
        )
        task = (
            context.request
            if not artifacts
            else f"{context.request}\n\nValidated artifacts:\n{artifacts}"
        )
        prior = "\n".join(
            f"{item.producer}: {cast(str, item.content['text'])}" for item in transcript
        )
        return task if not prior else f"{task}\n\nPrior explicit transcript:\n{prior}"

    @staticmethod
    def _normalize_stop_reason(reason: str | None) -> str:
        if reason in {
            "explicit_completion",
            "consensus",
            "budget_exhausted",
            "wall_time",
            "cancelled",
            "max_turns",
        }:
            return reason
        if reason and "maximum number of turns" in reason.casefold():
            return "max_turns"
        return "discussion_completed"

    async def _store_artifact(self, context: TaskContext, artifact: Artifact) -> UUID:
        reference = ArtifactReference(id=artifact.id, sha256=artifact.content_sha256)
        write_id = uuid4()
        self._pending_artifact_writes[write_id] = reference

        async def commit() -> None:
            await self._repository.reserve_write(
                context.tenant_id,
                context.run_id,
                reference,
                write_id=write_id,
            )
            await self._repository.put(
                context.tenant_id,
                context.run_id,
                artifact,
                write_id=write_id,
            )
            resolved = await self._repository.get_many(
                context.tenant_id, context.run_id, (reference,)
            )
            if resolved != (artifact,):
                raise ArtifactRepositoryError("artifact repository verification failed")

        task = asyncio.create_task(commit())
        cancel_event = self._cancel_event
        if cancel_event is None:
            task.cancel()
            raise asyncio.CancelledError
        cancel_wait = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait((task, cancel_wait), return_when=asyncio.FIRST_COMPLETED)
            if cancel_wait in done:
                raise asyncio.CancelledError
            task.result()
        except BaseException:
            original_error = task.exception() if task.done() and not task.cancelled() else None
            rollback_detail = (
                safe_runtime_failure_reason(original_error, fallback="artifact write failed")
                if isinstance(original_error, Exception)
                else "artifact write failed"
            )
            # Fence before cancelling: a late writer must observe the abort tombstone,
            # or its already-written artifact must be removed by the same abort.
            abort_succeeded = await self._abort_artifact_write(context, write_id)
            task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=_ARTIFACT_CLEANUP_GRACE_SECONDS
                )
            except BaseException as error:  # noqa: BLE001 - detached task is fenced
                error.__traceback__ = None
                del error
            if not task.done():
                self._cleanup_tasks.add(task)
                task.add_done_callback(self._finish_cleanup_task)
            if not abort_succeeded:
                raise RuntimeExecutionError(
                    f"artifact rollback failed after {rollback_detail}"
                ) from None
            raise
        finally:
            cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)
        return write_id

    async def _abort_artifact_write(self, context: TaskContext, write_id: UUID) -> bool:
        reference = self._pending_artifact_writes.get(write_id)
        if reference is None:
            return True
        task = asyncio.create_task(
            self._repository.abort_write(
                context.tenant_id,
                context.run_id,
                reference,
                write_id=write_id,
            )
        )
        try:
            result = await asyncio.wait_for(
                asyncio.shield(task), timeout=_ARTIFACT_CLEANUP_GRACE_SECONDS
            )
        except BaseException as error:  # noqa: BLE001 - cleanup is fail closed
            error.__traceback__ = None
            del error
            if not task.done():
                task.cancel()
                self._cleanup_tasks.add(task)
                task.add_done_callback(self._finish_cleanup_task)
            return False
        self._pending_artifact_writes.pop(write_id, None)
        return result

    def _finish_cleanup_task(self, task: asyncio.Task[Any]) -> None:
        self._cleanup_tasks.discard(task)
        try:
            task.result()
        except BaseException as error:  # noqa: BLE001 - detached task is consumed
            error.__traceback__ = None
            del error

    async def _await_autogen_cleanup(self, awaitable: Coroutine[Any, Any, Any]) -> bool:
        task = asyncio.create_task(awaitable)
        done, pending = await asyncio.wait(
            (task,),
            timeout=_AUTOGEN_SHUTDOWN_GRACE_SECONDS,
        )
        if pending:
            task.cancel()
            done_after_cancel, pending_after_cancel = await asyncio.wait(
                (task,),
                timeout=_AUTOGEN_SHUTDOWN_GRACE_SECONDS,
            )
            if pending_after_cancel:
                self._cleanup_tasks.add(task)
                task.add_done_callback(self._finish_cleanup_task)
                return False
            done = done_after_cancel
        for done_task in done:
            try:
                done_task.result()
            except BaseException as error:  # noqa: BLE001 - cleanup failure is reported safely
                error.__traceback__ = None
                del error
                return False
        return True

    def _checkpoint(
        self,
        context: TaskContext,
        *,
        next_sequence: int,
        terminal: bool,
        reason: str | None,
        artifacts: tuple[Artifact, ...],
        usage: DiscussionUsage,
        model_ledger: tuple[_LedgerEntry, ...] = (),
        tool_ledger: tuple[_LedgerEntry, ...] = (),
    ) -> RuntimeCheckpoint:
        return RuntimeCheckpoint(
            id=uuid4(),
            runtime_type=_RUNTIME_TYPE,
            runtime_version=_RUNTIME_VERSION,
            run_id=context.run_id,
            tenant_id=context.tenant_id,
            mode=self.mode,
            state={
                "plan_digest": self._plan.digest,
                "autogen_version": _AUTOGEN_VERSION,
                "terminal": terminal,
                "reason": reason,
                "next_sequence": next_sequence,
                "artifact_registry": {str(item.id): item.content_sha256 for item in artifacts},
                "usage": {"tokens": usage.tokens, "cost_usd": str(usage.cost_usd)},
                "model_ledger": model_ledger,
                "tool_ledger": tool_ledger,
            },
        )

    def _validate_checkpoint(self, checkpoint: RuntimeCheckpoint, context: TaskContext) -> None:
        if (
            checkpoint.runtime_type != _RUNTIME_TYPE
            or checkpoint.runtime_version != _RUNTIME_VERSION
            or checkpoint.mode is not self.mode
            or checkpoint.run_id != context.run_id
            or checkpoint.tenant_id != context.tenant_id
            or checkpoint.state_sha256 != checkpoint.recompute_state_sha256()
            or checkpoint.state.get("plan_digest") != self._plan.digest
            or checkpoint.state.get("autogen_version") != _AUTOGEN_VERSION
        ):
            raise RuntimeExecutionError("runtime checkpoint is incompatible")
        state = checkpoint.state
        if set(state) != {
            "plan_digest",
            "autogen_version",
            "terminal",
            "reason",
            "next_sequence",
            "artifact_registry",
            "usage",
            "model_ledger",
            "tool_ledger",
        }:
            raise RuntimeExecutionError("runtime checkpoint is incompatible")
        registry = state["artifact_registry"]
        usage = state["usage"]
        model_ledger = state["model_ledger"]
        tool_ledger = state["tool_ledger"]
        if (
            type(state["terminal"]) is not bool
            or (state["reason"] is not None and type(state["reason"]) is not str)
            or type(state["next_sequence"]) is not int
            or not 1 <= state["next_sequence"] < 2**63
            or not isinstance(registry, Mapping)
            or len(registry) > 64
            or not isinstance(usage, Mapping)
            or set(usage) != {"tokens", "cost_usd"}
            or type(usage["tokens"]) is not int
            or not isinstance(model_ledger, tuple)
            or not isinstance(tool_ledger, tuple)
            or len(model_ledger) > 64
            or len(tool_ledger) > 64
            or any(not isinstance(entry, Mapping) for entry in (*model_ledger, *tool_ledger))
        ):
            raise RuntimeExecutionError("runtime checkpoint is incompatible")
        try:
            cost = Decimal(cast(str, usage["cost_usd"]))
            for artifact_id, sha256 in registry.items():
                if type(artifact_id) is not str or type(sha256) is not str:
                    raise ValueError
                UUID(artifact_id)
                if re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
                    raise ValueError
        except (ArithmeticError, TypeError, ValueError):
            raise RuntimeExecutionError("runtime checkpoint is incompatible") from None
        if (
            not cost.is_finite()
            or cost < 0
            or usage["tokens"] < 0
            or (state["terminal"] is True and type(state["reason"]) is not str)
        ):
            raise RuntimeExecutionError("runtime checkpoint is incompatible")

    async def _hydrate_checkpoint(
        self, checkpoint: RuntimeCheckpoint, context: TaskContext
    ) -> tuple[Artifact, ...]:
        registry = cast(Mapping[str, str], checkpoint.state["artifact_registry"])
        references = tuple(
            ArtifactReference(id=UUID(artifact_id), sha256=sha256)
            for artifact_id, sha256 in registry.items()
        )
        try:
            artifacts = await self._repository.get_many(
                context.tenant_id, context.run_id, references
            )
        except ArtifactRepositoryError:
            raise RuntimeExecutionError("runtime checkpoint artifacts are unavailable") from None
        participants = set(self.participant_ids)
        if any(
            artifact.type not in {"text", "model_result", "tool_result"}
            or (artifact.type in {"text", "tool_result"} and artifact.producer not in participants)
            or (artifact.type == "model_result" and artifact.producer != "agent_hub")
            or artifact.content_sha256 != registry[str(artifact.id)]
            or artifact.recompute_content_sha256() != artifact.content_sha256
            for artifact in artifacts
        ):
            raise RuntimeExecutionError("runtime checkpoint artifacts are invalid")
        return artifacts

    @staticmethod
    def _restored_ledger(
        checkpoint: RuntimeCheckpoint | None, name: str
    ) -> tuple[_LedgerEntry, ...]:
        if checkpoint is None:
            return ()
        raw = cast(tuple[JsonValue, ...], checkpoint.state[name])
        return tuple(dict(cast(Mapping[str, JsonValue], entry)) for entry in raw)

    @staticmethod
    def _restored_usage(checkpoint: RuntimeCheckpoint | None) -> DiscussionUsage:
        if checkpoint is None:
            return DiscussionUsage()
        usage = cast(Mapping[str, JsonValue], checkpoint.state["usage"])
        return DiscussionUsage(
            tokens=cast(int, usage["tokens"]),
            cost_usd=Decimal(cast(str, usage["cost_usd"])),
        )

    async def save_checkpoint(self) -> RuntimeCheckpoint:
        if self._last_checkpoint is None:
            raise RuntimeExecutionError("runtime has no checkpoint")
        return self._last_checkpoint

    async def restore_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        if self._active_task is not None or type(checkpoint) is not RuntimeCheckpoint:
            raise RuntimeExecutionError("runtime checkpoint is incompatible")
        validated = RuntimeCheckpoint.from_payload(checkpoint.to_payload())
        if (
            validated.runtime_type != _RUNTIME_TYPE
            or validated.runtime_version != _RUNTIME_VERSION
            or validated.mode is not self.mode
        ):
            raise RuntimeExecutionError("runtime checkpoint is incompatible")
        self._restored = validated
        self._last_checkpoint = validated

    async def cancel(self) -> None:
        token = self._cancel_token
        task = self._active_task
        if token is not None:
            token.cancel()
        cancel_event = self._cancel_event
        if cancel_event is not None:
            cancel_event.set()
        if task is not None and task is not asyncio.current_task():
            task.cancel()


def _can_complete_with_partial_discussion(
    message_artifacts: Sequence[Artifact],
    failure_reason: str,
) -> bool:
    if not message_artifacts:
        return False
    if not failure_reason.startswith("model gateway failed"):
        return False
    return any(
        isinstance(artifact.content, Mapping)
        and isinstance(artifact.content.get("text"), str)
        and bool(cast(str, artifact.content["text"]).strip())
        for artifact in message_artifacts
    )


def _discussion_has_enough_distinct_outputs(
    message_artifacts: Sequence[Artifact],
    *,
    participant_count: int,
    consensus_votes: int,
) -> bool:
    if participant_count < 3:
        return False
    required = min(participant_count, max(2, consensus_votes + 1))
    speakers: set[str] = set()
    for artifact in message_artifacts:
        if not isinstance(artifact.content, Mapping):
            continue
        text = artifact.content.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        if artifact.producer:
            speakers.add(artifact.producer)
    return len(speakers) >= required


def _should_fail_on_autogen_cleanup(
    *,
    cleanup_failed: bool,
    framework_failed: bool,
    framework_timed_out: bool,
    message_artifacts: Sequence[Artifact],
) -> bool:
    if not cleanup_failed or framework_failed or framework_timed_out:
        return False
    return not any(
        isinstance(artifact.content, Mapping)
        and isinstance(artifact.content.get("text"), str)
        and bool(cast(str, artifact.content["text"]).strip())
        for artifact in message_artifacts
    )


__all__ = [
    "AutoGenDiscussionRuntime",
    "DiscussionParticipant",
    "DiscussionPlan",
    "GatewayChatCompletionClient",
    "RuntimeBusy",
    "RuntimeExecutionError",
]
