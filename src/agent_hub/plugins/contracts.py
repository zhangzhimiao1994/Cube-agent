from __future__ import annotations

from collections.abc import Mapping, Sequence

from agent_hub.runtime.contracts import JsonValue

SUPPORTED_PLUGIN_SANDBOX_PROFILES = frozenset(("remote_connector",))
ADAPTER_DECLARABLE_PLUGIN_SANDBOX_PROFILES = frozenset(("http_read", "in_process"))


def http_json_adapter_descriptor() -> Mapping[str, JsonValue]:
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


def adapter_declared_sandbox_profiles(
    descriptor: Mapping[str, JsonValue],
) -> frozenset[str]:
    capability_schema = descriptor.get("capability_schema")
    if not isinstance(capability_schema, Mapping):
        return frozenset()
    properties = capability_schema.get("properties")
    if not isinstance(properties, Mapping):
        return frozenset()
    sandbox_schema = properties.get("sandbox_profile")
    if not isinstance(sandbox_schema, Mapping):
        return frozenset()
    enum_values = sandbox_schema.get("enum")
    if not isinstance(enum_values, Sequence) or isinstance(enum_values, str | bytes):
        return frozenset()
    return frozenset(value for value in enum_values if isinstance(value, str) and value)


def adapter_runtime_sandbox_profiles(
    descriptor: Mapping[str, JsonValue],
) -> tuple[str, ...]:
    declared_profiles = (
        adapter_declared_sandbox_profiles(descriptor)
        & ADAPTER_DECLARABLE_PLUGIN_SANDBOX_PROFILES
    )
    return tuple(sorted(SUPPORTED_PLUGIN_SANDBOX_PROFILES | declared_profiles))


def adapter_capability_contract(
    descriptor: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue]:
    return {
        "schema_version": 1,
        "declared_sandbox_profiles": tuple(sorted(adapter_declared_sandbox_profiles(descriptor))),
        "runtime_sandbox_profiles": adapter_runtime_sandbox_profiles(descriptor),
    }


def adapter_descriptor_with_contract(
    descriptor: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue]:
    payload = dict(descriptor)
    payload["capability_contract"] = adapter_capability_contract(descriptor)
    return payload


__all__ = [
    "ADAPTER_DECLARABLE_PLUGIN_SANDBOX_PROFILES",
    "SUPPORTED_PLUGIN_SANDBOX_PROFILES",
    "adapter_capability_contract",
    "adapter_declared_sandbox_profiles",
    "adapter_descriptor_with_contract",
    "adapter_runtime_sandbox_profiles",
    "http_json_adapter_descriptor",
]
