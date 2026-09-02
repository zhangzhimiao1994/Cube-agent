from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from agent_hub.runtime.contracts import Artifact, JsonValue

_PROJECT_ZIP_TOOL = "project.generate_zip"
_ZIP_MIME_TYPE = "application/zip"


def reusable_generated_file_result(
    tool_name: str,
    artifacts: Sequence[Artifact],
) -> Mapping[str, JsonValue] | None:
    if tool_name != _PROJECT_ZIP_TOOL:
        return None
    return final_attachment_result(artifacts, mime_type=_ZIP_MIME_TYPE, extension=".zip")


def final_attachment_result(
    artifacts: Sequence[Artifact],
    *,
    mime_type: str | None = None,
    extension: str | None = None,
) -> Mapping[str, JsonValue] | None:
    for artifact in reversed(artifacts):
        if artifact.type != "tool_result":
            continue
        result = artifact.content.get("result")
        if _is_final_attachment_result(result, mime_type=mime_type, extension=extension):
            return cast(Mapping[str, JsonValue], result)
    return None


def _is_final_attachment_result(
    value: object,
    *,
    mime_type: str | None,
    extension: str | None,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    file_payload = value.get("file")
    if not isinstance(file_payload, Mapping):
        return False
    filename = file_payload.get("filename")
    result_mime_type = file_payload.get("mime_type")
    download_url = file_payload.get("download_url")
    artifact_id = value.get("artifact_id")
    if (
        value.get("presentation") != "final_attachment"
        or type(artifact_id) is not str
        or type(filename) is not str
        or type(download_url) is not str
        or not download_url.strip()
    ):
        return False
    if extension is not None and not filename.endswith(extension):
        return False
    return mime_type is None or result_mime_type == mime_type


def final_attachment_filename(result: Mapping[str, JsonValue]) -> str | None:
    file_payload = result.get("file")
    if not isinstance(file_payload, Mapping):
        return None
    filename = file_payload.get("filename")
    if type(filename) is not str:
        return None
    stripped = filename.strip()
    return stripped or None


def final_attachment_text_conflicts(text: str) -> bool:
    normalized = text.lower()
    denial_markers = (
        "无法",
        "不能",
        "没法",
        "没有暴露",
        "没有可用",
        "cannot",
        "can't",
        "unable",
        "not available",
        "no harness tool",
        "no tool",
    )
    return any(marker in normalized for marker in denial_markers)


def final_attachment_ready_text(result: Mapping[str, JsonValue]) -> str:
    filename = final_attachment_filename(result) or "生成文件"
    file_payload = result.get("file")
    mime_type = file_payload.get("mime_type") if isinstance(file_payload, Mapping) else None
    if filename.lower().endswith(".zip") or mime_type == _ZIP_MIME_TYPE:
        return f"已生成可下载项目 ZIP：{filename}。"
    return f"已生成可下载文件：{filename}。"
