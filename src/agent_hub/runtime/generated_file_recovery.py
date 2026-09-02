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
    for artifact in reversed(artifacts):
        if artifact.type != "tool_result":
            continue
        result = artifact.content.get("result")
        if _is_final_project_zip_result(result):
            return cast(Mapping[str, JsonValue], result)
    return None


def _is_final_project_zip_result(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    file_payload = value.get("file")
    if not isinstance(file_payload, Mapping):
        return False
    filename = file_payload.get("filename")
    mime_type = file_payload.get("mime_type")
    download_url = file_payload.get("download_url")
    artifact_id = value.get("artifact_id")
    return (
        value.get("presentation") == "final_attachment"
        and type(artifact_id) is str
        and type(filename) is str
        and filename.endswith(".zip")
        and mime_type == _ZIP_MIME_TYPE
        and type(download_url) is str
        and bool(download_url.strip())
    )
