from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
import zipfile
from collections import OrderedDict
from collections.abc import Awaitable, Mapping
from inspect import isawaitable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from agent_hub.capabilities.project_inspection import PythonCatalogQuery
    from agent_hub.capabilities.python_project_reports import PythonProjectFailure
    from agent_hub.capabilities.review_context import _WriteDispatch
    from agent_hub.harness.tool_gateway import HarnessToolGateway
    from agent_hub.runs.project_inputs import OriginalProjectPreparer
    from agent_hub.runs.project_verification_receipts import ProjectVerificationReceiptRepository

from agent_hub.capabilities.execution_diagnostics import ExecutionDiagnostic
from agent_hub.capabilities.manifest import (
    is_safe_manifest_name,
    project_capability_manifest_item,
)
from agent_hub.capabilities.project_contracts import ManagedProjectInput, ProjectCheckContract
from agent_hub.capabilities.project_inspection import (
    PROJECT_INSPECT_TOOL,
    ProjectInspectionError,
    inspect_project,
)
from agent_hub.capabilities.project_prerequisites import ProjectPrerequisiteObservation
from agent_hub.capabilities.project_verification import (
    PROJECT_VERIFY_TOOL,
    ProjectExecutor,
    ProjectVerificationError,
    ProjectVerificationFailure,
    ProjectVerificationService,
)
from agent_hub.capabilities.scoped_read import (
    ScopedReadError,
    authorized_workspace_scope,
    read_scoped_file,
)
from agent_hub.capabilities.tools.builtin_contracts import (
    builtin_tool_description,
    builtin_tool_input_schema,
)
from agent_hub.capabilities.tools.calculator import Calculator
from agent_hub.capabilities.tools.http_read import HttpReader, HttpReadError
from agent_hub.capabilities.tools.project_zip import (
    PROJECT_ZIP_ABSOLUTE_TOTAL_SOURCE_BYTES,
    PROJECT_ZIP_MAX_FILE_BYTES,
    PROJECT_ZIP_MAX_FILES,
    PROJECT_ZIP_TOOL_NAME,
)
from agent_hub.capabilities.tools.registry import ToolRegistry
from agent_hub.capabilities.types import CapabilityRequest
from agent_hub.documents.docx import DocxBlueprint, build_docx
from agent_hub.documents.pptx import PptxBlueprint, build_pptx
from agent_hub.files.generated import (
    DOCX_MIME_TYPE,
    PPTX_MIME_TYPE,
    ZIP_MIME_TYPE,
    GeneratedFileMetadata,
    GeneratedFileStore,
    safe_generated_filename,
)
from agent_hub.files.workspace import ProjectWorkspaceFile, ProjectWorkspaceStore
from agent_hub.project_preflight import (
    build_project_preflight_files,
    project_preflight_model_input_schema,
)
from agent_hub.runtime.artifacts import ArtifactRepository, ArtifactRepositoryError
from agent_hub.runtime.continuation_artifacts import load_continuation
from agent_hub.runtime.contracts import JsonValue, TaskContext, _freeze_object
from agent_hub.skills.package import InvalidSkillPackage, SkillPackageInspector
from agent_hub.skills.sandbox.base import (
    SANDBOX_PROFILES,
    SandboxProfile,
    SkillInvocation,
    SkillResult,
    SkillSandbox,
)
from agent_hub.skills.sandbox.docker import DockerSkillSandbox
from agent_hub.skills.sandbox.remote import configured_remote_skill_sandboxes
from agent_hub.skills.sandbox.systemd import SystemdSkillSandbox
from agent_hub.subprocess_cleanup import ProcessCleanupIncomplete

_SAFE_CAPABILITY_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_DOCX_TOOL = "document.generate_docx"
_PPTX_TOOL = "presentation.generate_pptx"
_PROJECT_PREFLIGHT_TOOL = "project.preflight_architecture"
_PROJECT_ZIP_TOOL = PROJECT_ZIP_TOOL_NAME
_WORKSPACE_WRITE_TOOL = "workspace.write_text"
_WORKSPACE_PRUNE_TOOL = "workspace.prune"
_WORKSPACE_LIST_TOOL = "workspace.list"
_WORKSPACE_BUNDLE_TOOL = "workspace.bundle"
_MAX_PROJECT_FILES = PROJECT_ZIP_MAX_FILES
_MAX_PROJECT_FILE_BYTES = PROJECT_ZIP_MAX_FILE_BYTES
_MAX_PROJECT_ZIP_SOURCE_BYTES = PROJECT_ZIP_ABSOLUTE_TOTAL_SOURCE_BYTES
_DOTTED_BUILT_INS = frozenset({
    PROJECT_VERIFY_TOOL,
    PROJECT_INSPECT_TOOL,
    "calculator.evaluate",
    "http.read",
    "workspace.read",
    _DOCX_TOOL,
    _PPTX_TOOL,
    _PROJECT_PREFLIGHT_TOOL,
    _PROJECT_ZIP_TOOL,
    _WORKSPACE_WRITE_TOOL,
    _WORKSPACE_PRUNE_TOOL,
    _WORKSPACE_LIST_TOOL,
    _WORKSPACE_BUNDLE_TOOL,
})
_REPLAY_SAFE = frozenset({
    PROJECT_INSPECT_TOOL,
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
    _WORKSPACE_WRITE_TOOL,
    _WORKSPACE_PRUNE_TOOL,
    _WORKSPACE_LIST_TOOL,
    _WORKSPACE_BUNDLE_TOOL,
})
_BUILTIN_ALIASES = {
    "calculator.evaluate": "calculator",
    "workspace.read": "workspace_read",
}
_MANIFEST_BUILTINS = (
    PROJECT_VERIFY_TOOL,
    PROJECT_INSPECT_TOOL,
    "calculator.evaluate",
    "http.read",
    _DOCX_TOOL,
    _PPTX_TOOL,
    _PROJECT_PREFLIGHT_TOOL,
    _PROJECT_ZIP_TOOL,
    _WORKSPACE_WRITE_TOOL,
    _WORKSPACE_LIST_TOOL,
    _WORKSPACE_BUNDLE_TOOL,
    "read_context",
    "workspace.read",
)


class RuntimeCapabilityError(RuntimeError):
    """Stable runtime capability failure."""

    def __init__(
        self, *args: object, execution_diagnostic: ExecutionDiagnostic | None = None,
        project_failure: ProjectVerificationFailure | None = None,
        project_prerequisite: ProjectPrerequisiteObservation | None = None,
        python_project_failure: PythonProjectFailure | None = None,
    ) -> None:
        from agent_hub.capabilities.python_project_reports import PythonProjectFailure

        if execution_diagnostic is not None and type(execution_diagnostic) is not ExecutionDiagnostic:
            raise TypeError("execution_diagnostic must be ExecutionDiagnostic")
        if project_failure is not None and type(project_failure) is not ProjectVerificationFailure:
            raise TypeError("project_failure must be ProjectVerificationFailure")
        if python_project_failure is not None and (
            type(python_project_failure) is not PythonProjectFailure
            or execution_diagnostic is None
            or python_project_failure.diagnostic is not execution_diagnostic
            or project_failure is not None
        ):
            raise TypeError("python failure must retain its separate native diagnostic")
        if project_prerequisite is not None and (
            type(project_prerequisite) is not ProjectPrerequisiteObservation
            or execution_diagnostic is not None or project_failure is not None
            or python_project_failure is not None
        ):
            raise TypeError("project prerequisite must be a separate typed blocked observation")
        super().__init__(*args)
        self._execution_diagnostic = execution_diagnostic
        self._project_failure = project_failure
        self._project_prerequisite = project_prerequisite
        self._python_project_failure = python_project_failure

    @property
    def execution_diagnostic(self) -> ExecutionDiagnostic | None:
        return self._execution_diagnostic

    @property
    def project_failure(self) -> ProjectVerificationFailure | None:
        return self._project_failure

    @property
    def project_prerequisite(self) -> ProjectPrerequisiteObservation | None:
        return self._project_prerequisite

    @property
    def python_project_failure(self) -> PythonProjectFailure | None:
        return self._python_project_failure


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
        skill_sandboxes: Mapping[str, SkillSandbox] | None = None,
        calculator: Calculator | None = None,
        http_reader: HttpReader | None = None,
        tool_registry: CapabilityManifestProvider | None = None,
        project_executor: ProjectExecutor | None = None,
        artifact_repository: ArtifactRepository | None = None,
        receipt_repository: ProjectVerificationReceiptRepository | None = None,
    ) -> None:
        self._skill_store_dir = skill_store_dir
        self._workspace_root = workspace_root
        self._project_workspace_dir = project_workspace_dir
        self._run_repository = run_repository
        self._artifact_repository = artifact_repository
        self._generated_file_store = (
            GeneratedFileStore(generated_artifact_dir) if generated_artifact_dir is not None else None
        )
        self._project_workspace_store = (
            ProjectWorkspaceStore(project_workspace_dir)
            if project_workspace_dir is not None
            else None
        )
        if skill_sandbox is not None and skill_sandboxes is not None:
            raise ValueError("configure skill_sandbox or skill_sandboxes, not both")
        if skill_sandboxes is not None:
            self._skill_sandboxes = dict(skill_sandboxes)
        elif skill_sandbox is not None:
            self._skill_sandboxes = {"systemd": skill_sandbox}
        else:
            self._skill_sandboxes = {
                "systemd": SystemdSkillSandbox(),
                "docker": DockerSkillSandbox(),
                **configured_remote_skill_sandboxes(),
            }
        self._calculator = calculator or Calculator()
        self._http_reader = http_reader or HttpReader.production()
        self._tool_registry = tool_registry
        self._tenant_id = tenant_id
        self._project_executor_configured = project_executor is not None or sys.platform == "linux"
        self._project_verification = (
            ProjectVerificationService(root=project_workspace_dir, repository=run_repository,
                                       executor=project_executor,
                                       receipt_repository=receipt_repository,
                                       generated_file_store=self._generated_file_store,
                                       artifact_repository=(artifact_repository
                                           if receipt_repository is not None else None))
            if project_workspace_dir is not None else None
        )
        self._project_delivery_receipts: dict[tuple[UUID, UUID], tuple[str, str]] = {}
        self._generated_delivery_bindings: dict[tuple[UUID, UUID], Mapping[str, JsonValue]] = {}
        self._project_contract_cache: OrderedDict[tuple[UUID, UUID, str], tuple[ProjectCheckContract, int]] = OrderedDict()
        self._project_contract_cache_bytes = 0

    def original_project_preparer(self, authorizer: HarnessToolGateway) -> OriginalProjectPreparer | None:
        from agent_hub.runs.project_inputs import OriginalProjectPreparer
        from agent_hub.runs.repository import RunRepository

        if (self._project_workspace_dir is None or self._project_verification is None
                or self._run_repository is None or not self._project_executor_configured):
            return None
        return OriginalProjectPreparer(
            root=self._project_workspace_dir, repository=cast(RunRepository, self._run_repository),
            authorizer=authorizer, verifier=self._project_verification,
            artifact_repository=self._artifact_repository,
        )

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

    async def project_verification_authorization(
        self, request: CapabilityRequest,
    ) -> CapabilityRequest | None:
        if self._project_verification is None:
            return None
        return await self._project_verification.authorization_request(request)

    async def project_verification_static_authorization(
        self, request: CapabilityRequest,
    ) -> CapabilityRequest | None:
        getter = getattr(self._run_repository, 'get', None)
        if callable(getter):
            record = await getter(request.tenant_id, request.run_id)
            routing = getattr(record, 'routing_decision', None)
            if isinstance(routing, Mapping) and 'generated_project_contract_version' in routing:
                if (type(routing['generated_project_contract_version']) is not int
                        or routing['generated_project_contract_version'] != 1
                        or 'project_input' in routing or 'project_input_contract_version' in routing):
                    raise RuntimeCapabilityError('generated project contract is invalid')
                return None
        return await self.project_verification_authorization(request)

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
        return self._skill_package_availability_reason(
            self._skill_package_path(tenant_id, normalized_name)
        ) is None

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
        if normalized_name == "http.read":
            return await self._execute_http_read(arguments)
        if normalized_name == "read_context":
            return await self._execute_read_context(tenant_id, run_id, arguments)
        if normalized_name == "workspace_read":
            return await self._execute_workspace_read(tenant_id, run_id, arguments)
        if normalized_name == _DOCX_TOOL:
            return self._execute_generate_docx(tenant_id, run_id, arguments)
        if normalized_name == _PPTX_TOOL:
            return self._execute_generate_pptx(tenant_id, run_id, arguments)
        if normalized_name == _PROJECT_PREFLIGHT_TOOL:
            if await self._original_project_contract(tenant_id, run_id) is not None:
                raise RuntimeCapabilityError('original project contract requires scoped workspace operations')
            return self._execute_project_preflight(tenant_id, arguments)
        if normalized_name == _PROJECT_ZIP_TOOL:
            if await self._original_project_contract(tenant_id, run_id) is not None:
                raise RuntimeCapabilityError('original project contract requires scoped workspace operations')
            return self._execute_generate_project_zip(tenant_id, run_id, arguments)
        if normalized_name == _WORKSPACE_WRITE_TOOL:
            from agent_hub.capabilities.review_context import _claim_write_dispatch

            try:
                dispatch = _claim_write_dispatch(
                    self,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    actor=actor,
                    name=normalized_name,
                    arguments=arguments,
                    idempotency_key=idempotency_key,
                )
            except ValueError:
                raise RuntimeCapabilityError("concrete write authority unavailable") from None
            return await self._execute_workspace_write_text(
                tenant_id, run_id, arguments, _dispatch=dispatch
            )
        if normalized_name == _WORKSPACE_PRUNE_TOOL:
            return await self._execute_workspace_prune(tenant_id, run_id, arguments)
        if normalized_name == _WORKSPACE_LIST_TOOL:
            return await self._execute_workspace_list(tenant_id, run_id)
        if normalized_name == _WORKSPACE_BUNDLE_TOOL:
            return await self._execute_workspace_bundle(tenant_id, run_id, arguments)
        if normalized_name == PROJECT_INSPECT_TOOL:
            if arguments and 'python_catalog' not in arguments:
                raise RuntimeCapabilityError("project.inspect takes no arguments other than python_catalog")
            if self._project_workspace_dir is None:
                raise RuntimeCapabilityError("project inspection scope unavailable")
            try:
                from agent_hub.capabilities.project_inspection import parse_python_catalog_query

                catalog_query = parse_python_catalog_query(arguments)
                observation = dict(await inspect_project(
                    repository=self._run_repository, tenant_id=tenant_id, run_id=run_id,
                    project_root=self._project_workspace_dir,
                ))
                observation['python_build_profile'] = (
                    await self._python_build_profile(tenant_id, run_id) if catalog_query is None
                    else await self._python_build_profile(tenant_id, run_id, catalog_query=catalog_query))
                return observation
            except ProjectInspectionError as error:
                raise RuntimeCapabilityError(str(error)) from None
        if normalized_name == PROJECT_VERIFY_TOOL:
            from agent_hub.capabilities.generated_project_service import (
                GeneratedProjectDraftRejected,
                GeneratedProjectError,
                PythonMetadataPreparationApprovalRequired,
            )
            from agent_hub.capabilities.python_generated_service import (
                PythonGeneratedRecipeApprovalRequired,
            )
            if arguments:
                raise RuntimeCapabilityError("project.verify takes no arguments")
            if self._project_verification is None or not self._project_executor_configured:
                raise RuntimeCapabilityError("project execution backend is unavailable")
            self._project_delivery_receipts.pop((tenant_id, run_id), None)
            self._generated_delivery_bindings.pop((tenant_id, run_id), None)
            try:
                result = await self._project_verification.verify(
                    tenant_id=tenant_id, run_id=run_id, actor=actor, action_key=idempotency_key,
                )
                self._cache_project_verification(tenant_id, run_id, result)
                return result
            except (PythonGeneratedRecipeApprovalRequired, PythonMetadataPreparationApprovalRequired):
                raise
            except GeneratedProjectDraftRejected:
                raise
            except GeneratedProjectError as error:
                raise RuntimeCapabilityError(str(error)) from None
            except ProjectVerificationError as error:
                raise RuntimeCapabilityError(str(error), execution_diagnostic=error.diagnostic,
                                             project_failure=error.project_failure,
                                             python_project_failure=error.python_project_failure,
                                             project_prerequisite=error.project_prerequisite) from None
        return await self._execute_skill(
            tenant_id=tenant_id,
            run_id=run_id,
            actor=actor,
            skill_id=normalized_name,
            arguments=arguments,
            idempotency_key=idempotency_key,
        )

    def _cache_project_verification(
        self, tenant_id: UUID, run_id: UUID, result: Mapping[str, JsonValue],
    ) -> None:
        receipt = cast(Mapping[str, JsonValue], result["verification"])
        receipt_digest = hashlib.sha256(json.dumps(
            receipt, default=dict, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii")).hexdigest()
        self._project_delivery_receipts[(tenant_id, run_id)] = (
            cast(str, receipt["source_sha256"]), receipt_digest,
        )
        generated_binding = result.get('generated_project_acceptance')
        if isinstance(generated_binding, Mapping):
            self._generated_delivery_bindings[(tenant_id, run_id)] = generated_binding
        else:
            self._generated_delivery_bindings.pop((tenant_id, run_id), None)

    async def restore_project_verification(
        self, *, tenant_id: UUID, run_id: UUID, actor: str, action_key: str,
    ) -> Mapping[str, JsonValue]:
        self._project_delivery_receipts.pop((tenant_id, run_id), None)
        self._generated_delivery_bindings.pop((tenant_id, run_id), None)
        if self._project_verification is None:
            raise RuntimeCapabilityError("project verification scope is unavailable")
        try:
            result = await self._project_verification.restore(
                tenant_id=tenant_id, run_id=run_id, actor=actor, action_key=action_key,
            )
            self._cache_project_verification(tenant_id, run_id, result)
            return result
        except ProjectVerificationError as error:
            raise RuntimeCapabilityError(str(error), execution_diagnostic=error.diagnostic,
                                         project_failure=error.project_failure,
                                         python_project_failure=error.python_project_failure,
                                         project_prerequisite=error.project_prerequisite) from None

    async def action_preparation_snapshot(self, context: TaskContext) -> Mapping[str, JsonValue]:
        from agent_hub.capabilities.action_preparation import capture_action_preparation

        if self._project_verification is None:
            raise RuntimeCapabilityError('project preparation scope is unavailable')
        return await capture_action_preparation(self._project_verification, context)

    async def _python_build_profile(
        self, tenant_id: UUID, run_id: UUID, *, catalog_query: PythonCatalogQuery | None = None,
    ) -> Mapping[str, JsonValue]:
        from agent_hub.capabilities.project_inspection import python_catalog_projection
        from agent_hub.skills.sandbox.project import ProjectInvocation
        from agent_hub.skills.sandbox.python_project_profile import PythonProjectProfileSandbox

        getter = getattr(self._run_repository, 'get', None)
        if not callable(getter):
            if catalog_query is not None:
                raise ProjectInspectionError('python catalog unavailable')
            return {'status': 'unavailable'}
        record = await getter(tenant_id, run_id)
        project, session = authorized_workspace_scope(record, tenant_id=tenant_id, run_id=run_id)
        invocation = ProjectInvocation(execution_id='python_profile_' + uuid4().hex, tenant_id=tenant_id,
            run_id=run_id, project_id=project, session_id=session, sandbox_profile='restricted',
            install_allowed=False, timeout_seconds=15)
        try:
            result = await PythonProjectProfileSandbox().inspect(invocation)
        except (ValueError, OSError, TimeoutError):
            if catalog_query is not None:
                raise ProjectInspectionError('python catalog unavailable') from None
            return {'status': 'unavailable', 'readiness': 'not_probed'}
        if not result.ok or result.profile is None:
            if catalog_query is not None:
                raise ProjectInspectionError('python catalog unavailable')
            return {'status': 'unavailable', 'readiness': 'not_probed', 'reason': result.error}
        return {
            **python_catalog_projection(result.profile, query=catalog_query),
            'recipe': 'python_offline_wheel_v1', 'build_backend': 'setuptools.build_meta',
            'requires_python': '>=3.12,<3.13', 'build_requirement_names': ('setuptools', 'wheel'),
            'source_directory': 'src', 'test_directory': 'tests', 'test_runner': 'unittest',
            'lock_format': 'name==version --hash=sha256:actual-catalog-digest',
            'catalog_usage': 'Declared candidates only; select needed direct and transitive dependencies. Not an installation receipt.',
        }

    def _execute_calculator(self, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        expression = arguments.get("expression")
        if not isinstance(expression, str):
            raise RuntimeCapabilityError("calculator requires expression")
        result = self._calculator.evaluate(expression)
        return {"value": str(result.value)}

    async def _execute_http_read(
        self,
        arguments: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        url = arguments.get("url")
        if not isinstance(url, str) or not url.strip():
            raise RuntimeCapabilityError("http.read requires url")
        try:
            result = await self._http_reader.fetch(url.strip())
        except HttpReadError as error:
            raise RuntimeCapabilityError(str(error)) from None
        return {
            "url": result.url,
            "status_code": result.status_code,
            "body": result.body,
            "truncated": result.truncated,
        }

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
                timestamp_filename=True,
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
                timestamp_filename=True,
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
        from agent_hub.capabilities.project_stack_admission import require_supported_project_paths

        try:
            require_supported_project_paths(tuple(files))
        except ValueError as error:
            raise RuntimeCapabilityError(str(error)) from None
        if self._project_executor_configured and {"package.json", "pyproject.toml"}.intersection(files):
            raise RuntimeCapabilityError(
                "Node/Python project delivery requires workspace.write_text, project.verify and workspace.bundle"
            )
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
                timestamp_filename=True,
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

    async def _execute_workspace_write_text(
        self,
        tenant_id: UUID,
        run_id: UUID,
        arguments: Mapping[str, JsonValue],
        *,
        _dispatch: _WriteDispatch | None = None,
    ) -> Mapping[str, JsonValue]:
        store = self._require_project_workspace_store()
        if _dispatch is not None:
            from agent_hub.capabilities.review_context import _WriteDispatch

            if type(_dispatch) is not _WriteDispatch or not _dispatch.claimed:
                raise RuntimeCapabilityError("concrete write authority unavailable")
            try:
                # Match and execute this owned copy across all backend awaits.
                arguments = _freeze_object(arguments, name="owned concrete write arguments")
                _dispatch.begin_write(self, tenant_id, run_id, arguments)
            except ValueError:
                raise RuntimeCapabilityError("concrete write authority unavailable") from None
        project_id, session_id = await self._project_workspace_scope(
            tenant_id,
            run_id,
            write_required=_dispatch is None,
        )
        path = _required_string(arguments, "path")
        referenced = "content_reference" in arguments
        if referenced:
            # References transport reviewed file data only; they grant no write authority.
            reference = arguments["content_reference"]
            if set(arguments) != {"path", "content_reference"}:
                raise RuntimeCapabilityError(
                    "workspace content sources must be exclusive with no extra arguments"
                )
            if self._artifact_repository is None or not isinstance(reference, Mapping):
                raise RuntimeCapabilityError("workspace content reference is unavailable")
            try:
                loaded = await load_continuation(
                    self._artifact_repository,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    reference=reference,
                )
            except ArtifactRepositoryError:
                raise RuntimeCapabilityError(
                    "workspace content reference is invalid or unavailable"
                ) from None
            if type(loaded) is not str or not loaded.strip():
                raise RuntimeCapabilityError(
                    "workspace referenced content must be a nonempty string"
                )
            content = loaded
            # Hydration may await remote storage; do not write using a stale workspace grant.
            if await self._project_workspace_scope(tenant_id, run_id) != (project_id, session_id):
                raise RuntimeCapabilityError("workspace scope changed during content resolution")
        else:
            content = _required_string(arguments, "content")
        contract = await self._original_project_contract(tenant_id, run_id)
        if (
            contract is not None
            and path != "DELIVERY_MANIFEST.json"
            and not contract.is_editable(path)
        ):
            raise RuntimeCapabilityError("original project checks are protected from modification")
        if _dispatch is not None:
            try:
                await _dispatch.require_current(self)
            except ValueError:
                raise RuntimeCapabilityError("concrete write authority unavailable") from None
        task = asyncio.current_task()
        if referenced and task is not None and task.cancelling():
            raise asyncio.CancelledError
        try:
            metadata = store.write_bytes(
                tenant_id=tenant_id,
                project_id=project_id,
                session_id=session_id,
                relative_path=path,
                data=content.encode("utf-8"),
                mime_type=_workspace_mime_type(path),
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise RuntimeCapabilityError(str(error)) from None
        public = cast(Mapping[str, JsonValue], metadata.to_public_dict())
        return {
            "summary": f"Wrote workspace file {metadata.path}.",
            "file": public,
            "workspace_files": (public,),
        }

    async def _execute_workspace_list(
        self,
        tenant_id: UUID,
        run_id: UUID,
    ) -> Mapping[str, JsonValue]:
        store = self._require_project_workspace_store()
        project_id, session_id = await self._project_workspace_scope(
            tenant_id, run_id, write_required=False
        )
        try:
            files = store.list_files(tenant_id, project_id, session_id)
        except (OSError, ValueError) as error:
            raise RuntimeCapabilityError(str(error)) from None
        public = tuple(cast(Mapping[str, JsonValue], item.to_public_dict()) for item in files)
        return {
            "summary": f"Workspace contains {len(public)} files.",
            "workspace_files": public,
            "truncated": False,
            "bundle_download_url": store.bundle_download_url(project_id, session_id),
        }

    async def _execute_workspace_prune(
        self,
        tenant_id: UUID,
        run_id: UUID,
        arguments: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        store = self._require_project_workspace_store()
        project_id, session_id = await self._project_workspace_scope(tenant_id, run_id)
        raw_paths = arguments.get("keep_paths")
        if not isinstance(raw_paths, tuple | list) or not raw_paths:
            raise RuntimeCapabilityError("workspace prune requires keep_paths")
        if any(not isinstance(path, str) for path in raw_paths):
            raise RuntimeCapabilityError("workspace prune keep_paths must be strings")
        contract = await self._original_project_contract(tenant_id, run_id)
        if contract is not None and not set(contract.protected_files).issubset(raw_paths):
            raise RuntimeCapabilityError('original project checks are protected from deletion')
        try:
            removed = store.prune_files(
                tenant_id,
                project_id,
                session_id,
                keep_paths=tuple(cast(str, path) for path in raw_paths),
            )
        except (OSError, ValueError) as error:
            raise RuntimeCapabilityError(str(error)) from None
        return {
            "summary": f"Pruned {len(removed)} obsolete workspace files.",
            "removed_paths": removed,
        }

    async def _execute_workspace_bundle(
        self,
        tenant_id: UUID,
        run_id: UUID,
        arguments: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        store = self._require_project_workspace_store()
        generated_store = self._require_generated_file_store()
        project_id, session_id = await self._project_workspace_scope(tenant_id, run_id)
        from agent_hub.capabilities.project_stack_admission import require_supported_project_paths

        files = store.list_files(tenant_id, project_id, session_id)
        try:
            require_supported_project_paths(tuple(item.path for item in files))
        except ValueError as error:
            raise RuntimeCapabilityError(str(error)) from None
        original_contract = await self._original_project_contract(tenant_id, run_id)
        if original_contract is not None and (
            not self._project_executor_configured or self._project_verification is None
        ):
            raise RuntimeCapabilityError('project execution backend is unavailable')
        title = _optional_string(arguments, "title") or "Project Workspace"
        verified_source: str | None = None
        delivery_receipt = self._project_delivery_receipts.get((tenant_id, run_id))
        generated_binding = self._generated_delivery_bindings.get((tenant_id, run_id))
        python_product: Mapping[str, JsonValue] | None = None
        if (self._project_executor_configured and self._project_verification is not None
            and (original_contract is not None or delivery_receipt is not None or any(
                item.path in {"package.json", "pyproject.toml"} for item in files
            ))):
            try:
                verified_source = await self._project_verification.require_current_receipt(tenant_id, run_id)
                python_product = await self._project_verification.require_current_python_product(tenant_id, run_id)
            except ProjectVerificationError as error:
                if generated_binding is not None:
                    self._project_delivery_receipts.pop((tenant_id, run_id), None)
                    self._generated_delivery_bindings.pop((tenant_id, run_id), None)
                raise RuntimeCapabilityError(str(error)) from None
        filename = _filename(arguments, title=title, extension=".zip")
        try:
            artifact_id = uuid4()
            packaging = asyncio.create_task(asyncio.to_thread(
                self._bundle_workspace,
                store=store,
                generated_store=generated_store,
                tenant_id=tenant_id,
                run_id=run_id,
                project_id=project_id,
                session_id=session_id,
                artifact_id=artifact_id,
                filename=filename,
                verified_source_sha256=verified_source,
                python_product=python_product,
            ))
            try:
                metadata, files = await asyncio.shield(packaging)
            except asyncio.CancelledError:
                # A cancelled waiter cannot stop a disk writer. Drain it before
                # removing this unpublished artifact; never leak a late product.
                while not packaging.done():
                    try:
                        await asyncio.shield(packaging)
                    except asyncio.CancelledError:
                        continue
                    except Exception:  # noqa: BLE001 - preserve cancellation after draining the writer.
                        break
                if not packaging.cancelled() and packaging.exception() is None:
                    abandoned, _ = packaging.result()
                    generated_store.resolve_for(tenant_id, run_id, artifact_id,
                                                abandoned.storage_key).unlink(missing_ok=True)
                raise
        except (OSError, ValueError, ProjectVerificationError) as error:
            raise RuntimeCapabilityError(str(error)) from None
        if generated_binding is not None or python_product is not None:
            try:
                assert self._project_verification is not None
                current_source = await self._project_verification.require_current_receipt(tenant_id, run_id)
                current_product = await self._project_verification.require_current_python_product(tenant_id, run_id)
                if (current_source != verified_source
                        or current_product != python_product
                        or self._project_delivery_receipts.get((tenant_id, run_id)) != delivery_receipt
                        or self._generated_delivery_bindings.get((tenant_id, run_id)) != generated_binding):
                    raise ProjectVerificationError('project verification required before delivery')
            except (asyncio.CancelledError, ProcessCleanupIncomplete):
                generated_store.resolve_for(tenant_id, run_id, artifact_id,
                                            metadata.storage_key).unlink(missing_ok=True)
                self._project_delivery_receipts.pop((tenant_id, run_id), None)
                self._generated_delivery_bindings.pop((tenant_id, run_id), None)
                raise
            except Exception:  # noqa: BLE001 - never return stale generated artifact evidence.
                generated_store.resolve_for(tenant_id, run_id, artifact_id,
                                            metadata.storage_key).unlink(missing_ok=True)
                self._project_delivery_receipts.pop((tenant_id, run_id), None)
                self._generated_delivery_bindings.pop((tenant_id, run_id), None)
                raise RuntimeCapabilityError('project verification required before delivery') from None
        result = dict(
            _file_result(
                artifact_id=artifact_id,
                public_metadata=metadata.to_public_dict(),
                internal_metadata=metadata.to_content_file(),
                presentation=_generated_file_presentation(
                    arguments, default="final_attachment"
                ),
                summary=f"Generated workspace ZIP artifact {metadata.filename}.",
            )
        )
        result["workspace_files"] = tuple(
            cast(Mapping[str, JsonValue], item.to_public_dict()) for item in files
        )
        result["bundle_download_url"] = store.bundle_download_url(project_id, session_id)
        if python_product is not None:
            descriptor = python_product["descriptor"]
            if not isinstance(descriptor, Mapping):
                raise RuntimeCapabilityError("Python product reference is invalid")
            result["python_product"] = {key: descriptor[key] for key in
                ("filename", "sha256", "size", "source_sha256", "execution_id")}
            result["python_product_path"] = "dist/" + cast(str, descriptor["filename"])
        if delivery_receipt is not None and delivery_receipt[0] == verified_source:
            # The receipt was issued by verify(), and _bundle_workspace checked the actual ZIP bytes.
            result["verification_source_sha256"] = verified_source
            result["verification_receipt_sha256"] = delivery_receipt[1]
            if generated_binding is not None:
                result['generated_project_acceptance'] = generated_binding
            if original_contract is not None and self._project_verification is not None:
                binding = self._project_verification.original_receipt_binding(tenant_id, run_id)
                if binding is not None:
                    result['original_check_contract'] = binding
        return result

    @staticmethod
    def _bundle_workspace(
        *,
        store: ProjectWorkspaceStore,
        generated_store: GeneratedFileStore,
        tenant_id: UUID,
        run_id: UUID,
        project_id: str,
        session_id: str,
        artifact_id: UUID,
        filename: str,
        verified_source_sha256: str | None = None,
        python_product: Mapping[str, JsonValue] | None = None,
    ) -> tuple[GeneratedFileMetadata, tuple[ProjectWorkspaceFile, ...]]:
        bundle = store.create_session_zip(tenant_id, project_id, session_id)
        try:
            if verified_source_sha256 is not None:
                hashes: dict[str, str] = {}
                with zipfile.ZipFile(bundle.path) as archive:
                    for entry in archive.infolist():
                        if entry.filename in hashes:
                            raise ValueError("bundle does not match verified source")
                        digest = hashlib.sha256(b"x\0" if (entry.external_attr >> 16) & 0o111 else b"f\0")
                        with archive.open(entry) as stream:
                            while chunk := stream.read(65536):
                                digest.update(chunk)
                        hashes[entry.filename] = digest.hexdigest()
                bundled = hashlib.sha256(json.dumps(
                    hashes, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                ).encode("ascii")).hexdigest()
                if bundled != verified_source_sha256:
                    raise ValueError("bundle does not match verified source")
            if python_product is not None:
                if verified_source_sha256 is None:
                    raise ValueError("Python product requires current verification")
                from agent_hub.capabilities.python_product_bundle import append_python_product
                from agent_hub.capabilities.python_product_delivery import open_product

                with open_product(generated_store, python_product, tenant_id, run_id,
                                  verified_source_sha256) as (fd, descriptor):
                    append_python_product(bundle.path, fd, descriptor,
                        max_file_bytes=store._max_file_bytes,
                        max_bundle_bytes=store._max_bundle_bytes,
                        max_bundle_files=store._max_bundle_files)
            metadata = generated_store.store_path(
                tenant_id=tenant_id,
                run_id=run_id,
                artifact_id=artifact_id,
                filename=filename,
                mime_type=ZIP_MIME_TYPE,
                source=bundle.path,
                move=True,
                timestamp_filename=True,
            )
        finally:
            bundle.path.unlink(missing_ok=True)
            try:
                bundle.path.parent.rmdir()
            except OSError:
                pass
        return metadata, bundle.files

    async def _original_project_contract(
        self, tenant_id: UUID, run_id: UUID,
    ) -> ProjectCheckContract | None:
        from agent_hub.capabilities.project_baselines import (
            ProjectBaselineReference,
            ProjectBaselineStore,
        )

        getter = getattr(self._run_repository, 'get', None)
        if not callable(getter):
            return None
        try:
            record = getter(tenant_id, run_id)
            if isawaitable(record):
                record = await record
            route = getattr(record, 'routing_decision', None)
            if not isinstance(route, Mapping):
                return None
            if 'generated_project_contract_version' in route or 'generated_project_preparation' in route:
                if self._project_verification is None:
                    raise ValueError
                generated = await self._project_verification.generated.load(tenant_id, run_id, 'main_agent')
                return None if generated is None else generated.stored.contract
            if ('project_input_contract_version' not in route and route.get('project_input') is None):
                return None
            version = route.get('project_input_contract_version')
            if type(version) is not int or version != 1 or self._project_workspace_dir is None:
                raise ValueError
            selection = ManagedProjectInput.from_payload(route.get('project_input'))
            project, session = authorized_workspace_scope(record, tenant_id=tenant_id, run_id=run_id)
            state = route.get('project_input_preparation')
            if (isinstance(state, Mapping) and type(state.get('schema_version')) is int
                    and state['schema_version'] == 2):
                if self._project_verification is None:
                    raise ValueError
                python_context = await self._project_verification.python_original_context(tenant_id, run_id)
                if python_context is None:
                    raise ValueError
                contract = python_context.stored.contract
                if ((contract.tenant_id, contract.run_id, contract.project_id, contract.session_id)
                        != (tenant_id, run_id, project, session)
                        or contract.editable_paths != selection.editable_paths):
                    raise ValueError
                return contract
            if (not isinstance(state, Mapping) or type(state.get('schema_version')) is not int
                    or state['schema_version'] != 1 or state.get('phase') != 'observed'):
                raise ValueError
            reference = ProjectBaselineReference.from_payload(state.get('reference'))
            key = (tenant_id, run_id, reference.contract_sha256)
            cached = self._project_contract_cache.get(key)
            if cached is not None:
                contract = cached[0]
                self._project_contract_cache.move_to_end(key)
            else:
                from agent_hub.skills.sandbox.project import ProjectInvocation

                invocation = ProjectInvocation(
                    execution_id='original_contract_guard', tenant_id=tenant_id, run_id=run_id,
                    project_id=project, session_id=session, sandbox_profile='workspace_write',
                    timeout_seconds=1,
                )
                stored = await asyncio.to_thread(
                    ProjectBaselineStore(self._project_workspace_dir).load, invocation, reference,
                )
                contract = stored.contract
                # Eviction only costs a read-only revalidation; it cannot lose authority or evidence.
                cost = len(json.dumps(contract.to_payload(), sort_keys=True).encode('utf-8'))
                limit = 32 * 1024**2
                while self._project_contract_cache and self._project_contract_cache_bytes + cost > limit:
                    _, (_, removed) = self._project_contract_cache.popitem(last=False)
                    self._project_contract_cache_bytes -= removed
                if cost <= limit:
                    self._project_contract_cache[key] = (contract, cost)
                    self._project_contract_cache_bytes += cost
            if ((contract.tenant_id, contract.run_id, contract.project_id, contract.session_id)
                    != (tenant_id, run_id, project, session)
                    or contract.input_source_sha256 != reference.input_source_sha256
                    or contract.editable_paths != selection.editable_paths):
                raise ValueError
            return contract
        except Exception:  # noqa: BLE001 - a model cannot erase incomplete or changed original-check state.
            raise RuntimeCapabilityError('original project contract is unavailable or changed') from None

    async def _project_workspace_scope(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        write_required: bool = True,
    ) -> tuple[str, str]:
        getter = getattr(self._run_repository, "get", None)
        if not callable(getter):
            raise RuntimeCapabilityError("workspace scope is not configured")
        try:
            record = getter(tenant_id, run_id)
            if isawaitable(record):
                record = await cast(Awaitable[object], record)
        except (KeyError, LookupError, RuntimeError, TypeError, ValueError) as error:
            raise RuntimeCapabilityError("workspace scope could not be resolved") from error
        routing = getattr(record, "routing_decision", None)
        if not isinstance(routing, Mapping):
            raise RuntimeCapabilityError("workspace scope could not be resolved")
        permissions = routing.get("requested_permissions")
        sandbox_profile = routing.get("sandbox_profile")
        if not isinstance(permissions, tuple | list):
            raise RuntimeCapabilityError("workspace access is not authorized")
        if write_required:
            if sandbox_profile != "workspace_write" or "workspace.write" not in permissions:
                raise RuntimeCapabilityError("workspace write is not authorized")
        elif (
            sandbox_profile not in {"read_only", "restricted", "workspace_write"}
            or "workspace.read" not in permissions
        ):
            raise RuntimeCapabilityError("workspace read is not authorized")
        project_id = routing.get("project_id")
        session_id = routing.get("workspace_session_id")
        if not isinstance(project_id, str) or not isinstance(session_id, str):
            raise RuntimeCapabilityError("workspace scope could not be resolved")
        return project_id, session_id

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

    def _require_project_workspace_store(self) -> ProjectWorkspaceStore:
        if self._project_workspace_store is None:
            raise RuntimeCapabilityError("project workspace store is not configured")
        return self._project_workspace_store

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
        writable_tmp_path.mkdir(mode=0o770, parents=True, exist_ok=True)
        writable_tmp_path.chmod(0o770)
        try:
            execution_backend, sandbox_profile, sandbox = await self._skill_sandbox_for_run(
                tenant_id=tenant_id,
                run_id=run_id,
            )
            try:
                result = await sandbox.run(
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
                        sandbox_profile=sandbox_profile,
                        writable_tmp_path=writable_tmp_path,
                    )
                )
            except OSError:
                raise RuntimeCapabilityError(
                    f"execution backend is unavailable: {execution_backend}"
                ) from None
        finally:
            shutil.rmtree(writable_tmp_path, ignore_errors=True)
        diagnostic = (
            ExecutionDiagnostic(backend=execution_backend, result=result)
            if type(result) is SkillResult
            and (result.timed_out or (result.exit_code is not None and result.exit_code != 0))
            else None
        )
        if result.timed_out:
            raise RuntimeCapabilityError(
                "skill execution timed out", execution_diagnostic=diagnostic,
            )
        if result.exit_code != 0:
            if _backend_process_unavailable(execution_backend, result.stderr):
                raise RuntimeCapabilityError(
                    f"execution backend is unavailable: {execution_backend}",
                    execution_diagnostic=diagnostic,
                )
            raise RuntimeCapabilityError("skill execution failed", execution_diagnostic=diagnostic)
        parsed = _parse_stdout(result.stdout)
        return {
            "execution_backend": execution_backend,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "result": parsed,
        }

    async def _skill_sandbox_for_run(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
    ) -> tuple[str, SandboxProfile, SkillSandbox]:
        execution_backend = "systemd"
        sandbox_profile: SandboxProfile = "workspace_write"
        if self._run_repository is not None:
            getter = getattr(self._run_repository, "get", None)
            if not callable(getter):
                raise RuntimeCapabilityError("run repository cannot resolve execution backend")
            try:
                record = await getter(tenant_id, run_id)
            except (KeyError, LookupError, RuntimeError, TypeError, ValueError) as error:
                raise RuntimeCapabilityError("run execution backend could not be resolved") from error
            routing_decision = getattr(record, "routing_decision", None)
            if not isinstance(routing_decision, Mapping):
                raise RuntimeCapabilityError("run execution backend could not be resolved")
            configured_backend = routing_decision.get("execution_backend", "systemd")
            if not isinstance(configured_backend, str):
                raise RuntimeCapabilityError("execution backend is not supported")
            execution_backend = configured_backend.strip().casefold()
            configured_profile = routing_decision.get("sandbox_profile", "workspace_write")
            if not isinstance(configured_profile, str):
                raise RuntimeCapabilityError("sandbox profile is not supported")
            normalized_profile = configured_profile.strip().casefold()
            if normalized_profile not in SANDBOX_PROFILES:
                raise RuntimeCapabilityError("sandbox profile is not supported")
            sandbox_profile = cast(SandboxProfile, normalized_profile)
        sandbox = self._skill_sandboxes.get(execution_backend)
        if sandbox is None:
            raise RuntimeCapabilityError("execution backend is not supported")
        return execution_backend, sandbox_profile, sandbox

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
        item: dict[str, JsonValue] = {
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
        input_schema = _builtin_input_schema(
            name, content_references=self._artifact_repository is not None,
        )
        if input_schema is not None:
            item["input_schema"] = input_schema
        description = builtin_tool_description(name)
        if description is not None:
            item["description"] = description
            item["input_contract_version"] = 1
            item["input_contract_added_fields"] = ("input_schema", "description")
        return item

    def _builtin_availability_reason(self, name: str) -> str | None:
        if name == PROJECT_VERIFY_TOOL:
            if self._project_workspace_dir is None:
                return "project_workspace_store_not_configured"
            if not callable(getattr(self._run_repository, "get", None)):
                return "workspace_scope_not_configured"
            if not self._project_executor_configured:
                return "project_execution_backend_unavailable"
        if name == PROJECT_INSPECT_TOOL:
            if self._project_workspace_dir is None:
                return "project_workspace_store_not_configured"
            if not callable(getattr(self._run_repository, "get", None)):
                return "workspace_scope_not_configured"
        if name == "workspace.read":
            if self._workspace_root is None and self._project_workspace_dir is None:
                return "workspace_root_not_configured"
            if not callable(getattr(self._run_repository, "get", None)):
                return "workspace_scope_not_configured"
        if name in {
            _PROJECT_PREFLIGHT_TOOL,
            _WORKSPACE_WRITE_TOOL,
            _WORKSPACE_LIST_TOOL,
            _WORKSPACE_BUNDLE_TOOL,
        } and self._project_workspace_store is None:
            return "project_workspace_store_not_configured"
        if name in {_DOCX_TOOL, _PPTX_TOOL, _PROJECT_ZIP_TOOL, _WORKSPACE_BUNDLE_TOOL} and (
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
            availability_reason = self._skill_package_availability_reason(package_path)
            items.append(
                {
                    "id": skill_id,
                    "kind": "skill",
                    "adapter": "skill_sandbox",
                    "permission_class": "skill.use",
                    "sandbox_profile": "systemd_skill_sandbox",
                    "available": availability_reason is None,
                    "availability_reason": availability_reason,
                    "replay_safe": False,
                    "aliases": (),
                }
            )
        return tuple(items)

    @staticmethod
    def _skill_package_availability_reason(package_path: Path) -> str | None:
        try:
            SkillPackageInspector().inspect(package_path.read_bytes())
        except (InvalidSkillPackage, OSError, ValueError):
            return "skill_package_not_executable"
        return None

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
    if name == "calculator.evaluate":
        return ("calculator", "calculator_evaluate")
    alias = _BUILTIN_ALIASES.get(name)
    return () if alias is None else (alias,)


def _tuple_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple | list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _builtin_permission_class(name: str) -> str:
    if name == PROJECT_VERIFY_TOOL:
        return "command.run"
    if name == "calculator.evaluate":
        return "calculator.evaluate"
    if name == "read_context":
        return "context.read"
    if name == "http.read":
        return "network.read"
    if name == "workspace.read":
        return "file.read"
    if name in {_WORKSPACE_LIST_TOOL, PROJECT_INSPECT_TOOL}:
        return "file.read"
    if name == _WORKSPACE_WRITE_TOOL:
        return "file.write"
    return "file.create"


def _builtin_sandbox_profile(name: str) -> str:
    if name == PROJECT_VERIFY_TOOL:
        return "project_execution"
    if name in {"calculator.evaluate", "read_context"}:
        return "in_process"
    if name == "http.read":
        return "http_read"
    if name in {"workspace.read", PROJECT_INSPECT_TOOL}:
        return "workspace_read"
    if name in {
        _PROJECT_PREFLIGHT_TOOL,
        _WORKSPACE_WRITE_TOOL,
        _WORKSPACE_LIST_TOOL,
        _WORKSPACE_BUNDLE_TOOL,
    }:
        return "project_workspace_store"
    return "generated_artifact_store"


def _builtin_input_schema(
    name: str, *, content_references: bool = False,
) -> Mapping[str, JsonValue] | None:
    builtin_schema = builtin_tool_input_schema(name)
    if builtin_schema is not None:
        return builtin_schema
    if name == _PROJECT_PREFLIGHT_TOOL:
        return project_preflight_model_input_schema()
    if name == _WORKSPACE_WRITE_TOOL:
        schema: dict[str, JsonValue] = {
            "type": "object",
            "additionalProperties": False,
            "required": ("path", "content"),
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 512},
                "content": {"type": "string", "minLength": 1},
            },
        }
        if content_references:
            properties = cast(dict[str, JsonValue], schema["properties"])
            properties["content_reference"] = {
                "type": "object", "additionalProperties": False,
                "required": ("version", "content_sha256", "total_bytes", "segments"),
                "properties": {
                    "version": {"type": "integer", "const": 1},
                    "content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "total_bytes": {"type": "integer", "minimum": 1, "maximum": 64 * 1024 * 1024},
                    "segments": {
                        "type": "array", "minItems": 1, "maxItems": 1024,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ("id", "sha256"),
                            "properties": {
                                "id": {"type": "string", "pattern":
                                       "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"},
                                "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            },
                        },
                    },
                },
            }
            schema["required"] = ("path",)
            schema["oneOf"] = ({"required": ("content",)}, {"required": ("content_reference",)})
        return schema
    if name == _WORKSPACE_LIST_TOOL:
        return {"type": "object", "additionalProperties": False, "properties": {}}
    if name == _WORKSPACE_BUNDLE_TOOL:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "filename": {"type": "string", "minLength": 1, "maxLength": 200},
                "presentation": {
                    "type": "string",
                    "enum": ("step_detail", "final_attachment"),
                },
            },
        }
    return None


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
        raise RuntimeCapabilityError(
            f"files must contain 1 to {_MAX_PROJECT_FILES} entries"
        )
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


def _backend_process_unavailable(execution_backend: str, stderr: str) -> bool:
    normalized = stderr.casefold()
    if execution_backend == "docker":
        return any(
            marker in normalized
            for marker in (
                "cannot connect to the docker daemon",
                "error during connect",
                "no such image",
                "unable to find image",
                "network agent-hub-skill-net not found",
            )
        )
    if execution_backend == "systemd":
        return any(
            marker in normalized
            for marker in (
                "failed to connect to bus",
                "access denied",
                "authentication is required",
                "failed to start transient service unit",
            )
        )
    return False


def _json_dict(value: Mapping[str, JsonValue]) -> dict[str, object]:
    active_containers: set[int] = set()

    def visit(item: object) -> object:
        if item is None or type(item) in {bool, int, str}:
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("nonfinite JSON number")
            return item
        if not isinstance(item, Mapping | list | tuple):
            raise TypeError("value is not JSON-compatible")
        identity = id(item)
        if identity in active_containers:
            raise ValueError("cyclic JSON value")
        active_containers.add(identity)
        try:
            if isinstance(item, Mapping):
                result: dict[str, object] = {}
                for key, child in item.items():
                    if type(key) is not str:
                        raise TypeError("JSON object keys must be strings")
                    result[key] = visit(child)
                return result
            return [visit(child) for child in item]
        finally:
            active_containers.remove(identity)

    try:
        parsed = visit(value)
    except (TypeError, ValueError, RecursionError):
        raise RuntimeCapabilityError("capability arguments are invalid") from None
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
