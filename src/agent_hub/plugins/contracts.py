from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

from agent_hub.runtime.contracts import JsonValue

SUPPORTED_PLUGIN_SANDBOX_PROFILES = frozenset(("remote_connector",))
ADAPTER_DECLARABLE_PLUGIN_SANDBOX_PROFILES = frozenset(
    ("http_read", "in_process", "local_process")
)
PLUGIN_ADAPTER_RUNTIME_FAILURE_CODES: tuple[str, ...] = (
    "plugin.adapter_unavailable",
    "plugin.timeout",
    "plugin.credential_unavailable",
    "plugin.invalid_arguments",
    "plugin.invalid_result",
    "plugin.backend_unavailable",
    "plugin.endpoint_unavailable",
    "plugin.sandbox_unsupported",
)

_PLUGIN_CREATION_EN = re.compile(
    r"\b(?:generate|create|build|write|implement|develop|add)\s+"
    r"(?:(?!(?:using|with|via|through|for|from|by|report|documentation|docs?|"
    r"instructions?|examples?|guides?|schema)\b)[\w-]+\s+){0,10}"
    r"(?:(?:plugins?|capability|capabilities)\b|\u63d2\u4ef6|\u80fd\u529b)"
    r"(?!\s+(?:documentation|docs?|instructions?|examples?|guides?|schema)\b)",
    re.IGNORECASE,
)
_PLUGIN_CREATION_ZH = re.compile(
    r"(?:\u751f\u6210|\u521b\u5efa|\u5f00\u53d1|\u7f16\u5199|\u5b9e\u73b0|"
    r"\u65b0\u589e|\u6dfb\u52a0|\u505a|\u5199)"
    r"(?:(?!(?:\u4f7f\u7528|\u8c03\u7528|\u901a\u8fc7|\u5229\u7528|\u7528|"
    r"\u62a5\u544a|\u6587\u6863|\u8bf4\u660e|\u793a\u4f8b|\u6559\u7a0b))[^\r\n\uff0c,]){0,32}?"
    r"(?:\u63d2\u4ef6|\u80fd\u529b|(?:plugins?|capability|capabilities)\b)"
    r"(?!(?:\u6587\u6863|\u8bf4\u660e|\u793a\u4f8b|\u6559\u7a0b))",
    re.IGNORECASE,
)
_PLUGIN_CONVERSION = re.compile(
    r"\b(?:pluginize|pluginise)\b|(?:\u628a|\u5c06)[^\r\n\uff0c,]{0,48}\u63d2\u4ef6\u5316",
    re.IGNORECASE,
)
_PLUGIN_CREATION_EXPLANATION = re.compile(
    r"\b(?:explain|describe|how\s+to|how\s+do)\b(?!.*\b(?:just|instead|now)\s*$)|"
    r"(?:\u89e3\u91ca|\u4ecb\u7ecd|\u8bf4\u660e|\u5982\u4f55|\u600e\u4e48)"
    r"(?!.*(?:\u76f4\u63a5|\u800c\u662f|\u73b0\u5728)\s*$)|"
    r"(?:\b(?:do\s+not|don't|never)(?:\s+(?:actually|really))?\b|"
    r"(?:\u4e0d\u8981|\u65e0\u9700|\u4e0d\u9700\u8981|\u4e0d\u7528)"
    r"(?:\u518d|\u53bb|\u6765|\u76f4\u63a5)?)\s*$",
    re.IGNORECASE,
)


def plugin_package_stdin_schema() -> Mapping[str, JsonValue]:
    """SDK 1.0 package invocation envelope, distinct from business input_schema."""
    return {
        "type": "object",
        "required": (
            "schema_version", "plugin_id", "capability_id", "arguments",
            "resource_config", "capability_config", "context",
        ),
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "plugin_id": {"type": "string"},
            "capability_id": {"type": "string"},
            "arguments": {"type": "object"},
            "resource_config": {"type": "object"},
            "capability_config": {"type": "object"},
            "context": {
                "type": "object",
                "required": ("tenant_id", "user_id", "run_id", "actor", "idempotency_key"),
                "properties": {
                    key: {"type": "string"}
                    for key in ("tenant_id", "user_id", "run_id", "actor", "idempotency_key")
                },
                "additionalProperties": True,
            },
        },
        "additionalProperties": True,
    }


def plugin_package_generation_guidance(request: str) -> str:
    """Supply SDK knowledge for creation requests without changing execution policy."""
    for clause in re.split(r"[,.!?;\n\uff0c\u3002\uff01\uff1f\uff1b]", request):
        match = (_PLUGIN_CREATION_EN.search(clause) or _PLUGIN_CREATION_ZH.search(clause)
                 or _PLUGIN_CONVERSION.search(clause))
        if match is None or _PLUGIN_CREATION_EXPLANATION.search(clause[:match.start()]):
            continue
        return (
            "PLUGIN_PACKAGE_STDIO_CONTRACT (SDK API 1.0): When implementing a local Python "
            "plugin/capability adapter package, read one UTF-8 JSON object from stdin until EOF. "
            "The runtime sends all seven envelope fields below, including nonempty configuration "
            "and context metadata. Validate the capability's business input_schema only against "
            "request['arguments'], never the whole stdin envelope. Read capability_id to dispatch; "
            "accept and ignore unused metadata and additional envelope/context fields. "
            "Do not restrict top-level keys to capability_id and arguments. "
            "Write exactly one business JSON object matching output_schema to stdout, with no "
            "result wrapper, log lines, prose, or Markdown; send diagnostics to stderr. "
            "stdin/stdout are local process pipes, not network traffic. These are the generated "
            "adapter's I/O rules, not this model response's format or tool permissions.\n"
            "PLUGIN_PACKAGE_STDIN_SCHEMA_JSON=" + json.dumps(
                plugin_package_stdin_schema(), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
        )
    return ""


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
        "failure_codes": PLUGIN_ADAPTER_RUNTIME_FAILURE_CODES,
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
    "PLUGIN_ADAPTER_RUNTIME_FAILURE_CODES",
    "SUPPORTED_PLUGIN_SANDBOX_PROFILES",
    "adapter_capability_contract",
    "adapter_declared_sandbox_profiles",
    "adapter_descriptor_with_contract",
    "adapter_runtime_sandbox_profiles",
    "http_json_adapter_descriptor",
    "plugin_package_generation_guidance",
    "plugin_package_stdin_schema",
]
