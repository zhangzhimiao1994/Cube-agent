"""Bounded, non-executing prerequisite observations for an authoritative project."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID

from agent_hub.capabilities.project_stack_admission import additional_stack_discovery
from agent_hub.capabilities.scoped_read import (
    _read_no_links,
    authorized_workspace_scope,
)
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.runtime.contracts import JsonValue

if TYPE_CHECKING:
    from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfile

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
_CATALOG_BYTES = 6144  # Reserve space for the runtime's recipe guidance fields.
_CATALOG_ROWS = 24
_CATALOG_SHA = re.compile(r"[a-f0-9]{64}")
_CATALOG_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class ProjectInspectionError(RuntimeError):
    """Project observations could not be authorized."""


class PythonCatalogQueryInvalid(ProjectInspectionError):
    """A pure catalog query failed validation before any inspection was attempted."""


@dataclass(frozen=True, slots=True)
class PythonCatalogQueryFeedback:
    """A delivered rejection observation; never an inspection or execution receipt."""

    def to_payload(self) -> Mapping[str, JsonValue]:
        return {
            'schema_version': 1,
            'kind': 'project_inspection_query_feedback',
            'status': 'rejected',
            'reason_code': 'python_catalog_query_invalid',
            'inspection_performed': False,
            'correction': (
                'Correct the python_catalog query, or call project.inspect with {} to inspect '
                'the current catalog. Use its observed lowercase 64-hex catalog_sha256; '
                'offset must be an integer from 0 to 1000; supplied names must contain '
                '1 to 16 sorted unique canonical lowercase hyphenated names.'
            ),
        }


def is_python_catalog_query_feedback(value: Mapping[str, JsonValue]) -> bool:
    """Recognize only the complete fixed rejection; it grants no inspection evidence."""
    expected = PythonCatalogQueryFeedback().to_payload()
    return (set(value) == set(expected)
            and all(type(value[key]) is type(item) and value[key] == item
                    for key, item in expected.items()))


@dataclass(frozen=True, slots=True)
class PythonCatalogQuery:
    catalog_sha256: str
    offset: int = 0
    names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (type(self.catalog_sha256) is not str or _CATALOG_SHA.fullmatch(self.catalog_sha256) is None
                or type(self.offset) is not int or not 0 <= self.offset <= 1000
                or type(self.names) is not tuple or len(self.names) > 16
                or any(type(name) is not str or not 1 <= len(name) <= 128
                       or _CATALOG_NAME.fullmatch(name) is None for name in self.names)
                or self.names != tuple(sorted(set(self.names)))):
            raise PythonCatalogQueryInvalid('python_catalog_query_invalid')

    def to_payload(self) -> Mapping[str, JsonValue]:
        query: dict[str, JsonValue] = {'catalog_sha256': self.catalog_sha256, 'offset': self.offset}
        if self.names:
            query['names'] = self.names
        return {'python_catalog': query}


def parse_python_catalog_query(arguments: Mapping[str, JsonValue]) -> PythonCatalogQuery | None:
    if not isinstance(arguments, Mapping):
        raise PythonCatalogQueryInvalid('python_catalog_query_invalid')
    if not arguments:
        return None
    if set(arguments) != {'python_catalog'}:
        raise PythonCatalogQueryInvalid('python_catalog_query_invalid')
    query = arguments['python_catalog']
    if (not isinstance(query, Mapping) or 'catalog_sha256' not in query
            or set(query) - {'catalog_sha256', 'offset', 'names'}):
        raise PythonCatalogQueryInvalid('python_catalog_query_invalid')
    digest, offset = query['catalog_sha256'], query.get('offset', 0)
    if type(digest) is not str or type(offset) is not int:
        raise PythonCatalogQueryInvalid('python_catalog_query_invalid')
    names: list[str] = []
    if 'names' in query:
        raw_names = query['names']
        if not isinstance(raw_names, (list, tuple)) or not 1 <= len(raw_names) <= 16:
            raise PythonCatalogQueryInvalid('python_catalog_query_invalid')
        for name in raw_names:
            if type(name) is not str:
                raise PythonCatalogQueryInvalid('python_catalog_query_invalid')
            names.append(name)
    return PythonCatalogQuery(digest, offset, tuple(names))


def _python_catalog_size(value: Mapping[str, JsonValue]) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(',', ':')).encode('utf-8'))


def python_catalog_projection(
    profile: PythonProjectProfile, query: PythonCatalogQuery | None = None,
) -> Mapping[str, JsonValue]:
    """Page public declarations only; the Root digest remains opaque authority."""
    raw = cast(dict[str, JsonValue], profile.model_dump(mode='json'))
    if query is None and _python_catalog_size(raw) <= _CATALOG_BYTES:
        return raw
    if query is not None and query.catalog_sha256 != profile.catalog_sha256:
        raise ProjectInspectionError('python_catalog_changed')
    names, offset = ((), 0) if query is None else (query.names, query.offset)
    matches = tuple(wheel for wheel in profile.catalog if not names or wheel.name in names)
    if offset > len(matches):
        raise ProjectInspectionError('python_catalog_offset_invalid')

    def page(rows: list[JsonValue]) -> dict[str, JsonValue]:
        end = offset + len(rows)
        return {**raw, 'catalog': cast(JsonValue, rows),
                'catalog_complete': not names and offset == 0 and end == len(profile.catalog),
                'catalog_page': {
                    'schema_version': 1, 'total_count': len(profile.catalog),
                    'matched_count': len(matches), 'offset': offset, 'returned_count': len(rows),
                    'next_offset': end if end < len(matches) else None, 'names': names,
                }}

    rows: list[JsonValue] = []
    result = page(rows)
    if _python_catalog_size(result) > _CATALOG_BYTES:
        raise ProjectInspectionError('python_catalog_projection_limit')
    # Rebuild metadata with each whole row so next_offset and all echoed names count.
    for wheel in matches[offset:offset + _CATALOG_ROWS]:
        candidate_rows = [*rows, cast(JsonValue, wheel.model_dump(mode='json'))]
        candidate = page(candidate_rows)
        if _python_catalog_size(candidate) > _CATALOG_BYTES:
            break
        rows, result = candidate_rows, candidate
    if not rows and offset < len(matches):
        raise ProjectInspectionError('python_catalog_projection_limit')
    return result


class _RunRepository(Protocol):
    async def get(self, tenant_id: UUID, run_id: UUID) -> object: ...


async def project_inspection_scope(
    *, repository: object, tenant_id: UUID, run_id: UUID,
) -> tuple[str, str, str]:
    """Validate the original authoritative scope without reading workspace files."""
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
    return project, session, backend


async def inspect_project(
    *, repository: object, tenant_id: UUID, run_id: UUID, project_root: Path,
) -> Mapping[str, JsonValue]:
    project, session, backend = await project_inspection_scope(
        repository=repository, tenant_id=tenant_id, run_id=run_id,
    )

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
    try:
        files = ProjectWorkspaceStore(project_root).list_files(tenant_id, project, session)
        discovery = additional_stack_discovery(tuple(item.path for item in files))
    except (OSError, ValueError, RuntimeError):
        issues.append({'path': 'project', 'reason': 'stack_discovery_unavailable'})
        result['issues'] = tuple(issues)
        result['install_plan_state'] = 'blocked'
    else:
        if discovery is not None:
            result['stack_discovery'] = discovery
            issues.append({'path': 'project', 'reason': 'stack_execution_unsupported'})
            result['issues'] = tuple(issues)
            result['operations'] = {'build': 'unknown', 'test': 'unknown'}
            result['install_plan_state'] = 'blocked'
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
