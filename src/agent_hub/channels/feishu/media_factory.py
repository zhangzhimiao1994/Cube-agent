from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from agent_hub.channels.feishu.media import (
    FeishuMediaClient,
    FeishuMediaService,
    FeishuOpenAPIMediaClient,
)
from agent_hub.channels.feishu.settings import FeishuSettings
from agent_hub.config.schema import PlatformConfig
from agent_hub.models.capacity import CapacityPool, CredentialDescriptor, CredentialRegistry
from agent_hub.models.gateway import (
    GatewayCompletion,
    ModelGateway,
    ModelGatewayError,
    ModelTransport,
)
from agent_hub.models.litellm_client import LiteLLMClient
from agent_hub.models.registry import ModelRegistry
from agent_hub.models.types import Deployment, ModelRequest
from agent_hub.multimodal.images import FilesystemImageStore, MemoryImageStore
from agent_hub.multimodal.service import VisionService
from agent_hub.multimodal.types import ImageCleanupRecoveryItem, ImageObjectStore


class ConfigServiceProtocol(Protocol):
    async def get_current(self, tenant_id: UUID) -> object | None: ...


class LogServiceProtocol(Protocol):
    async def record_log(
        self,
        *,
        category: str,
        level: str,
        title: str,
        message: str,
        source: str,
        details: dict[str, str],
    ) -> object: ...


class SecretServiceProtocol(Protocol):
    async def resolve(self, tenant_id: UUID, reference: str) -> str: ...

    async def fingerprint(self, tenant_id: UUID, reference: str) -> str: ...


MediaClientFactory = Callable[[FeishuSettings], FeishuMediaClient]


class TenantSecretResolver:
    def __init__(self, secret_service: SecretServiceProtocol, tenant_id: UUID) -> None:
        self._secret_service = secret_service
        self._tenant_id = tenant_id

    async def resolve(self, secret_ref: str) -> str:
        return await self._secret_service.resolve(self._tenant_id, secret_ref)


class ConfigBackedVisionGateway:
    def __init__(
        self,
        *,
        config_service: ConfigServiceProtocol,
        secret_service: SecretServiceProtocol,
        redis_client: object,
        tenant_id: UUID,
        transport: ModelTransport | None = None,
    ) -> None:
        self._config_service = config_service
        self._secret_service = secret_service
        self._redis_client = redis_client
        self._tenant_id = tenant_id
        self._transport = transport or LiteLLMClient()

    async def complete_with_context(self, request: ModelRequest) -> GatewayCompletion:
        revision = await self._config_service.get_current(self._tenant_id)
        document = getattr(revision, "document", None)
        if document is None:
            raise ModelGatewayError("vision model is not configured")
        config = PlatformConfig.model_validate(document)
        deployments = _deployments(config)
        if not deployments:
            raise ModelGatewayError("vision model is not configured")
        gateway = ModelGateway(
            ModelRegistry(deployments),
            await self._capacity_pool(deployments),
            TenantSecretResolver(self._secret_service, self._tenant_id),
            self._transport,
            fallbacks=_fallbacks(config),
            capacity_wait_timeout=60,
        )
        return await gateway.complete_with_context(request)

    async def _capacity_pool(self, deployments: Sequence[Deployment]) -> CapacityPool:
        credentials = CredentialRegistry(
            [
                CredentialDescriptor(
                    secret_ref,
                    await self._secret_service.fingerprint(self._tenant_id, secret_ref),
                )
                for secret_ref in dict.fromkeys(
                    deployment.secret_ref for deployment in deployments
                )
            ]
        )
        return CapacityPool(
            self._redis_client,
            deployments=deployments,
            credentials=credentials,
        )


class AdminLogImageCleanupRecoverySink:
    def __init__(self, log_service: object | None) -> None:
        self._log_service = log_service

    async def enqueue(self, item: ImageCleanupRecoveryItem) -> None:
        recorder = getattr(self._log_service, "record_log", None)
        if recorder is None:
            return
        await cast(LogServiceProtocol, self._log_service).record_log(
            category="channel_error",
            level="error",
            title="Vision image cleanup recovery queued",
            message="vision image cleanup recovery queued",
            source="channels.feishu.media",
            details={
                "store_id": item.store_id,
                "namespace": item.namespace,
                "tenant_sha256": item.tenant_sha256,
                "object_key": item.object_key,
                "canonical_sha256": item.canonical_sha256,
                "reason": item.reason,
            },
        )


@dataclass(slots=True)
class FeishuMediaServiceFactory:
    analyzer: VisionService
    log_service: object | None
    media_client_factory: MediaClientFactory
    object_store: ImageObjectStore

    def __call__(self, settings: FeishuSettings) -> FeishuMediaService:
        return FeishuMediaService(
            client=self.media_client_factory(settings),
            analyzer=self.analyzer,
            log_service=self.log_service,
        )

    async def aclose(self) -> None:
        close = getattr(self.object_store, "close", None)
        if callable(close):
            close()


def build_feishu_media_service_factory(
    *,
    config_service: ConfigServiceProtocol,
    secret_service: SecretServiceProtocol,
    redis_client: object,
    tenant_id: UUID,
    attachment_store_dir: Path,
    log_service: object | None = None,
    environment: str = "production",
    transport: ModelTransport | None = None,
    media_client_factory: MediaClientFactory | None = None,
) -> FeishuMediaServiceFactory:
    object_store = _image_store(attachment_store_dir / "vision", environment=environment)
    analyzer = VisionService(
        ConfigBackedVisionGateway(
            config_service=config_service,
            secret_service=secret_service,
            redis_client=redis_client,
            tenant_id=tenant_id,
            transport=transport,
        ),
        object_store,
        cleanup_recovery_sink=AdminLogImageCleanupRecoverySink(log_service),
    )
    return FeishuMediaServiceFactory(
        analyzer=analyzer,
        log_service=log_service,
        media_client_factory=media_client_factory or _openapi_media_client,
        object_store=object_store,
    )


def _image_store(root: Path, *, environment: str) -> ImageObjectStore:
    if environment not in {"development", "test"} and os.name == "posix":
        return FilesystemImageStore(root)
    return MemoryImageStore(root, namespace="local")


def _openapi_media_client(settings: FeishuSettings) -> FeishuOpenAPIMediaClient:
    return FeishuOpenAPIMediaClient(settings=settings)


def _deployments(config: PlatformConfig) -> tuple[Deployment, ...]:
    deployments: list[Deployment] = []
    for logical_model, definition in sorted(config.models.items()):
        for index, deployment in enumerate(definition.deployments, start=1):
            deployments.append(
                deployment.to_deployment(
                    deployment_id=f"{logical_model}_{index}",
                    logical_model=logical_model,
                )
            )
    return tuple(deployments)


def _fallbacks(config: PlatformConfig) -> dict[str, str]:
    return {
        logical_model: definition.fallback_model
        for logical_model, definition in config.models.items()
        if definition.fallback_model is not None
    }


__all__ = [
    "AdminLogImageCleanupRecoverySink",
    "ConfigBackedVisionGateway",
    "FeishuMediaServiceFactory",
    "build_feishu_media_service_factory",
]
