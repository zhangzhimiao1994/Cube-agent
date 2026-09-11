"""Offline dependency policy helpers for plugin packages."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

PluginPackageDependencyInstallPolicy = Literal["disabled", "offline_cache"]
PluginPackageDependencyCacheStatus = Literal["missing", "present"]
PluginPackageDependencyAllowlistStatus = Literal["missing", "allowed", "not_allowed"]


class PluginPackageDependencyLike(Protocol):
    kind: str
    source: str
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class NormalizedPluginPackageDependency:
    kind: str
    source: str
    name: str
    version: str

    @property
    def allowlist_key(self) -> str:
        return f"{self.kind}:{self.source}:{self.name}=={self.version}"


@dataclass(frozen=True, slots=True)
class PluginPackageDependencyLock:
    sha256: str
    dependencies: tuple[NormalizedPluginPackageDependency, ...]


@dataclass(frozen=True, slots=True)
class PluginPackageDependencyPolicy:
    install_policy: PluginPackageDependencyInstallPolicy = "disabled"
    allowlist: frozenset[str] = frozenset()
    cache_dir: Path | None = None

    def evaluate(
        self,
        lock: PluginPackageDependencyLock,
    ) -> tuple[
        Literal["not_configured", "offline_cache"],
        PluginPackageDependencyCacheStatus,
        PluginPackageDependencyAllowlistStatus,
    ]:
        if self.install_policy != "offline_cache":
            return ("not_configured", "missing", "missing")
        cache_status: PluginPackageDependencyCacheStatus = (
            "present"
            if self.cache_dir is not None and (self.cache_dir / lock.sha256).is_dir()
            else "missing"
        )
        dependency_keys = frozenset(dependency.allowlist_key for dependency in lock.dependencies)
        allowlist_status: PluginPackageDependencyAllowlistStatus = (
            "allowed" if dependency_keys <= self.allowlist else "not_allowed"
        )
        return ("offline_cache", cache_status, allowlist_status)


def plugin_package_dependency_policy_from_settings(
    settings: object,
) -> PluginPackageDependencyPolicy:
    raw_install_policy = getattr(
        settings, "plugin_package_dependency_install_policy", "disabled"
    )
    install_policy: PluginPackageDependencyInstallPolicy = (
        cast(PluginPackageDependencyInstallPolicy, raw_install_policy)
        if raw_install_policy in {"disabled", "offline_cache"}
        else "disabled"
    )
    allowlist: object = getattr(
        settings, "plugin_package_dependency_allowlist", frozenset[str]()
    )
    cache_dir = getattr(settings, "plugin_package_dependency_cache_dir", None)
    normalized_allowlist: frozenset[str] = (
        frozenset(str(item) for item in allowlist)
        if isinstance(allowlist, frozenset | set)
        else frozenset()
    )
    return PluginPackageDependencyPolicy(
        install_policy=install_policy,
        allowlist=normalized_allowlist,
        cache_dir=cache_dir if isinstance(cache_dir, Path) else None,
    )


def normalize_plugin_package_dependency_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def normalize_plugin_package_dependency_allowlist_entry(value: str) -> str:
    match = re.fullmatch(
        r"python:pypi:([A-Za-z0-9][A-Za-z0-9_.-]{0,127})=="
        r"([A-Za-z0-9][A-Za-z0-9!+.,<=>~_-]{0,127})",
        value,
    )
    if match is None:
        raise ValueError("plugin package dependency allowlist entry is invalid")
    return (
        "python:pypi:"
        f"{normalize_plugin_package_dependency_name(match.group(1))}=={match.group(2)}"
    )


def plugin_package_dependency_lock(
    dependencies: Sequence[PluginPackageDependencyLike],
) -> PluginPackageDependencyLock | None:
    locked: list[NormalizedPluginPackageDependency] = []
    for dependency in dependencies:
        kind = getattr(dependency, "kind", None)
        source = getattr(dependency, "source", None)
        name = getattr(dependency, "name", None)
        version = getattr(dependency, "version", None)
        if not all(type(value) is str for value in (kind, source, name, version)):
            return None
        normalized_kind = cast(str, kind)
        normalized_source = cast(str, source)
        normalized_name = cast(str, name)
        normalized_version = cast(str, version)
        locked.append(
            NormalizedPluginPackageDependency(
                kind=normalized_kind,
                source=normalized_source,
                name=normalize_plugin_package_dependency_name(normalized_name),
                version=normalized_version,
            )
        )
    ordered = tuple(
        sorted(
            locked,
            key=lambda item: (item.kind, item.source, item.name, item.version),
        )
    )
    lock_bytes = "".join(
        f"{item.kind} {item.source} {item.name}=={item.version}\n" for item in ordered
    ).encode("utf-8")
    return PluginPackageDependencyLock(
        sha256=hashlib.sha256(lock_bytes).hexdigest(),
        dependencies=ordered,
    )
