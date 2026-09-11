"""Offline dependency policy helpers for plugin packages."""

from __future__ import annotations

import hashlib
import json
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
            if self.cache_dir is not None
            and _dependency_cache_entry_matches(self.cache_dir, lock)
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


def _dependency_cache_entry_matches(
    cache_dir: Path,
    lock: PluginPackageDependencyLock,
) -> bool:
    entry_dir = cache_dir / lock.sha256
    if not entry_dir.is_dir():
        return False
    manifest_path = entry_dir / "dependency-lock.json"
    if not manifest_path.is_file():
        return False
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, list):
        return False
    expected_dependencies: list[dict[str, str]] = [
        {
            "kind": dependency.kind,
            "source": dependency.source,
            "name": dependency.name,
            "version": dependency.version,
        }
        for dependency in lock.dependencies
    ]
    return (
        payload.get("schema_version") == 1
        and payload.get("sha256") == lock.sha256
        and dependencies == expected_dependencies
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
