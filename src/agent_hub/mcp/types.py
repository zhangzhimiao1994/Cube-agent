from __future__ import annotations

import ipaddress
import math
import re
from enum import StrEnum
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_hub.security.secrets import SecretReference

JsonValue = object
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


class McpTransportKind(StrEnum):
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable_http"


class McpServerDefinition(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        arbitrary_types_allowed=True,
    )

    tenant_id: UUID
    id: str = Field(min_length=1, max_length=128)
    transport: McpTransportKind
    command: str | None = None
    args: tuple[str, ...] = Field(default_factory=tuple)
    executable_allowlist: tuple[str, ...] = Field(default_factory=tuple)
    url: str | None = None
    domain_allowlist: tuple[str, ...] = Field(default_factory=tuple)
    oauth_token_ref: SecretReference | None = None
    header_secret_refs: dict[str, SecretReference] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def parse_secret_references(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        raw = dict(value)
        tenant_raw = raw.get("tenant_id")
        if not isinstance(tenant_raw, UUID):
            return raw
        oauth_ref = raw.get("oauth_token_ref")
        if isinstance(oauth_ref, str):
            raw["oauth_token_ref"] = SecretReference.parse(tenant_raw, oauth_ref)
        headers = raw.get("header_secret_refs")
        if isinstance(headers, dict):
            raw["header_secret_refs"] = {
                header: SecretReference.parse(tenant_raw, secret_ref)
                if isinstance(secret_ref, str)
                else secret_ref
                for header, secret_ref in headers.items()
            }
        return raw

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("MCP server id must be a safe identifier")
        return value

    @field_validator("args", "executable_allowlist", "domain_allowlist", mode="before")
    @classmethod
    def coerce_tuple_fields(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("oauth_token_ref")
    @classmethod
    def validate_oauth_ref(cls, value: SecretReference | None) -> SecretReference | None:
        return value

    @field_validator("header_secret_refs")
    @classmethod
    def validate_header_secret_refs(
        cls,
        value: dict[str, SecretReference],
    ) -> dict[str, SecretReference]:
        result: dict[str, SecretReference] = {}
        for header, secret_ref in value.items():
            if header != header.strip() or not header or any(ord(ch) < 32 for ch in header):
                raise ValueError("header names must be printable unpadded text")
            result[header] = secret_ref
        return result

    @field_validator("executable_allowlist", "domain_allowlist")
    @classmethod
    def validate_unique_printable(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        normalized: list[str] = []
        for item in value:
            cleaned = item.strip()
            if cleaned != item or not item or any(ord(ch) < 32 or ord(ch) == 127 for ch in item):
                raise ValueError("allowlist values must be printable unpadded text")
            if cleaned in seen:
                raise ValueError("allowlist values must be unique")
            seen.add(cleaned)
            normalized.append(cleaned)
        return tuple(normalized)

    @model_validator(mode="after")
    def validate_transport(self) -> McpServerDefinition:
        if self.transport is McpTransportKind.STDIO:
            if self.command is None:
                raise ValueError("stdio MCP servers require a command")
            if self.url is not None:
                raise ValueError("stdio MCP servers cannot define a URL")
            if self.command not in self.executable_allowlist:
                raise ValueError("stdio command must be present in executable allowlist")
            return self
        if self.url is None:
            raise ValueError("remote MCP servers require a URL")
        if self.command is not None:
            raise ValueError("remote MCP servers cannot define a command")
        _validate_remote_url(self.url, self.domain_allowlist)
        return self


class McpToolSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    description: str = Field(default="", max_length=1024)
    input_schema: dict[str, object] = Field(default_factory=dict)

    @field_validator("input_schema", mode="before")
    @classmethod
    def validate_schema_json(cls, value: object) -> object:
        return _validate_json(value)


class DiscoveredMcpTool(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    server_id: str
    name: str
    description: str = ""
    input_schema: dict[str, object] = Field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        return f"{self.server_id}.{self.name}"


class McpGenerationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    generation_id: int
    tools: tuple[DiscoveredMcpTool, ...]
    health: dict[str, str] = Field(default_factory=dict)


class McpInvocationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    tenant_id: UUID
    user_id: UUID
    run_id: UUID
    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    idempotency_key: str = Field(min_length=1, max_length=160)


class McpInvocationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    content: dict[str, object] = Field(default_factory=dict)

    @field_validator("content", mode="before")
    @classmethod
    def validate_content_json(cls, value: object) -> object:
        return _validate_json(value)


class McpAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: str
    server_id: str | None = None
    tool_name: str | None = None
    generation_id: int
    status: str
    reason: str | None = None


def _validate_remote_url(url: str, allowed_domains: tuple[str, ...]) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("remote MCP URLs must use HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("remote MCP URLs cannot contain credentials")
    host = parsed.hostname
    if host is None:
        raise ValueError("remote MCP URLs require a host")
    host = host.rstrip(".").lower()
    if not _is_allowed_domain(host, allowed_domains):
        raise ValueError("remote MCP host must be in domain allowlist")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("remote MCP URLs cannot target private or local IP addresses")


def _is_allowed_domain(host: str, allowed_domains: tuple[str, ...]) -> bool:
    for domain in allowed_domains:
        candidate = domain.rstrip(".").lower()
        if host == candidate or host.endswith(f".{candidate}"):
            return True
    return False


def _validate_json(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, list):
        return [_validate_json(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            result[key] = _validate_json(item)
        return result
    raise ValueError("value must be JSON-compatible")
