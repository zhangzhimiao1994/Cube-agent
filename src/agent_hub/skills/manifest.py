from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")
_SAFE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class SkillNetworkPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    mode: Literal["none", "allowlist"] = "none"
    allow_hosts: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("allow_hosts", mode="before")
    @classmethod
    def coerce_hosts(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("allow_hosts")
    @classmethod
    def validate_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        normalized: list[str] = []
        for host in value:
            cleaned = host.strip().lower()
            if cleaned != host or not cleaned or "/" in cleaned or "\\" in cleaned:
                raise ValueError("network hosts must be unpadded hostnames")
            if cleaned in seen:
                raise ValueError("network hosts must be unique")
            seen.add(cleaned)
            normalized.append(cleaned)
        return tuple(normalized)

    @model_validator(mode="after")
    def validate_mode(self) -> SkillNetworkPolicy:
        if self.mode == "none" and self.allow_hosts:
            raise ValueError("network allow_hosts require allowlist mode")
        if self.mode == "allowlist" and not self.allow_hosts:
            raise ValueError("allowlist mode requires at least one host")
        return self


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=5, max_length=64)
    entry_point: str = Field(min_length=1, max_length=256)
    compatible_runtime: str = Field(min_length=1, max_length=128)
    declared_tools: tuple[str, ...] = Field(default_factory=tuple)
    network_policy: SkillNetworkPolicy = Field(default_factory=SkillNetworkPolicy)
    writable_paths: tuple[str, ...] = Field(default_factory=tuple)
    env_secret_refs: tuple[str, ...] = Field(default_factory=tuple)
    dependency_lock_hash: str = Field(min_length=64, max_length=64)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _SAFE_NAME_PATTERN.fullmatch(value):
            raise ValueError("skill name must be lowercase safe identifier")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not _SEMVER_PATTERN.fullmatch(value):
            raise ValueError("skill version must be semantic version")
        return value

    @field_validator("entry_point")
    @classmethod
    def validate_entry_point(cls, value: str) -> str:
        return _validate_relative_path(value, field_name="entry_point")

    @field_validator("declared_tools", "writable_paths", "env_secret_refs", mode="before")
    @classmethod
    def coerce_tuple_fields(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("declared_tools", "writable_paths", "env_secret_refs")
    @classmethod
    def validate_unique_text(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        for item in value:
            if item != item.strip() or not item or any(ord(ch) < 32 or ord(ch) == 127 for ch in item):
                raise ValueError("manifest list values must be printable unpadded text")
            if item in seen:
                raise ValueError("manifest list values must be unique")
            seen.add(item)
        return value

    @field_validator("writable_paths")
    @classmethod
    def validate_writable_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_relative_path(path, field_name="writable_paths") for path in value)

    @field_validator("dependency_lock_hash")
    @classmethod
    def validate_dependency_lock_hash(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("dependency_lock_hash must be a lowercase sha256 hex digest")
        return value


def _validate_relative_path(value: str, *, field_name: str) -> str:
    cleaned = value.replace("\\", "/")
    if cleaned != value or cleaned.startswith(("/", "../")) or "/../" in cleaned:
        raise ValueError(f"{field_name} must be a safe relative path")
    if cleaned in {".", ".."} or cleaned.endswith("/.."):
        raise ValueError(f"{field_name} must be a safe relative path")
    if re.match(r"^[A-Za-z]:", cleaned):
        raise ValueError(f"{field_name} must be a safe relative path")
    if any(part in {"", ".", ".."} for part in cleaned.split("/")):
        raise ValueError(f"{field_name} must be a normalized relative path")
    return cleaned
