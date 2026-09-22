from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
import zipfile
from collections.abc import Awaitable, Mapping
from inspect import isawaitable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol, cast
from uuid import UUID, uuid4

from agent_hub.capabilities.manifest import (
    is_safe_manifest_name,
    project_capability_manifest_item,
)
from agent_hub.capabilities.scoped_read import ScopedReadError, read_scoped_file
from agent_hub.capabilities.tools.calculator import Calculator
from agent_hub.capabilities.tools.registry import ToolRegistry
from agent_hub.documents.docx import DocxBlueprint, build_docx
from agent_hub.documents.pptx import PptxBlueprint, build_pptx
from agent_hub.files.generated import (
    DOCX_MIME_TYPE,
    PPTX_MIME_TYPE,
    ZIP_MIME_TYPE,
    GeneratedFileStore,
    safe_generated_filename,
)
from agent_hub.files.workspace import ProjectWorkspaceStore
from agent_hub.project_preflight import build_project_preflight_files
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.base import SkillInvocation, SkillSandbox
from agent_hub.skills.sandbox.systemd import SystemdSkillSandbox

_SAFE_CAPABILITY_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_DOCX_TOOL = "document.generate_docx"
_PPTX_TOOL = "presentation.generate_pptx"
_PROJECT_PREFLIGHT_TOOL = "project.preflight_architecture"
_PROJECT_ZIP_TOOL = "project.generate_zip"
_MAX_PROJECT_FILES = 64
_MAX_PROJECT_FILE_BYTES = 256_000
_MAX_PROJECT_ZIP_SOURCE_BYTES = 2_000_000
_DOTTED_BUILT_INS = frozenset({
    "calculator.evaluate",
    "workspace.read",
    _DOCX_TOOL,
    _PPTX_TOOL,
    _PROJECT_PREFLIGHT_TOOL,
    _PROJECT_ZIP_TOOL,
})
_REPLAY_SAFE = frozenset({
    "calculator",
    "calculator_evaluate",
    "calculator.evaluate",
    "read_context",
    "workspace_read",
    "workspace.read",
    _DOCX_TOOL,
    _PPTX_TOOL,
    _PROJECT_PREFLIGHT_TOOL,
    _PROJECT_ZIP_TOOL,
})
_BUILTIN_ALIASES = {
    "calculator.evaluate": "calculator",
    "workspace.read": "workspace_read",
}
_MANIFEST_BUILTINS = (
    "calculator.evaluate",
    _DOCX_TOOL,
    _PPTX_TOOL,
    _PROJECT_PREFLIGHT_TOOL,
    _PROJECT_ZIP_TOOL,
    "read_context",
    "workspace.read",
)


class RuntimeCapabilityError(RuntimeError):
    """Stable runtime capability failure."""


class CapabilityManifestSource(Protocol):
    def manifests(self) -> Mapping[str, JsonValue]: ...


class TenantCapabilityManifestSource(Protocol):
    def manifests_for_tenant(self, tenant_id: UUID) -> Mapping[str, JsonValue]: ...


CapabilityManifestProvider = CapabilityManifestSource | TenantCapabilityManifestSource


class RuntimeCapabilityGateway:
    """Production capability executor for non-dangerous built-ins and approved skills."""

    def __init__(
        self,
        *,
        skill_store_dir: Path,
        tenant_id: UUID | None = None,
        workspace_root: Path | None = None,
        generated_artifact_dir: Path | None = None,
        project_workspace_dir: Path | None = None,
        run_repository: object | None = None,
        skill_sandbox: SkillSandbox | None = None,
        calculator: Calculator | None = None,
        tool_registry: CapabilityManifestProvider | None = None,
    ) -> None:
        self._skill_store_dir = skill_store_dir
        self._workspace_root = workspace_root
        self._project_workspace_dir = project_workspace_dir
        self._run_repository = run_repository
        self._generated_file_store = (
            GeneratedFileStore(generated_artifact_dir) if generated_artifact_dir is not None else None
        )
        self._project_workspace_store = (
            ProjectWorkspaceStore(project_workspace_dir)
            if project_workspace_dir is not None
            else None
        )
        self._skill_sandbox = skill_sandbox or SystemdSkillSandbox()
        self._calculator = calculator or Calculator()
        self._tool_registry = tool_registry
        self._tenant_id = tenant_id

    def is_replay_safe(self, name: str) -> bool:
        normalized_name = _normalize_tool_name(name)
        if normalized_name in _REPLAY_SAFE:
            return True
        if self._tool_registry is None:
            return False
        return _manifest_replay_safe(
            self._tool_registry,
            self._tenant_id,
            normalized_name,
        )

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        normalized_name = _normalize_tool_name(name)
        manifest_builtin_name = _manifest_builtin_name(normalized_name)
        if manifest_builtin_name is not None:
            return self._builtin_availability_reason(manifest_builtin_name) is None
        if normalized_name in _REPLAY_SAFE:
            return True
        if self._tool_registry is not None and _manifest_available(
            self._tool_registry,
            tenant_id,
            normalized_name,
        ):
            return True
        if _SAFE_CAPABILITY_NAME.fullmatch(normalized_name) is None:
            return False
        return self._skill_package_path(tenant_id, normalized_name).is_file()

    async def ensure_tenant_loaded(self, tenant_id: UUID) -> None:
        if self._tool_registry is None:
            return
        ensure_tenant_loaded = getattr(self._tool_registry, "ensure_tenant_loaded", None)
        if not callable(ensure_tenant_loaded):
            return
        try:
            result = ensure_tenant_loaded(tenant_id)
            if isawaitable(result):
                await cast(Awaitable[object], result)
        except Exception:  # noqa: BLE001 - optional inventory preparation must fail closed.
            return

    async def refresh_tenant(self, tenant_id: UUID) -> None:
        if self._tool_registry is None:
            return
        refresh_tenant = getattr(self._tool_registry, "refresh_tenant", None)
        if not callable(refresh_tenant):
            return
        try:
            result = refresh_tenant(tenant_id)
            if isawaitable(result):
                await cast(Awaitable[object], result)
        except Exception:  # noqa: BLE001 - optional inventory refresh must fail closed.
            return

    def capability_manifest(
        self,
        tenant_id: UUID,
        *,
        extra_sources: tuple[CapabilityManifestProvider, ...] = (),
    ) -> Mapping[str, JsonValue]:
        builtin_items = tuple(
            self._builtin_manifest_item(name)
            for name in _MANIFEST_BUILTINS
        )
        skill_items = self._skill_manifest_items(tenant_id)
        existing_items = (*builtin_items, *skill_items)
        return {
            "schema_version": 1,
            "capabilities": (
                *builtin_items,
                *skill_items,
                *self._registry_manifest_items(
                    tenant_id,
                    existing_items,
                    extra_sources=extra_sources,
                ),
            ),
        }

    async def execute(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        name: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        _require_safe("actor", actor)
        normalized_name = _normalize_tool_name(name)
        _require_safe("capability name", normalized_name)
        _require_safe("idempotency key", idempotency_key, max_length=160)
        if normalized_name in {"calculator", "calculator_evaluate"}:
            return self._execute_calculator(arguments)
        if normalized_name == "read_context":
            return await self._execute_read_context(tenant_id, run_id, arguments)
        if normalized_name == "workspace_read":
            return await self._execute_workspace_read(tenant_id, run_id, arguments)
        if normalized_name == _DOCX_TOOL:
            return self._execute_generate_docx(tenant_id, run_id, arguments)
        if normalized_name == _PPTX_TOOL:
            return self._execute_generate_pptx(tenant_id, run_id, arguments)
        if normalized_name == _PROJECT_PREFLIGHT_TOOL:
            return self._execute_project_preflight(tenant_id, arguments)
        if normalized_name == _PROJECT_ZIP_TOOL:
            return self._execute_generate_project_zip(tenant_id, run_id, arguments)
        return await self._execute_skill(
            tenant_id=tenant_id,
            run_id=run_id,
            actor=actor,
            skill_id=normalized_name,
            arguments=arguments,
            idempotency_key=idempotency_key,
        )

    def _execute_calculator(self, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        expression = arguments.get("expression")
        if not isinstance(expression, str):
            raise RuntimeCapabilityError("calculator requires expression")
        result = self._calculator.evaluate(expression)
        return {"value": str(result.value)}

    async def _execute_read_context(
        self, tenant_id: UUID, run_id: UUID, arguments: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        if "path" in arguments:
            return await self._execute_workspace_read(tenant_id, run_id, arguments)
        query = arguments.get("query")
        if query is None:
            query = arguments.get("text")
        if query is not None and (not isinstance(query, str) or not query.strip()):
            raise RuntimeCapabilityError("read_context query must be a nonblank string")
        normalized_query = query.strip() if isinstance(query, str) else None
        matches = await self._read_context_artifact_matches(tenant_id, run_id, normalized_query)
        if matches:
            paths = _artifact_context_paths(matches)
            summary = (
                "Generated project artifact context is available."
                if not paths
                else "Generated project artifact context is available: "
                + ", ".join(paths[:12])
                + (" ..." if len(paths) > 12 else "")
            )
            return {
                "query": normalized_query,
                "matches": matches,
                "summary": summary,
                "truncated": False,
            }
        return {
            "query": normalized_query,
            "matches": (),
            "summary": "No additional runtime context is available for this query.",
            "truncated": False,
        }

    async def _read_context_artifact_matches(
        self,
        tenant_id: UUID,
        run_id: UUID,
        query: str | None,
    ) -> tuple[Mapping[str, JsonValue], ...]:
        if not _read_context_query_requests_artifacts(query):
            return ()
        artifacts_method = getattr(self._run_repository, "artifacts", None)
        if not callable(artifacts_method):
            return ()
        artifact_result = artifacts_method(tenant_id, run_id)
        if isawaitable(artifact_result):
            artifact_result = await cast(Awaitable[object], artifact_result)
        if not isinstance(artifact_result, tuple | list):
            return ()
        matches: list[Mapping[str, JsonValue]] = []
        for artifact in artifact_result:
            if not isinstance(artifact, Mapping):
                continue
            match = _project_artifact_context_match(artifact)
            if match is not None:
                matches.append(match)
        return tuple(matches[:8])

    async def _execute_workspace_read(
        self, tenant_id: UUID, run_id: UUID, arguments: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        path = arguments.get("path")
        if not isinstance(path, str):
            raise RuntimeCapabilityError("workspace reader requires path")
        try:
            result = await read_scoped_file(
                repository=self._run_repository, tenant_id=tenant_id, run_id=run_id,
                project_root=self._project_workspace_dir, attachment_root=self._workspace_root,
                path=path,
            )
        except ScopedReadError as error:
            raise RuntimeCapabilityError(str(error)) from None
        return {
            "path": result.relative_path,
            "text": result.text,
            "truncated": result.truncated,
        }

    def _execute_generate_docx(
        self,
        tenant_id: UUID,
        run_id: UUID,
        arguments: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        store = self._require_generated_file_store()
        title = _required_string(arguments, "title")
        blueprint = DocxBlueprint(
            title=title,
            subtitle=_optional_string(arguments, "subtitle"),
            sections=_optional_mapping_list(arguments, "sections"),
        )
        filename = _filename(arguments, title=title, extension=".docx")
        artifact_id = uuid4()
        with tempfile.TemporaryDirectory(prefix="agent-hub-docx-") as temporary_dir:
            output = Path(temporary_dir) / filename
            try:
                build_docx(blueprint, output)
            except ValueError as error:
                raise RuntimeCapabilityError(str(error)) from None
            metadata = store.store_bytes(
                tenant_id=tenant_id,
                run_id=run_id,
                artifact_id=artifact_id,
                filename=filename,
                mime_type=DOCX_MIME_TYPE,
                data=output.read_bytes(),
            )
        return _file_result(
            artifact_id=artifact_id,
            public_metadata=metadata.to_public_dict(),
            internal_metadata=metadata.to_content_file(),
            presentation=_generated_file_presentation(arguments),
            summary=f"Generated DOCX artifact {metadata.filename}.",
        )

    def _execute_generate_pptx(
        self,
        tenant_id: UUID,
        run_id: UUID,
        arguments: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        store = self._require_generated_file_store()
        title = _required_string(arguments, "title")
        blueprint = PptxBlueprint(
            title=title,
            subtitle=_optional_string(arguments, "subtitle"),
            template_id=_optional_string(arguments, "template_id") or "consulting-clean",
            slides=_optional_mapping_list(arguments, "slides"),
        )
        filename = _filename(arguments, title=title, extension=".pptx")
        artifact_id = uuid4()
        with tempfile.TemporaryDirectory(prefix="agent-hub-pptx-") as temporary_dir:
            output = Path(temporary_dir) / filename
            try:
                build_pptx(blueprint, output)
            except ValueError as error:
                if str(error).startswith("unknown PPTX template:"):
                    raise RuntimeCapabilityError("template_id is invalid") from None
                raise RuntimeCapabilityError(str(error)) from None
            metadata = store.store_bytes(
                tenant_id=tenant_id,
                run_id=run_id,
                artifact_id=artifact_id,
                filename=filename,
                mime_type=PPTX_MIME_TYPE,
                data=output.read_bytes(),
            )
        return _file_result(
            artifact_id=artifact_id,
            public_metadata=metadata.to_public_dict(),
            internal_metadata=metadata.to_content_file(),
            presentation=_generated_file_presentation(arguments),
            summary=f"Generated PPTX artifact {metadata.filename}.",
        )

    def _execute_generate_project_zip(
        self,
        tenant_id: UUID,
        run_id: UUID,
        arguments: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        store = self._require_generated_file_store()
        title = _required_string(arguments, "title")
        files = _project_files(arguments)
        workspace_files = self._copy_project_files_to_workspace(tenant_id, arguments, files)
        filename = _filename(arguments, title=title, extension=".zip")
        artifact_id = uuid4()
        with tempfile.TemporaryDirectory(prefix="agent-hub-project-") as temporary_dir:
            output = Path(temporary_dir) / filename
            with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path, data in sorted(files.items()):
                    archive.writestr(path, data)
            metadata = store.store_bytes(
                tenant_id=tenant_id,
                run_id=run_id,
                artifact_id=artifact_id,
                filename=filename,
                mime_type=ZIP_MIME_TYPE,
                data=output.read_bytes(),
            )
        result = dict(
            _file_result(
                artifact_id=artifact_id,
                public_metadata=metadata.to_public_dict(),
                internal_metadata=metadata.to_content_file(),
                presentation=_generated_file_presentation(arguments, default="final_attachment"),
                summary=f"Generated project ZIP artifact {metadata.filename}.",
            )
        )
        if workspace_files:
            result["workspace_files"] = workspace_files
        return result

    def _execute_project_preflight(
        self,
        tenant_id: UUID,
        arguments: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        if self._project_workspace_store is None:
            raise RuntimeCapabilityError("project workspace store is not configured")
        project_id = _optional_string(arguments, "project_id")
        session_id = _optional_string(arguments, "workspace_session_id")
        if project_id is None or session_id is None:
            raise RuntimeCapabilityError("project preflight requires project_id and workspace_session_id")
        files = build_project_preflight_files(
            title=_optional_string(arguments, "title") or "Project Architecture Preflight",
            request=_required_string(arguments, "request"),
        )
        workspace_files = tuple(
            cast(
                Mapping[str, JsonValue],
                self._project_workspace_store.write_bytes(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    session_id=session_id,
                    relative_path=path,
                    data=data,
                    mime_type=_workspace_mime_type(path),
                ).to_public_dict(),
            )
            for path, data in sorted(files.items())
        )
        return {
            "summary": "Generated project architecture preflight plan and browser map.",
            "plan_path": "PROJECT_ARCHITECTURE_PLAN.md",
            "graph_path": "architecture-map.html",
            "plan_download_url": _workspace_file_download_url(
                self._project_workspace_store,
                project_id,
                session_id,
                "PROJECT_ARCHITECTURE_PLAN.md",
            ),
            "graph_download_url": _workspace_file_download_url(
                self._project_workspace_store,
                project_id,
                session_id,
                "architecture-map.html",
            ),
            "workspace_files": workspace_files,
        }

    def _copy_project_files_to_workspace(
        self,
        tenant_id: UUID,
        arguments: Mapping[str, JsonValue],
        files: Mapping[str, bytes],
    ) -> tuple[Mapping[str, JsonValue], ...]:
        if self._project_workspace_store is None:
            return ()
        project_id = _optional_string(arguments, "project_id")
        session_id = _optional_string(arguments, "workspace_session_id")
        if project_id is None or session_id is None:
            return ()
        stored = [
            self._project_workspace_store.write_bytes(
                tenant_id=tenant_id,
                project_id=project_id,
                session_id=session_id,
                relative_path=path,
                data=data,
                mime_type=_workspace_mime_type(path),
            )
            for path, data in sorted(files.items())
        ]
        return tuple(cast(Mapping[str, JsonValue], item.to_public_dict()) for item in stored)

    def _require_generated_file_store(self) -> GeneratedFileStore:
        if self._generated_file_store is None:
            raise RuntimeCapabilityError("generated artifact store is not configured")
        return self._generated_file_store

    async def _execute_skill(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        actor: str,
        skill_id: str,
        arguments: Mapping[str, JsonValue],
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        package_path = self._skill_package_path(tenant_id, skill_id)
        if not package_path.is_file():
            raise RuntimeCapabilityError("skill is not installed or approved")
        archive_bytes = package_path.read_bytes()
        package_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        execution_id = _execution_id(actor, skill_id, idempotency_key)
        writable_tmp_path = self._skill_store_dir / str(tenant_id) / "tmp" / execution_id
        writable_tmp_path.mkdir(parents=True, exist_ok=True)
        try:
            result = await self._skill_sandbox.run(
                SkillInvocation(
                    execution_id=execution_id,
                    package_path=package_path,
                    package_sha256=package_sha256,
                    input={
                        "run_id": str(run_id),
                        "actor": actor,
                        "skill": skill_id,
                        "arguments": _json_dict(arguments),
                    },
                    timeout_seconds=300,
                    output_limit_bytes=1_000_000,
                    memory_limit_bytes=512 * 1024 * 1024,
                    cpu_quota_percent=100,
                    writable_tmp_path=writable_tmp_path,
                )
            )
        finally:
            shutil.rmtree(writable_tmp_path, ignore_errors=True)
        if result.timed_out:
            raise RuntimeCapabilityError("skill execution timed out")
        if result.exit_code != 0:
            raise RuntimeCapabilityError("skill execution failed")
        parsed = _parse_stdout(result.stdout)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "result": parsed,
        }

    def _skill_package_path(self, tenant_id: UUID, skill_id: str) -> Path:
        root = (self._skill_store_dir / str(tenant_id)).resolve()
        target = (root / f"{skill_id}.zip").resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise RuntimeCapabilityError("skill path is invalid") from None
        return target

    def _builtin_manifest_item(self, name: str) -> Mapping[str, JsonValue]:
        availability_reason = self._builtin_availability_reason(name)
        return {
            "id": name,
            "kind": "builtin",
            "adapter": "runtime_builtin",
            "permission_class": _builtin_permission_class(name),
            "sandbox_profile": _builtin_sandbox_profile(name),
            "available": availability_reason is None,
            "availability_reason": availability_reason,
            "replay_safe": self.is_replay_safe(name),
            "aliases": _builtin_aliases(name),
        }

    def _builtin_availability_reason(self, name: str) -> str | None:
        if name == "workspace.read":
            if self._workspace_root is None and self._project_workspace_dir is None:
                return "workspace_root_not_configured"
            if not callable(getattr(self._run_repository, "get", None)):
                return "workspace_scope_not_configured"
        if name == _PROJECT_PREFLIGHT_TOOL and self._project_workspace_store is None:
            return "project_workspace_store_not_configured"
        if name in {_DOCX_TOOL, _PPTX_TOOL, _PROJECT_ZIP_TOOL} and (
            self._generated_file_store is None
        ):
            return "generated_artifact_store_not_configured"
        return None

    def _skill_manifest_items(self, tenant_id: UUID) -> tuple[Mapping[str, JsonValue], ...]:
        tenant_dir = self._skill_store_dir / str(tenant_id)
        if not tenant_dir.is_dir():
            return ()
        items: list[Mapping[str, JsonValue]] = []
        for package_path in sorted(tenant_dir.glob("*.zip")):
            skill_id = package_path.stem
            if _SAFE_CAPABILITY_NAME.fullmatch(skill_id) is None:
                continue
            items.append(
                {
                    "id": skill_id,
                    "kind": "skill",
                    "adapter": "skill_sandbox",
                    "permission_class": "skill.use",
                    "sandbox_profile": "systemd_skill_sandbox",
                    "available": True,
                    "availability_reason": None,
                    "replay_safe": False,
                    "aliases": (),
                }
            )
        return tuple(items)

    def _registry_manifest_items(
        self,
        tenant_id: UUID,
        existing_items: tuple[Mapping[str, JsonValue], ...],
        *,
        extra_sources: tuple[CapabilityManifestProvider, ...] = (),
    ) -> tuple[Mapping[str, JsonValue], ...]:
        manifest_sources = (
            *((self._tool_registry,) if self._tool_registry is not None else ()),
            *extra_sources,
        )
        if not manifest_sources:
            return ()
        seen_ids: set[str] = set()
        for item in existing_items:
            item_id = item.get("id")
            if isinstance(item_id, str):
                seen_ids.add(item_id)
        seen_names = set(seen_ids)
        seen_names.update(
            alias
            for item in existing_items
            for alias in _tuple_strings(item.get("aliases"))
        )
        projected: list[Mapping[str, JsonValue]] = []
        for source in manifest_sources:
            for raw_item in _manifest_source_items(source, tenant_id):
                projected_item = project_capability_manifest_item(
                    raw_item,
                    seen_ids,
                    seen_names,
                )
                if projected_item is None:
                    continue
                projected.append(projected_item)
                item_id = cast(str, projected_item["id"])
                seen_ids.add(item_id)
                seen_names.add(item_id)
                seen_names.update(_tuple_strings(projected_item.get("aliases")))
        return tuple(projected)


def _manifest_source_items(
    source: CapabilityManifestProvider,
    tenant_id: UUID,
) -> tuple[Mapping[str, JsonValue], ...]:
    try:
        tenant_manifest = getattr(source, "manifests_for_tenant", None)
        if callable(tenant_manifest):
            manifest = tenant_manifest(tenant_id)
        else:
            manifest = cast(CapabilityManifestSource, source).manifests()
    except Exception:  # noqa: BLE001 - optional manifest sources must fail closed.
        return ()
    if not isinstance(manifest, Mapping):
        return ()
    if manifest.get("schema_version") != 1:
        return ()
    raw_items = manifest.get("capabilities")
    if not isinstance(raw_items, tuple | list):
        return ()
    return tuple(item for item in raw_items if isinstance(item, Mapping))


def _manifest_replay_safe(
    source: CapabilityManifestProvider,
    tenant_id: UUID | None,
    name: str,
) -> bool:
    allow_missing_available = isinstance(source, ToolRegistry)
    items = _replay_safe_manifest_source_items(source, tenant_id)
    ambiguous_names = _ambiguous_manifest_names(items)
    for item in items:
        available = item.get("available")
        if (
            available is not True
            and not (allow_missing_available and available is None)
        ) or item.get("replay_safe") is not True:
            continue
        item_names = _manifest_item_names(item)
        if not item_names or any(item_name in ambiguous_names for item_name in item_names):
            continue
        if _RESERVED_REPLAY_SAFE_NAMES.intersection(item_names):
            continue
        item_id = item.get("id")
        aliases = _tuple_strings(item.get("aliases"))
        if item_id == name or name in aliases:
            return True
    return False


def _manifest_available(
    source: CapabilityManifestProvider,
    tenant_id: UUID,
    name: str,
) -> bool:
    if not is_safe_manifest_name(name):
        return False
    items = _manifest_source_items(source, tenant_id)
    ambiguous_names = _ambiguous_manifest_names(items)
    for item in items:
        if item.get("available") is not True:
            continue
        item_names = _manifest_item_names(item)
        if not item_names or any(item_name in ambiguous_names for item_name in item_names):
            continue
        item_id = item.get("id")
        aliases = _tuple_strings(item.get("aliases"))
        if item_id == name or name in aliases:
            return True
    return False


def _replay_safe_manifest_source_items(
    source: CapabilityManifestProvider,
    tenant_id: UUID | None,
) -> tuple[Mapping[str, JsonValue], ...]:
    if tenant_id is not None:
        return _manifest_source_items(source, tenant_id)
    try:
        plain_manifest = getattr(source, "manifests", None)
        if not callable(plain_manifest):
            return ()
        manifest = plain_manifest()
    except Exception:  # noqa: BLE001 - optional replay metadata must fail closed.
        return ()
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1:
        return ()
    raw_items = manifest.get("capabilities")
    if not isinstance(raw_items, tuple | list):
        return ()
    return tuple(item for item in raw_items if isinstance(item, Mapping))


def _ambiguous_manifest_names(items: tuple[Mapping[str, JsonValue], ...]) -> frozenset[str]:
    counts: dict[str, int] = {}
    for item in items:
        for name in _manifest_item_names(item):
            counts[name] = counts.get(name, 0) + 1
    return frozenset(name for name, count in counts.items() if count > 1)


def _manifest_item_names(item: Mapping[str, JsonValue]) -> tuple[str, ...]:
    item_id = item.get("id")
    names = [item_id] if isinstance(item_id, str) else []
    names.extend(_tuple_strings(item.get("aliases")))
    return tuple(names)


_RESERVED_REPLAY_SAFE_NAMES = frozenset(
    {
        *_REPLAY_SAFE,
        *_BUILTIN_ALIASES,
        *_BUILTIN_ALIASES.values(),
    }
)


def _require_safe(name: str, value: str, *, max_length: int = 128) -> None:
    if name == "capability name" and value in _DOTTED_BUILT_INS:
        return
    if (
        not isinstance(value, str)
        or len(value) > max_length
        or _SAFE_CAPABILITY_NAME.fullmatch(value) is None
    ):
        raise RuntimeCapabilityError(f"{name} is invalid")


def _normalize_tool_name(name: str) -> str:
    return _BUILTIN_ALIASES.get(name, name)


def _manifest_builtin_name(name: str) -> str | None:
    if name in _MANIFEST_BUILTINS:
        return name
    for builtin_name, alias in _BUILTIN_ALIASES.items():
        if name == alias:
            return builtin_name
    return None


def _builtin_aliases(name: str) -> tuple[str, ...]:
    alias = _BUILTIN_ALIASES.get(name)
    return () if alias is None else (alias,)


def _tuple_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple | list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _builtin_permission_class(name: str) -> str:
    if name == "calculator.evaluate":
        return "calculator.evaluate"
    if name == "read_context":
        return "context.read"
    if name == "workspace.read":
        return "file.read"
    return "file.create"


def _builtin_sandbox_profile(name: str) -> str:
    if name in {"calculator.evaluate", "read_context"}:
        return "in_process"
    if name == "workspace.read":
        return "workspace_read"
    if name == _PROJECT_PREFLIGHT_TOOL:
        return "project_workspace_store"
    return "generated_artifact_store"


def _required_string(arguments: Mapping[str, JsonValue], field_name: str) -> str:
    value = arguments.get(field_name)
    if not isinstance(value, str):
        raise RuntimeCapabilityError(f"{field_name} must be a string")
    if not value.strip():
        raise RuntimeCapabilityError(f"{field_name} must not be empty")
    return value


def _optional_string(arguments: Mapping[str, JsonValue], field_name: str) -> str | None:
    value = arguments.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeCapabilityError(f"{field_name} must be a string")
    return value


def _optional_mapping_list(
    arguments: Mapping[str, JsonValue],
    field_name: str,
) -> list[dict[str, object]]:
    value = arguments.get(field_name)
    if value is None:
        return []
    if not isinstance(value, list | tuple):
        raise RuntimeCapabilityError(f"{field_name} must be a list")
    items: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise RuntimeCapabilityError(f"{field_name} items must be objects")
        items.append(dict(item))
    return items


def _filename(arguments: Mapping[str, JsonValue], *, title: str, extension: str) -> str:
    value = arguments.get("filename")
    if value is not None:
        if not isinstance(value, str):
            raise RuntimeCapabilityError("filename must be a string")
        try:
            return safe_generated_filename(value)
        except ValueError as error:
            raise RuntimeCapabilityError(str(error)) from None
    basename = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    if not basename:
        basename = "artifact"
    try:
        return safe_generated_filename(f"{basename[:80]}{extension}")
    except ValueError as error:
        raise RuntimeCapabilityError(str(error)) from None


def _generated_file_presentation(
    arguments: Mapping[str, JsonValue], *, default: str = "step_detail"
) -> str:
    value = arguments.get("presentation")
    if value is None:
        return default
    if value in {"step_detail", "final_attachment"}:
        return str(value)
    raise RuntimeCapabilityError("presentation must be step_detail or final_attachment")


def _project_files(arguments: Mapping[str, JsonValue]) -> dict[str, bytes]:
    raw_files = arguments.get("files")
    raw_entries = _project_file_entries(raw_files)
    if not raw_entries or len(raw_entries) > _MAX_PROJECT_FILES:
        raise RuntimeCapabilityError("files must contain 1 to 64 entries")
    files: dict[str, bytes] = {}
    total_bytes = 0
    for raw_path, raw_content in raw_entries:
        if not isinstance(raw_path, str):
            raise RuntimeCapabilityError("file paths must be strings")
        path = _project_archive_path(raw_path)
        if isinstance(raw_content, str):
            data = raw_content.encode("utf-8")
        else:
            raise RuntimeCapabilityError("file contents must be strings")
        if len(data) > _MAX_PROJECT_FILE_BYTES:
            raise RuntimeCapabilityError("file content is too large")
        total_bytes += len(data)
        if total_bytes > _MAX_PROJECT_ZIP_SOURCE_BYTES:
            raise RuntimeCapabilityError("project content is too large")
        if path in files:
            raise RuntimeCapabilityError("duplicate file path after normalization")
        files[path] = data
    return files


def _project_file_entries(raw_files: object) -> tuple[tuple[object, object], ...]:
    if isinstance(raw_files, Mapping):
        unwrapped = _unwrap_project_files_item(raw_files)
        return tuple(unwrapped.items())
    if isinstance(raw_files, list | tuple):
        entries: list[tuple[object, object]] = []
        for item in raw_files:
            if not isinstance(item, Mapping) or len(item) != 1:
                raise RuntimeCapabilityError("file entries must map one path to content")
            entries.append(next(iter(item.items())))
        return tuple(entries)
    raise RuntimeCapabilityError("files must be an object or list")


def _unwrap_project_files_item(raw_files: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    if set(raw_files) != {"item"}:
        return raw_files
    item = raw_files.get("item")
    if isinstance(item, Mapping):
        return item
    return raw_files


def _project_archive_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    if not normalized or normalized != path.strip():
        raise RuntimeCapabilityError("file path is invalid")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(path)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or any(part in {"", ".", ".."} for part in posix.parts)
        or len(posix.parts) > 8
    ):
        raise RuntimeCapabilityError("file path is invalid")
    for part in posix.parts:
        try:
            safe_generated_filename(part)
        except ValueError as error:
            raise RuntimeCapabilityError(str(error)) from None
    return posix.as_posix()


def _workspace_mime_type(path: str) -> str:
    lowered = path.lower()
    if lowered.endswith(".md"):
        return "text/markdown"
    if lowered.endswith(".json"):
        return "application/json"
    if lowered.endswith(".csv"):
        return "text/csv"
    if lowered.endswith(".html"):
        return "text/html"
    if lowered.endswith(".css"):
        return "text/css"
    if lowered.endswith(".js"):
        return "text/javascript"
    if lowered.endswith(".ts"):
        return "text/typescript"
    if lowered.endswith(".py"):
        return "text/x-python"
    return "text/plain"


def _workspace_file_download_url(
    store: ProjectWorkspaceStore,
    project_id: str,
    session_id: str,
    path: str,
) -> str:
    bundle_url = store.bundle_download_url(project_id, session_id)
    base_url = bundle_url.rsplit("/bundle/download", 1)[0]
    return f"{base_url}/files/download?path={path}"


def _read_context_query_requests_artifacts(query: str | None) -> bool:
    if query is None:
        return True
    lowered = query.casefold()
    markers = (
        "artifact",
        "bundle",
        "file",
        "implementer",
        "package",
        "project",
        "source",
        "test",
        "workspace",
        "workspace_bundle",
        "zip",
        "产物",
        "文件",
        "源码",
        "项目",
    )
    return any(marker in lowered for marker in markers)


def _project_artifact_context_match(
    artifact: Mapping[object, object],
) -> Mapping[str, JsonValue] | None:
    content = artifact.get("content")
    if not isinstance(content, Mapping):
        return None
    result = content.get("result")
    if not isinstance(result, Mapping):
        return None
    file_payload = _public_string_mapping(result.get("file"))
    workspace_files = _public_workspace_files(result.get("workspace_files"))
    flags = _public_artifact_flags(result)
    if file_payload is None and not workspace_files and not flags:
        return None
    artifact_id = result.get("artifact_id") or artifact.get("id")
    match: dict[str, JsonValue] = {
        "kind": "generated_project_artifact",
        "summary": _public_string(result.get("summary"))
        or "Generated project artifact is available.",
    }
    if isinstance(artifact_id, str) and artifact_id.strip():
        match["artifact_id"] = artifact_id.strip()
    if file_payload is not None:
        match["file"] = file_payload
    if workspace_files:
        match["workspace_files"] = workspace_files
        paths = tuple(
            path
            for item in workspace_files
            if isinstance(path := item.get("path"), str) and path
        )
        if paths:
            match["file_paths"] = paths
    match.update(flags)
    return match


def _artifact_context_paths(matches: tuple[Mapping[str, JsonValue], ...]) -> list[str]:
    paths: list[str] = []
    for match in matches:
        raw_paths = match.get("file_paths")
        if not isinstance(raw_paths, tuple):
            continue
        for path in raw_paths:
            if isinstance(path, str) and path not in paths:
                paths.append(path)
    return paths


def _public_artifact_flags(result: Mapping[object, object]) -> dict[str, JsonValue]:
    flags: dict[str, JsonValue] = {}
    for key in (
        "deliverable_quality",
        "agent_standard_verification",
        "discussion_trace",
        "plugin_contract",
    ):
        value = result.get(key)
        if isinstance(value, Mapping):
            public = _public_string_mapping(value)
            if public:
                flags[key] = public
    return flags


def _public_workspace_files(value: object) -> tuple[Mapping[str, JsonValue], ...]:
    if not isinstance(value, tuple | list):
        return ()
    files: list[Mapping[str, JsonValue]] = []
    for item in value[:64]:
        if not isinstance(item, Mapping):
            continue
        public: dict[str, JsonValue] = {}
        for key in ("path", "relative_path", "filename", "mime_type", "download_url", "sha256"):
            raw = item.get(key)
            if isinstance(raw, str) and raw.strip():
                public["path" if key == "relative_path" else key] = raw.strip()
        size = item.get("size", item.get("size_bytes"))
        if type(size) is int:
            public["size"] = size
        if public:
            files.append(public)
    return tuple(files)


def _public_string_mapping(value: object) -> Mapping[str, JsonValue] | None:
    if not isinstance(value, Mapping):
        return None
    public: dict[str, JsonValue] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            continue
        if isinstance(raw_value, str | bool | int):
            public[raw_key] = raw_value
        elif isinstance(raw_value, tuple | list):
            string_items = tuple(item for item in raw_value if isinstance(item, str))
            if len(string_items) == len(raw_value):
                public[raw_key] = string_items
    return public or None


def _public_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _file_result(
    *,
    artifact_id: UUID,
    public_metadata: dict[str, str | int],
    internal_metadata: dict[str, str | int],
    presentation: str,
    summary: str,
) -> Mapping[str, JsonValue]:
    public_payload: dict[str, JsonValue] = {
        "artifact_id": str(artifact_id),
        **public_metadata,
    }
    internal_payload: dict[str, JsonValue] = {
        "artifact_id": str(artifact_id),
        **internal_metadata,
    }
    return {
        "artifact_id": str(artifact_id),
        "file": public_payload,
        "metadata": internal_payload,
        "presentation": presentation,
        "summary": summary,
    }


def _execution_id(actor: str, skill_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{actor}:{skill_id}:{idempotency_key}".encode()).hexdigest()[:24]
    return f"skill_{digest}"


def _json_dict(value: Mapping[str, JsonValue]) -> dict[str, object]:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):
        raise RuntimeCapabilityError("capability arguments are invalid")
    return parsed


def _parse_stdout(value: str) -> JsonValue:
    if not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    return _normalize_json_value(parsed)


def _normalize_json_value(value: object) -> JsonValue:
    if value is None or type(value) in {bool, int, str}:
        return cast(JsonValue, value)
    if type(value) is float:
        if not math.isfinite(value):
            raise RuntimeCapabilityError("skill stdout is not JSON serializable")
        return value
    if isinstance(value, list):
        return tuple(_normalize_json_value(item) for item in value)
    if isinstance(value, Mapping):
        return {str(key): _normalize_json_value(item) for key, item in value.items()}
    raise RuntimeCapabilityError("skill stdout is not JSON serializable")
