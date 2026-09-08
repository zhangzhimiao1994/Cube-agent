from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from jsonschema import ValidationError  # type: ignore[import-untyped]
from jsonschema.protocols import Validator  # type: ignore[import-untyped]

from agent_hub.api.routers.admin import PluginCapabilityRequest, PluginResourceResponse
from agent_hub.auth.models import Role
from agent_hub.capabilities.policy import CapabilityRule
from agent_hub.capabilities.runtime import RuntimeCapabilityError
from agent_hub.capabilities.tools.registry import PluginConfigCapabilityManifestSource
from agent_hub.capabilities.types import PolicyEffect
from agent_hub.plugins.schemas import PluginSchemaError, plugin_schema_validator
from agent_hub.runtime.contracts import JsonValue, _mutable_json


class PluginConfigService(Protocol):
    async def list_plugins(self) -> Sequence[Any]: ...


class PluginAuditRecorder(Protocol):
    async def record_audit_event(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        details: dict[str, object] | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class PluginInvocationContext:
    tenant_id: UUID
    user_id: UUID
    run_id: UUID
    actor: str
    idempotency_key: str


class PluginAdapter(Protocol):
    async def invoke(
        self,
        *,
        plugin: PluginResourceResponse,
        capability: PluginCapabilityRequest,
        arguments: Mapping[str, JsonValue],
        context: PluginInvocationContext,
    ) -> Mapping[str, JsonValue]: ...


type HttpJsonPost = Callable[
    [str, Mapping[str, JsonValue], float, Mapping[str, str]],
    Awaitable[Mapping[str, JsonValue]],
]
type PluginSecretResolver = Callable[[str], Awaitable[str]]


class HttpJsonPluginAdapter:
    def __init__(
        self,
        *,
        post_json: HttpJsonPost | None = None,
        secret_resolver: PluginSecretResolver | None = None,
    ) -> None:
        self._post_json = _httpx_post_json if post_json is None else post_json
        self._secret_resolver = secret_resolver

    async def invoke(
        self,
        *,
        plugin: PluginResourceResponse,
        capability: PluginCapabilityRequest,
        arguments: Mapping[str, JsonValue],
        context: PluginInvocationContext,
    ) -> Mapping[str, JsonValue]:
        url = _plugin_endpoint_url(plugin)
        if not _endpoint_domain_allowed(url, plugin.domain_allowlist):
            raise RuntimeCapabilityError("Plugin endpoint not allowed")
        payload: Mapping[str, JsonValue] = {
            "plugin_id": plugin.id,
            "capability_id": capability.id,
            "arguments": arguments,
            "resource_config": plugin.resource_config,
            "capability_config": capability.capability_config,
            "context": {
                "tenant_id": str(context.tenant_id),
                "user_id": str(context.user_id),
                "run_id": str(context.run_id),
                "actor": context.actor,
                "idempotency_key": context.idempotency_key,
            },
        }
        json_payload = cast(Mapping[str, JsonValue], _mutable_json(cast(JsonValue, payload)))
        headers = await self._headers_for_plugin(plugin)
        try:
            result = await self._post_json(url, json_payload, plugin.timeout_seconds, headers)
        except TimeoutError as error:
            raise RuntimeCapabilityError("Plugin tool timed out") from error
        except httpx.TimeoutException as error:
            raise RuntimeCapabilityError("Plugin tool timed out") from error
        except RuntimeCapabilityError:
            raise
        except Exception as error:
            raise RuntimeCapabilityError("Plugin tool failed") from error
        if not isinstance(result, Mapping):
            raise RuntimeCapabilityError("Plugin result is invalid")
        return result

    async def _headers_for_plugin(self, plugin: PluginResourceResponse) -> Mapping[str, str]:
        credential_ref = plugin.credential_ref
        if credential_ref is None:
            return {}
        if self._secret_resolver is None:
            raise RuntimeCapabilityError("Plugin credential unavailable")
        try:
            credential = await self._secret_resolver(credential_ref)
        except Exception as error:
            raise RuntimeCapabilityError("Plugin credential unavailable") from error
        if not isinstance(credential, str) or not credential:
            raise RuntimeCapabilityError("Plugin credential unavailable")
        scheme = plugin.credential_scheme.strip()
        value = credential if not scheme else f"{scheme} {credential}"
        return {plugin.credential_header: value}

    def descriptor(self) -> Mapping[str, JsonValue]:
        return _http_json_adapter_descriptor()


class RuntimePluginService:
    def __init__(
        self,
        *,
        tenant_id: UUID,
        admin_service: PluginConfigService,
        adapters: Mapping[str, PluginAdapter] | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._admin_service = admin_service
        self._adapters = _default_plugin_adapters(admin_service)
        self._adapters.update(adapters or {})
        self._plugins: tuple[PluginResourceResponse, ...] = ()

    async def start(self) -> None:
        await self.reload(self._tenant_id)

    async def reload(self, tenant_id: UUID | None = None) -> None:
        if tenant_id is not None and tenant_id != self._tenant_id:
            return
        try:
            self._plugins = tuple(
                PluginResourceResponse.model_validate(plugin)
                for plugin in await self._admin_service.list_plugins()
            )
        except Exception:  # noqa: BLE001 - plugin runtime context must fail closed.
            self._plugins = ()

    def capability_manifest_source(self) -> RuntimePluginService:
        return self

    def manifests_for_tenant(self, tenant_id: UUID) -> Mapping[str, JsonValue]:
        if tenant_id != self._tenant_id:
            return _empty_manifest()
        return PluginConfigCapabilityManifestSource(cast(Any, self._plugins)).manifests()

    def adapter_descriptors(self) -> tuple[Mapping[str, JsonValue], ...]:
        return tuple(
            _adapter_descriptor(adapter_id, adapter)
            for adapter_id, adapter in sorted(self._adapters.items())
        )

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        if tenant_id != self._tenant_id:
            return False
        return self._available_plugin_capability(name) is not None

    def capability_policy_parts(self, tenant_id: UUID, name: str) -> tuple[str, str, str] | None:
        if tenant_id != self._tenant_id:
            return None
        target = self._available_plugin_capability(name)
        if target is None:
            return None
        plugin, capability = target
        return _plugin_capability_policy_parts(plugin, capability)

    def capability_policy_rules(self, tenant_id: UUID) -> tuple[CapabilityRule, ...]:
        if tenant_id != self._tenant_id:
            return ()
        rules: list[CapabilityRule] = []
        for plugin in self._plugins:
            if not _plugin_is_running(plugin):
                continue
            for capability in plugin.capabilities:
                effect = _plugin_policy_effect(capability.policy_effect)
                if effect is None:
                    continue
                parts = _plugin_capability_policy_parts(plugin, capability)
                if parts is None:
                    continue
                policy_capability, policy_operation, resource_prefix = parts
                for role in _PLUGIN_POLICY_ROLES:
                    rules.append(
                        CapabilityRule(
                            tenant_id=tenant_id,
                            role=role,
                            agent_id=None,
                            capability=policy_capability,
                            operation=policy_operation,
                            resource_prefix=resource_prefix,
                            effect=effect,
                        )
                    )
        return tuple(rules)

    async def invoke(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        if tenant_id != self._tenant_id:
            raise RuntimeCapabilityError("Plugin tool unavailable")
        target = self._available_plugin_capability(name)
        if target is None:
            raise RuntimeCapabilityError("Plugin tool unavailable")
        plugin, capability = target
        context = PluginInvocationContext(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            actor=actor,
            idempotency_key=idempotency_key,
        )
        try:
            _ensure_supported_plugin_sandbox_profile(capability.sandbox_profile)
            adapter = self._adapters.get(capability.adapter)
            if adapter is None:
                raise RuntimeCapabilityError("Plugin backend unavailable")
            argument_validator = _plugin_schema_validator(
                schema=_adapter_argument_schema(capability.adapter, adapter),
                invalid_schema_message="Plugin adapter argument schema is invalid",
            )
            input_validator = _plugin_schema_validator(
                schema=capability.input_schema,
                invalid_schema_message="Plugin input schema is invalid",
            )
            output_validator = _plugin_schema_validator(
                schema=capability.output_schema,
                invalid_schema_message="Plugin output schema is invalid",
            )
            _validate_plugin_payload(
                payload=arguments,
                validator=argument_validator,
                validation_message="Plugin arguments do not match adapter schema",
                include_location=True,
            )
            _validate_plugin_payload(
                payload=arguments,
                validator=input_validator,
                validation_message="Plugin arguments do not match input schema",
                include_location=True,
            )
            result = await adapter.invoke(
                plugin=plugin,
                capability=capability,
                arguments=arguments,
                context=context,
            )
            _validate_plugin_payload(
                payload=result,
                validator=output_validator,
                validation_message="Plugin result does not match output schema",
                include_location=False,
            )
        except Exception:
            await self._record_invocation_audit(
                plugin,
                capability,
                context,
                action="plugin.invoke.failed",
            )
            raise
        await self._record_invocation_audit(
            plugin,
            capability,
            context,
            action="plugin.invoke.succeeded",
        )
        return result

    def _available_plugin_capability(
        self,
        name: str,
    ) -> tuple[PluginResourceResponse, PluginCapabilityRequest] | None:
        for plugin in self._plugins:
            if not _plugin_is_running(plugin):
                continue
            for capability in plugin.capabilities:
                if capability.id == name or name in capability.aliases:
                    return plugin, capability
        return None

    async def _record_invocation_audit(
        self,
        plugin: PluginResourceResponse,
        capability: PluginCapabilityRequest,
        context: PluginInvocationContext,
        *,
        action: str,
    ) -> None:
        recorder = getattr(self._admin_service, "record_audit_event", None)
        if not callable(recorder):
            return
        try:
            await cast(PluginAuditRecorder, self._admin_service).record_audit_event(
                actor=context.actor,
                action=action,
                resource=f"plugin:{plugin.id}:{capability.id}",
                details={
                    "plugin_id": plugin.id,
                    "capability_id": capability.id,
                    "adapter": capability.adapter,
                    "permission_class": capability.permission_class,
                    "sandbox_profile": capability.sandbox_profile,
                    "replay_safe": capability.replay_safe is True,
                    "run_id": str(context.run_id),
                    "user_id": str(context.user_id),
                    "idempotency_key": context.idempotency_key,
                },
            )
        except Exception:  # noqa: BLE001 - plugin execution must not leak audit backend failures.
            return


async def build_runtime_plugin_service(
    *,
    tenant_id: UUID,
    admin_service: PluginConfigService,
    adapters: Mapping[str, PluginAdapter] | None = None,
) -> RuntimePluginService:
    service = RuntimePluginService(
        tenant_id=tenant_id,
        admin_service=admin_service,
        adapters=adapters,
    )
    await service.start()
    return service


def _empty_manifest() -> Mapping[str, JsonValue]:
    return {
        "schema_version": 1,
        "capabilities": (),
    }


def _plugin_is_running(plugin: PluginResourceResponse) -> bool:
    return (
        plugin.enabled is True
        and plugin.status == "running"
        and plugin.health == "healthy"
    )


def _permission_class_parts(permission_class: str) -> tuple[str, str] | None:
    separator = "." if "." in permission_class else ":"
    parts = permission_class.split(separator, 1)
    if len(parts) != 2 or not all(parts):
        return None
    capability, operation = parts
    return capability, operation


def _plugin_policy_resource(
    plugin: PluginResourceResponse,
    capability: PluginCapabilityRequest,
) -> str:
    return f"plugin/{plugin.id}/{capability.id.replace('.', '/')}"


def _generic_plugin_policy_resource(capability: PluginCapabilityRequest) -> str:
    return f"plugin/{capability.id.replace('.', '/')}"


def _plugin_capability_policy_parts(
    plugin: PluginResourceResponse,
    capability: PluginCapabilityRequest,
) -> tuple[str, str, str] | None:
    if capability.permission_class == "plugin.use":
        return "plugin", "use", _generic_plugin_policy_resource(capability)
    permission = _permission_class_parts(capability.permission_class)
    if permission is None:
        return None
    policy_capability, policy_operation = permission
    return policy_capability, policy_operation, _plugin_policy_resource(plugin, capability)


def _plugin_policy_effect(value: str) -> PolicyEffect | None:
    if value == "inherit":
        return None
    try:
        return PolicyEffect(value)
    except ValueError:
        return PolicyEffect.DENY


def _ensure_supported_plugin_sandbox_profile(sandbox_profile: str) -> None:
    if sandbox_profile in _SUPPORTED_PLUGIN_SANDBOX_PROFILES:
        return
    raise RuntimeCapabilityError("Plugin sandbox profile unsupported")


_SUPPORTED_PLUGIN_SANDBOX_PROFILES = frozenset(("remote_connector",))
_PLUGIN_POLICY_ROLES = (Role.SUPER_ADMIN, Role.ADMIN, Role.OPERATOR)


def _default_plugin_adapters(admin_service: object) -> dict[str, PluginAdapter]:
    return {
        "http_json": HttpJsonPluginAdapter(
            secret_resolver=_secret_resolver(admin_service),
        )
    }


def _adapter_descriptor(adapter_id: str, adapter: PluginAdapter) -> Mapping[str, JsonValue]:
    descriptor = getattr(adapter, "descriptor", None)
    if callable(descriptor):
        try:
            payload = descriptor()
        except Exception:  # noqa: BLE001 - descriptor metadata must fail closed.
            payload = None
        if isinstance(payload, Mapping):
            return cast(Mapping[str, JsonValue], payload)
    return {
        "id": adapter_id,
        "name": adapter_id,
        "description": None,
        "resource_schema": {
            "type": "object",
            "additionalProperties": True,
        },
        "capability_schema": {
            "type": "object",
            "additionalProperties": True,
        },
        "argument_schema": {
            "type": "object",
            "additionalProperties": True,
        },
    }


def _adapter_argument_schema(
    adapter_id: str,
    adapter: PluginAdapter,
) -> Mapping[str, JsonValue] | None:
    argument_schema = _adapter_descriptor(adapter_id, adapter).get("argument_schema")
    if isinstance(argument_schema, Mapping):
        return argument_schema
    return None


def _http_json_adapter_descriptor() -> Mapping[str, JsonValue]:
    return {
        "id": "http_json",
        "name": "HTTP JSON",
        "description": "POSTs plugin invocations to an allowlisted HTTP endpoint.",
        "resource_schema": {
            "type": "object",
            "required": ("endpoint_url", "domain_allowlist"),
            "properties": {
                "endpoint_url": {
                    "type": "string",
                    "format": "uri",
                },
                "domain_allowlist": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 120,
                    "default": 10,
                },
                "credential_ref": {
                    "type": "string",
                },
                "credential_header": {
                    "type": "string",
                    "default": "X-Plugin-Credential",
                },
                "credential_scheme": {
                    "type": "string",
                    "default": "Bearer",
                },
            },
            "additionalProperties": False,
        },
        "capability_schema": {
            "type": "object",
            "required": ("id",),
            "properties": {
                "id": {"type": "string"},
                "permission_class": {"type": "string", "default": "plugin.use"},
                "sandbox_profile": {"type": "string", "default": "remote_connector"},
                "policy_effect": {
                    "type": "string",
                    "enum": ("inherit", "allow", "require_approval", "deny"),
                    "default": "inherit",
                },
                "replay_safe": {"type": "boolean", "default": False},
                "aliases": {"type": "array", "items": {"type": "string"}},
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
            "additionalProperties": False,
        },
        "argument_schema": {
            "type": "object",
            "additionalProperties": True,
        },
    }


def _plugin_endpoint_url(plugin: PluginResourceResponse) -> str:
    url = plugin.endpoint_url
    if not isinstance(url, str) or not url.strip() or url != url.strip():
        raise RuntimeCapabilityError("Plugin endpoint unavailable")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.hostname is None:
        raise RuntimeCapabilityError("Plugin endpoint unavailable")
    return url


def _endpoint_domain_allowed(url: str, domain_allowlist: Sequence[str]) -> bool:
    host = urlsplit(url).hostname
    if host is None:
        return False
    normalized_host = host.lower()
    allowed = {
        domain.lower().strip()
        for domain in domain_allowlist
        if isinstance(domain, str) and domain.strip()
    }
    return normalized_host in allowed


def _plugin_schema_validator(
    *,
    schema: Mapping[str, JsonValue] | None,
    invalid_schema_message: str,
) -> Validator | None:
    failure = False
    try:
        return plugin_schema_validator(
            schema=schema,
            invalid_schema_message=invalid_schema_message,
        )
    except PluginSchemaError:
        failure = True
    if failure:
        raise RuntimeCapabilityError(invalid_schema_message)
    return None


def _validate_plugin_payload(
    *,
    payload: Mapping[str, JsonValue],
    validator: Validator | None,
    validation_message: str,
    include_location: bool,
) -> None:
    if validator is None:
        return
    instance_payload = cast(Any, _mutable_json(cast(JsonValue, payload)))
    failure: str | None = None
    try:
        validator.validate(instance_payload)
    except ValidationError as error:
        location = _validation_error_location(error) if include_location else ""
        reason = _validation_error_reason(error)
        failure = f"{validation_message}{location}: {reason}"
    if failure is not None:
        raise RuntimeCapabilityError(failure)


def _validation_error_location(error: ValidationError) -> str:
    path = ".".join(str(item) for item in error.absolute_path)
    if not path:
        return ""
    return f" at {path}"


def _validation_error_reason(error: ValidationError) -> str:
    match error.validator:
        case "additionalProperties":
            return "unexpected field"
        case "enum":
            return "unsupported value"
        case "format":
            return "invalid format"
        case "maxItems" | "minItems":
            return "invalid item count"
        case "maxLength" | "minLength":
            return "invalid string length"
        case "maximum" | "minimum" | "exclusiveMaximum" | "exclusiveMinimum":
            return "number is outside the allowed range"
        case "pattern":
            return "invalid string pattern"
        case "required":
            return "required field is missing"
        case "type":
            return "invalid type"
        case _:
            return "validation failed"


async def _httpx_post_json(
    url: str,
    payload: Mapping[str, JsonValue],
    timeout_seconds: float,
    headers: Mapping[str, str],
) -> Mapping[str, JsonValue]:
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, Mapping):
        raise RuntimeCapabilityError("Plugin result is invalid")
    return cast(Mapping[str, JsonValue], data)


def _secret_resolver(admin_service: object) -> PluginSecretResolver | None:
    resolver = getattr(admin_service, "resolve_secret_value", None)
    if not callable(resolver):
        return None
    return cast(PluginSecretResolver, resolver)


__all__ = [
    "HttpJsonPluginAdapter",
    "HttpJsonPost",
    "PluginAdapter",
    "PluginConfigService",
    "PluginInvocationContext",
    "PluginSecretResolver",
    "RuntimePluginService",
    "build_runtime_plugin_service",
]
