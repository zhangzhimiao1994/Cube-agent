from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import cast
from uuid import UUID, uuid4

from agent_hub.capabilities.tools.calculator import Calculator
from agent_hub.capabilities.tools.workspace_read import WorkspaceReader
from agent_hub.documents.docx import DocxBlueprint, build_docx
from agent_hub.documents.pptx import PptxBlueprint, build_pptx
from agent_hub.files.generated import (
    DOCX_MIME_TYPE,
    PPTX_MIME_TYPE,
    ZIP_MIME_TYPE,
    GeneratedFileStore,
    safe_generated_filename,
)
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.base import SkillInvocation, SkillSandbox
from agent_hub.skills.sandbox.systemd import SystemdSkillSandbox

_SAFE_CAPABILITY_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_DOCX_TOOL = "document.generate_docx"
_PPTX_TOOL = "presentation.generate_pptx"
_PROJECT_ZIP_TOOL = "project.generate_zip"
_MAX_PROJECT_FILES = 64
_MAX_PROJECT_FILE_BYTES = 256_000
_MAX_PROJECT_ZIP_SOURCE_BYTES = 2_000_000
_DOTTED_BUILT_INS = frozenset({_DOCX_TOOL, _PPTX_TOOL, _PROJECT_ZIP_TOOL})
_REPLAY_SAFE = frozenset({
    "calculator",
    "calculator_evaluate",
    "read_context",
    "workspace_read",
    _DOCX_TOOL,
    _PPTX_TOOL,
    _PROJECT_ZIP_TOOL,
})


class RuntimeCapabilityError(RuntimeError):
    """Stable runtime capability failure."""


class RuntimeCapabilityGateway:
    """Production capability executor for non-dangerous built-ins and approved skills."""

    def __init__(
        self,
        *,
        skill_store_dir: Path,
        workspace_root: Path | None = None,
        generated_artifact_dir: Path | None = None,
        skill_sandbox: SkillSandbox | None = None,
        calculator: Calculator | None = None,
    ) -> None:
        self._skill_store_dir = skill_store_dir
        self._workspace_root = workspace_root
        self._generated_file_store = (
            GeneratedFileStore(generated_artifact_dir) if generated_artifact_dir is not None else None
        )
        self._skill_sandbox = skill_sandbox or SystemdSkillSandbox()
        self._calculator = calculator or Calculator()

    def is_replay_safe(self, name: str) -> bool:
        return name in _REPLAY_SAFE

    def is_available(self, tenant_id: UUID, name: str) -> bool:
        if name in _REPLAY_SAFE:
            return True
        if _SAFE_CAPABILITY_NAME.fullmatch(name) is None:
            return False
        return self._skill_package_path(tenant_id, name).is_file()

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
        _require_safe("capability name", name)
        _require_safe("idempotency key", idempotency_key, max_length=160)
        if name in {"calculator", "calculator_evaluate"}:
            return self._execute_calculator(arguments)
        if name == "read_context":
            return self._execute_read_context(arguments)
        if name == "workspace_read":
            return self._execute_workspace_read(arguments)
        if name == _DOCX_TOOL:
            return self._execute_generate_docx(tenant_id, run_id, arguments)
        if name == _PPTX_TOOL:
            return self._execute_generate_pptx(tenant_id, run_id, arguments)
        if name == _PROJECT_ZIP_TOOL:
            return self._execute_generate_project_zip(tenant_id, run_id, arguments)
        return await self._execute_skill(
            tenant_id=tenant_id,
            run_id=run_id,
            actor=actor,
            skill_id=name,
            arguments=arguments,
            idempotency_key=idempotency_key,
        )

    def _execute_calculator(self, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        expression = arguments.get("expression")
        if not isinstance(expression, str):
            raise RuntimeCapabilityError("calculator requires expression")
        result = self._calculator.evaluate(expression)
        return {"value": str(result.value)}

    def _execute_read_context(self, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        path = arguments.get("path")
        if isinstance(path, str):
            return self._execute_workspace_read(arguments)
        query = arguments.get("query")
        if query is None:
            query = arguments.get("text")
        if query is not None and (not isinstance(query, str) or not query.strip()):
            raise RuntimeCapabilityError("read_context query must be a nonblank string")
        return {
            "query": query.strip() if isinstance(query, str) else None,
            "matches": (),
            "summary": "No additional runtime context is available for this query.",
            "truncated": False,
        }

    def _execute_workspace_read(self, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        if self._workspace_root is None:
            raise RuntimeCapabilityError("workspace reader is not configured")
        path = arguments.get("path")
        if not isinstance(path, str):
            raise RuntimeCapabilityError("workspace reader requires path")
        result = WorkspaceReader(self._workspace_root).read(path)
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
        sections = _optional_mapping_list(arguments, "sections")
        blueprint = DocxBlueprint(
            title=title,
            subtitle=_optional_string(arguments, "subtitle"),
            sections=sections,
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
        result = dict(
            _file_result(
                artifact_id=artifact_id,
                metadata=metadata.to_public_dict(),
                summary=f"Generated DOCX artifact {metadata.filename}.",
            )
        )
        result["presentation"] = _generated_file_presentation(
            arguments,
            default="final_attachment",
        )
        return result

    def _execute_generate_pptx(
        self,
        tenant_id: UUID,
        run_id: UUID,
        arguments: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        store = self._require_generated_file_store()
        title = _required_string(arguments, "title")
        slides = _optional_mapping_list(arguments, "slides")
        blueprint = PptxBlueprint(
            title=title,
            subtitle=_optional_string(arguments, "subtitle"),
            template_id=_optional_string(arguments, "template_id") or "consulting-clean",
            slides=slides,
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
        result = dict(
            _file_result(
                artifact_id=artifact_id,
                metadata=metadata.to_public_dict(),
                summary=f"Generated PPTX artifact {metadata.filename}.",
            )
        )
        result["presentation"] = _generated_file_presentation(
            arguments,
            default="final_attachment",
        )
        return result

    def _execute_generate_project_zip(
        self,
        tenant_id: UUID,
        run_id: UUID,
        arguments: Mapping[str, JsonValue],
    ) -> Mapping[str, JsonValue]:
        store = self._require_generated_file_store()
        title = _required_string(arguments, "title")
        files = _project_files(arguments)
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
                metadata=metadata.to_public_dict(),
                summary=f"Generated project ZIP artifact {metadata.filename}.",
            )
        )
        result["presentation"] = _generated_file_presentation(
            arguments,
            default="final_attachment",
        )
        return result

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


def _require_safe(name: str, value: str, *, max_length: int = 128) -> None:
    if name == "capability name" and value in _DOTTED_BUILT_INS:
        return
    if (
        not isinstance(value, str)
        or len(value) > max_length
        or _SAFE_CAPABILITY_NAME.fullmatch(value) is None
    ):
        raise RuntimeCapabilityError(f"{name} is invalid")


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
    if not isinstance(raw_files, Mapping):
        raise RuntimeCapabilityError("files must be an object")
    if not raw_files or len(raw_files) > _MAX_PROJECT_FILES:
        raise RuntimeCapabilityError("files must contain 1 to 64 entries")
    files: dict[str, bytes] = {}
    total_bytes = 0
    for raw_path, raw_content in raw_files.items():
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
        files[path] = data
    return files


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


def _file_result(
    *,
    artifact_id: UUID,
    metadata: dict[str, str | int],
    summary: str,
) -> Mapping[str, JsonValue]:
    public_metadata: dict[str, JsonValue] = {
        "artifact_id": str(artifact_id),
        **metadata,
    }
    return {
        "artifact_id": str(artifact_id),
        "file": public_metadata,
        "metadata": public_metadata,
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
