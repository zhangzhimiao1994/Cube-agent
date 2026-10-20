"""Bounded, non-executing prerequisite observations for an authoritative project."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from agent_hub.capabilities.scoped_read import (
    _read_no_links,
    authorized_workspace_scope,
)
from agent_hub.runtime.contracts import JsonValue

PROJECT_INSPECT_TOOL = "project.inspect"
_MANIFESTS = ("package.json", "pyproject.toml", "requirements.txt")
_LOCKS = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock",
          "uv.lock", "poetry.lock", "Pipfile.lock")
_MAX_BYTES = 65_536
_DEPENDENCY_RESPONSE_BYTES = 35_000
_NPM_NAME = re.compile(r"(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]{0,127}")
_PYTHON_NAME = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})(.*)")
_CONSTRAINT = re.compile(r"[A-Za-z0-9*^~<>=!|., +_-]{1,160}")
_PYTHON_CONSTRAINT = re.compile(r"(?:\[[A-Za-z0-9_,. -]+\])?[A-Za-z0-9*~<>=!., +()_-]*")
_MARKER = re.compile(r"[A-Za-z0-9_ .<>=!'\"() -]{1,256}")


class ProjectInspectionError(RuntimeError):
    """Project observations could not be authorized."""


class _RunRepository(Protocol):
    async def get(self, tenant_id: UUID, run_id: UUID) -> object: ...


async def inspect_project(
    *, repository: object, tenant_id: UUID, run_id: UUID, project_root: Path,
) -> Mapping[str, JsonValue]:
    try:
        record = await cast(_RunRepository, repository).get(tenant_id, run_id)
        project, session = authorized_workspace_scope(record, tenant_id=tenant_id, run_id=run_id)
        routing = getattr(record, "routing_decision", None)
        if not isinstance(routing, Mapping):
            raise ProjectInspectionError
        backend = routing.get("execution_backend", "systemd")
        if not isinstance(backend, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", backend) is None:
            raise ProjectInspectionError
    except Exception:  # noqa: BLE001 - do not expose repository or other-scope details.
        raise ProjectInspectionError("project inspection scope unavailable") from None

    observations: list[JsonValue] = []
    dependencies: list[JsonValue] = []
    runtimes: list[JsonValue] = []
    issues: list[JsonValue] = []
    operations: dict[str, JsonValue] = {"build": "unknown", "test": "unknown"}
    declarations = 0
    python_manifest: bytes | None = None
    for filename in (*_MANIFESTS, *_LOCKS):
        parts = (str(tenant_id), "projects", project, "sessions", session, filename)
        item: dict[str, JsonValue] = {"path": filename, "status": "absent", "sha256": None}
        try:
            data = _read_no_links(project_root, parts, max_bytes=_MAX_BYTES)
        except FileNotFoundError:
            observations.append(item)
            continue
        except (OSError, ValueError, RuntimeError):
            item["status"] = "unavailable"
            observations.append(item)
            issues.append({"path": filename, "reason": "manifest_unavailable"})
            continue
        item["observed_bytes"] = min(len(data), _MAX_BYTES)
        if len(data) > _MAX_BYTES:
            item["status"] = "too_large"
            observations.append(item)
            # Large locks are metadata observations only, not evidence of a valid install.
            if filename in _MANIFESTS:
                issues.append({"path": filename, "reason": "manifest_too_large"})
            continue
        item["sha256"] = sha256(data).hexdigest()
        item["status"] = "observed"
        if filename in _MANIFESTS:
            try:
                text = data.decode("utf-8-sig", errors="strict")
                parsed = _parse_manifest(filename, text)
                new_dependencies, new_runtimes, new_operations, new_issues = parsed
            except (ValueError, TypeError, RecursionError):
                item["status"] = "invalid"
                issues.append({"path": filename, "reason": "manifest_invalid"})
            else:
                if filename == "pyproject.toml":
                    python_manifest = data
                declarations += 1
                dependencies.extend(new_dependencies)
                runtimes.extend(new_runtimes)
                operations.update(new_operations)
                issues.extend({"path": filename, "reason": reason}
                              for reason in dict.fromkeys(new_issues))
        observations.append(item)
    python_recipe: Mapping[str, JsonValue] | None = None
    if python_manifest is not None and _explicit_python_recipe(python_manifest):
        python_recipe, lock_observation, recipe_issue = _inspect_python_recipe(
            project_root, (str(tenant_id), "projects", project, "sessions", session), python_manifest,
        )
        observations.append(lock_observation)
        if recipe_issue is None:
            operations.update({"build": "declared", "test": "declared"})
        else:
            operations.update({"build": "unknown", "test": "unknown"})
            issues.append({"path": "requirements.lock", "reason": recipe_issue})
    dependency_count = len(dependencies)
    visible_dependencies: list[JsonValue] = []
    remaining = _DEPENDENCY_RESPONSE_BYTES
    for dependency in dependencies:
        size = len(json.dumps(dependency, separators=(",", ":")).encode("utf-8"))
        if size > remaining:
            break
        visible_dependencies.append(dependency)
        remaining -= size
    dependencies_truncated = len(visible_dependencies) != dependency_count
    if dependencies_truncated:
        issues.append({"path": "project", "reason": "dependency_observation_truncated"})
    result: dict[str, JsonValue] = {
        "schema_version": 1,
        "scope": {"tenant_id": str(tenant_id), "run_id": str(run_id),
                  "project_id": project, "workspace_session_id": session},
        "environment": {"selected_backend": backend, "readiness": "not_probed"},
        "manifests": tuple(observations), "dependencies": tuple(visible_dependencies),
        "dependency_count": dependency_count, "dependencies_truncated": dependencies_truncated,
        "runtimes": tuple(runtimes), "operations": operations, "issues": tuple(issues),
        "install_plan_state": (
            "blocked" if issues else "requires_environment_probe" if declarations
            else "no_supported_manifest"
        ),
        "summary": "Project manifests inspected without running project code; environment not probed.",
    }
    if python_recipe is not None:
        result["python_recipe"] = python_recipe
    return result


def _explicit_python_recipe(manifest: bytes) -> bool:
    document = tomllib.loads(manifest.decode("utf-8-sig"))
    tools = document.get("tool", {})
    agent = tools.get("agent-hub", {}) if isinstance(tools, dict) else {}
    return isinstance(agent, dict) and "python-project" in agent


def _inspect_python_recipe(
    root: Path, parts: tuple[str, ...], manifest: bytes,
) -> tuple[Mapping[str, JsonValue], Mapping[str, JsonValue], str | None]:
    from agent_hub.capabilities.python_project_recipe import parse_python_project_declaration

    observed: dict[str, JsonValue] = {"path": "requirements.lock", "status": "absent", "sha256": None}
    unavailable: Mapping[str, JsonValue] = {"status": "unavailable"}
    try:
        raw = _read_no_links(root, (*parts, "requirements.lock"), max_bytes=_MAX_BYTES)
    except FileNotFoundError:
        return unavailable, observed, "python_recipe_inputs_unavailable"
    except (OSError, ValueError, RuntimeError):
        observed["status"] = "unavailable"
        return unavailable, observed, "python_recipe_inputs_unavailable"
    observed["observed_bytes"] = min(len(raw), _MAX_BYTES)
    if len(raw) > _MAX_BYTES:
        observed["status"] = "too_large"
        return unavailable, observed, "python_recipe_inputs_unavailable"
    observed.update({"status": "observed", "sha256": sha256(raw).hexdigest()})
    try:
        declaration = parse_python_project_declaration(manifest, raw,
            max_control_bytes=2 * _MAX_BYTES, max_requirements=128)
    except (ValueError, TypeError, RecursionError):
        return unavailable, observed, "python_recipe_inputs_invalid"
    # Declared operations are advisory; no source hooks or runtime probes ran.
    return {"kind": declaration.kind, "status": "declared"}, observed, None


def _parse_manifest(
    filename: str, text: str,
) -> tuple[list[JsonValue], list[JsonValue], dict[str, JsonValue], list[str]]:
    if filename == "package.json":
        return _parse_node(text)
    if filename == "pyproject.toml":
        return _parse_pyproject(text)
    dependencies: list[JsonValue] = []
    issues: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        _add_python_requirement(line, "requirements", dependencies, issues)
    return dependencies, [{"name": "python", "constraint": None}], {}, issues


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _parse_node(
    text: str,
) -> tuple[list[JsonValue], list[JsonValue], dict[str, JsonValue], list[str]]:
    manifest: object = json.loads(text, object_pairs_hook=_json_object)
    if not isinstance(manifest, dict):
        raise TypeError
    dependencies: list[JsonValue] = []
    issues: list[str] = []
    for group in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        entries = manifest.get(group, {})
        if not isinstance(entries, dict):
            raise TypeError
        for name, specification in sorted(entries.items()):
            if not isinstance(name, str) or _NPM_NAME.fullmatch(name) is None:
                raise ValueError
            safe = isinstance(specification, str) and _CONSTRAINT.fullmatch(specification) is not None
            dependencies.append({
                "ecosystem": "npm", "name": name, "group": group,
                "source_kind": "registry" if safe else "requires_source_review",
                "constraint": specification if safe else None,
            })
            if not safe:
                issues.append("dependency_source_requires_review")
    engines = manifest.get("engines", {})
    scripts = manifest.get("scripts", {})
    if not isinstance(engines, dict) or not isinstance(scripts, dict):
        raise TypeError
    runtimes: list[JsonValue] = []
    for name in ("node", "npm"):
        constraint = engines.get(name)
        if constraint is not None and (
            not isinstance(constraint, str) or _CONSTRAINT.fullmatch(constraint) is None
        ):
            raise ValueError
        runtimes.append({"name": name, "constraint": constraint})
    operations: dict[str, JsonValue] = {}
    for name in ("build", "test"):
        command = scripts.get(name)
        if command is not None and (not isinstance(command, str) or not command.strip()):
            raise ValueError
        operations[name] = "declared" if command is not None else "absent"
    if "workspaces" in manifest:
        # Fixed root manifest inspection does not recursively resolve workspace packages yet.
        issues.append("workspace_dependency_graph_requires_inspection")
    if any(manifest.get(field) for field in ("overrides", "resolutions", "pnpm")):
        issues.append("dependency_overrides_require_review")
    manager = manifest.get("packageManager")
    if manager is not None and (
        not isinstance(manager, str) or re.fullmatch(r"npm@[0-9]+(?:\.[0-9]+){0,2}", manager) is None
    ):
        issues.append("package_manager_requires_inspection")
    return dependencies, runtimes, operations, issues


def _parse_pyproject(
    text: str,
) -> tuple[list[JsonValue], list[JsonValue], dict[str, JsonValue], list[str]]:
    manifest = tomllib.loads(text)
    project = manifest.get("project", {})
    build = manifest.get("build-system", {})
    if not isinstance(project, dict) or not isinstance(build, dict):
        raise TypeError
    issues: list[str] = []
    dependencies: list[JsonValue] = []
    tools = manifest.get("tool", {})
    if not isinstance(tools, dict):
        raise TypeError
    if "dependency-groups" in manifest or "poetry" in tools or "pdm" in tools:
        issues.append("dependency_groups_require_inspection")
    uv = tools.get("uv", {})
    if not isinstance(uv, dict):
        raise TypeError
    if any(uv.get(field) for field in ("sources", "index", "index-url", "extra-index-url")):
        issues.append("dependency_sources_require_review")
    dynamic = project.get("dynamic", [])
    if not isinstance(dynamic, list) or not all(isinstance(item, str) for item in dynamic):
        raise ValueError
    if {"dependencies", "optional-dependencies", "requires-python"}.intersection(dynamic):
        issues.append("dynamic_requirements_require_inspection")
    for source, field, group in ((project, "dependencies", "dependencies"),
                                 (build, "requires", "build-system")):
        entries = source.get(field, [])
        if not isinstance(entries, list):
            raise TypeError
        for entry in entries:
            _add_python_requirement(entry, group, dependencies, issues)
    optional = project.get("optional-dependencies", {})
    if optional:
        # Extras must be selected deliberately before resolving an installation graph.
        issues.append("optional_dependency_groups_require_selection")
    constraint = project.get("requires-python")
    if constraint is not None and (
        not isinstance(constraint, str) or _CONSTRAINT.fullmatch(constraint) is None
    ):
        raise ValueError
    return dependencies, [{"name": "python", "constraint": constraint}], {}, issues


def _add_python_requirement(
    value: object, group: str, dependencies: list[JsonValue], issues: list[str],
) -> None:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError
    if value.casefold().endswith((".whl", ".tar.gz", ".tar.bz2", ".tgz", ".zip")):
        issues.append("local_archive_requires_review")
        return
    # Requirements options, includes and URLs are not authority to fetch arbitrary sources.
    match = _PYTHON_NAME.fullmatch(value)
    if match is None:
        issues.append("requirement_requires_review")
        return
    name, tail = match.groups()
    requirement, separator, marker = tail.partition(";")
    safe = (_PYTHON_CONSTRAINT.fullmatch(requirement) is not None
            and (not separator or _MARKER.fullmatch(marker) is not None))
    dependencies.append({
        "ecosystem": "pypi", "name": name, "group": group,
        "source_kind": "registry" if safe else "requires_source_review",
        "constraint": (tail.strip() or None) if safe else None,
    })
    if not safe:
        issues.append("dependency_source_requires_review")
