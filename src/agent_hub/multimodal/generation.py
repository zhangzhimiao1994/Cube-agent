from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from agent_hub.models.gateway import GatewayCompletion
from agent_hub.models.types import ModelCapability, ModelMessage, ModelRequest


class MultimediaGenerationKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


class MultimediaGenerationJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MultimediaGenerationResult:
    kind: MultimediaGenerationKind
    logical_model: str
    deployment_id: str
    text: str | None


@dataclass(frozen=True, slots=True)
class MultimediaArtifact:
    kind: MultimediaGenerationKind
    uri: str | None
    text: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not MultimediaGenerationKind:
            raise ValueError("artifact kind is invalid")
        if self.uri is not None and (type(self.uri) is not str or not self.uri.strip()):
            raise ValueError("artifact uri must be nonblank when provided")
        if self.text is not None and type(self.text) is not str:
            raise ValueError("artifact text must be a string or None")


@dataclass(frozen=True, slots=True)
class MultimediaGenerationJob:
    id: str
    kind: MultimediaGenerationKind
    logical_model: str
    prompt: str
    status: MultimediaGenerationJobStatus
    artifacts: tuple[MultimediaArtifact, ...] = ()
    executor_id: str | None = None
    error: str | None = None


class InMemoryMultimediaGenerationJobStore:
    """Small process-local job inbox for async multimedia executor handoff."""

    def __init__(self) -> None:
        self._jobs: dict[str, MultimediaGenerationJob] = {}

    def create(
        self,
        *,
        kind: MultimediaGenerationKind,
        logical_model: str,
        prompt: str,
    ) -> MultimediaGenerationJob:
        job = MultimediaGenerationJob(
            id=f"media_{uuid4().hex}",
            kind=kind,
            logical_model=logical_model,
            prompt=prompt,
            status=MultimediaGenerationJobStatus.QUEUED,
        )
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> MultimediaGenerationJob:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise KeyError(f"unknown multimedia generation job: {job_id}") from None

    def start(self, job_id: str, *, executor_id: str) -> MultimediaGenerationJob:
        job = self.get(job_id)
        if job.status is not MultimediaGenerationJobStatus.QUEUED:
            raise RuntimeError("multimedia generation job is not queued")
        running = MultimediaGenerationJob(
            id=job.id,
            kind=job.kind,
            logical_model=job.logical_model,
            prompt=job.prompt,
            status=MultimediaGenerationJobStatus.RUNNING,
            artifacts=job.artifacts,
            executor_id=_validate_executor_id(executor_id),
            error=None,
        )
        self._jobs[job_id] = running
        return running

    def succeed(
        self,
        job_id: str,
        *,
        artifacts: tuple[MultimediaArtifact, ...],
    ) -> MultimediaGenerationJob:
        job = self.get(job_id)
        succeeded = MultimediaGenerationJob(
            id=job.id,
            kind=job.kind,
            logical_model=job.logical_model,
            prompt=job.prompt,
            status=MultimediaGenerationJobStatus.SUCCEEDED,
            artifacts=tuple(artifacts),
            executor_id=job.executor_id,
            error=None,
        )
        self._jobs[job_id] = succeeded
        return succeeded

    def fail(self, job_id: str, *, error: str) -> MultimediaGenerationJob:
        job = self.get(job_id)
        failed = MultimediaGenerationJob(
            id=job.id,
            kind=job.kind,
            logical_model=job.logical_model,
            prompt=job.prompt,
            status=MultimediaGenerationJobStatus.FAILED,
            artifacts=job.artifacts,
            executor_id=job.executor_id,
            error=error[:512],
        )
        self._jobs[job_id] = failed
        return failed


class MultimediaDailyLimitExceeded(RuntimeError):
    """Raised before dispatch when a configured daily generation cap is exhausted."""


class MultimediaGenerationGateway(Protocol):
    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion: ...


class MultimediaGenerationExecutor:
    """Controlled generation entry point with capability checks at model dispatch."""

    def __init__(
        self,
        gateway: MultimediaGenerationGateway,
        *,
        daily_limit: int | None = None,
        today: Callable[[], date] = date.today,
        job_store: InMemoryMultimediaGenerationJobStore | None = None,
    ) -> None:
        if daily_limit is not None and (type(daily_limit) is not int or daily_limit <= 0):
            raise ValueError("daily_limit must be a positive integer or None")
        self._gateway = gateway
        self._daily_limit = daily_limit
        self._today = today
        self._usage_day: date | None = None
        self._daily_count = 0
        self._job_store = job_store or InMemoryMultimediaGenerationJobStore()

    def submit(
        self,
        *,
        kind: MultimediaGenerationKind,
        logical_model: str,
        prompt: str,
    ) -> MultimediaGenerationJob:
        prompt = _validate_generation_input(kind=kind, prompt=prompt)
        return self._job_store.create(
            kind=kind,
            logical_model=logical_model,
            prompt=prompt,
        )

    def get_job(self, job_id: str) -> MultimediaGenerationJob:
        return self._job_store.get(job_id)

    async def run_job(
        self,
        job_id: str,
        *,
        executor_id: str,
    ) -> MultimediaGenerationJob:
        job = self._job_store.start(job_id, executor_id=executor_id)
        try:
            result = await self.generate(
                kind=job.kind,
                logical_model=job.logical_model,
                prompt=job.prompt,
            )
        except Exception as error:
            self._job_store.fail(job_id, error=str(error))
            raise
        artifact = MultimediaArtifact(
            kind=result.kind,
            uri=result.text,
            text=result.text,
        )
        return self._job_store.succeed(job_id, artifacts=(artifact,))

    async def generate(
        self,
        *,
        kind: MultimediaGenerationKind,
        logical_model: str,
        prompt: str,
    ) -> MultimediaGenerationResult:
        prompt = _validate_generation_input(kind=kind, prompt=prompt)
        self._claim_daily_slot()
        request = ModelRequest(
            logical_model=logical_model,
            messages=(ModelMessage(role="user", content=prompt),),
            required_capabilities=frozenset({_required_capability(kind)}),
            max_output_tokens=4096,
        )
        completion = await self._gateway.complete_with_context(request)
        return MultimediaGenerationResult(
            kind=kind,
            logical_model=completion.logical_model,
            deployment_id=completion.deployment_id,
            text=completion.response.text,
        )

    def _claim_daily_slot(self) -> None:
        if self._daily_limit is None:
            return
        today = self._today()
        if self._usage_day != today:
            self._usage_day = today
            self._daily_count = 0
        if self._daily_count >= self._daily_limit:
            raise MultimediaDailyLimitExceeded("daily multimedia generation limit exceeded")
        self._daily_count += 1


def _required_capability(kind: MultimediaGenerationKind) -> ModelCapability:
    if kind is MultimediaGenerationKind.IMAGE:
        return ModelCapability.IMAGE_GENERATION
    if kind is MultimediaGenerationKind.VIDEO:
        return ModelCapability.VIDEO_GENERATION
    if kind is MultimediaGenerationKind.AUDIO:
        return ModelCapability.AUDIO_GENERATION
    raise ValueError("generation kind is invalid")


def _validate_generation_input(*, kind: MultimediaGenerationKind, prompt: str) -> str:
    if type(kind) is not MultimediaGenerationKind:
        raise ValueError("generation kind is invalid")
    if type(prompt) is not str or not prompt.strip():
        raise ValueError("generation prompt must be nonblank")
    return prompt.strip()


def _validate_executor_id(executor_id: str) -> str:
    if type(executor_id) is not str or not executor_id.strip() or len(executor_id) > 128:
        raise ValueError("executor_id must be a nonblank bounded string")
    return executor_id.strip()


__all__ = [
    "InMemoryMultimediaGenerationJobStore",
    "MultimediaArtifact",
    "MultimediaDailyLimitExceeded",
    "MultimediaGenerationExecutor",
    "MultimediaGenerationGateway",
    "MultimediaGenerationJob",
    "MultimediaGenerationJobStatus",
    "MultimediaGenerationKind",
    "MultimediaGenerationResult",
]
