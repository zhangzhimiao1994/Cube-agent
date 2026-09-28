from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Final, cast

from agent_hub.runtime.contracts import JsonValue

PROJECT_ZIP_TOOL_NAME: Final = "project.generate_zip"
PROJECT_ZIP_MAX_FILES: Final = 64
PROJECT_ZIP_MAX_FILE_BYTES: Final = 256_000
PROJECT_ZIP_SOFT_TOTAL_SOURCE_BYTES: Final = 2_000_000
PROJECT_ZIP_ABSOLUTE_TOTAL_SOURCE_BYTES: Final = 10_000_000
PROJECT_ZIP_MODEL_EVIDENCE_INLINE_BYTES: Final = 128_000
PROJECT_ZIP_INCREMENTAL_TOOLS: Final = (
    "workspace.write_text",
    "workspace.list",
    "workspace.bundle",
)


def _plain_json(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_plain_json(item) for item in value)
    return value


def _encoded_arguments(arguments: Mapping[str, JsonValue]) -> bytes:
    return json.dumps(
        _plain_json(cast(JsonValue, arguments)),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def project_zip_audit_arguments(
    arguments: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    encoded = _encoded_arguments(arguments)
    files = arguments.get("files")
    file_count = len(files) if isinstance(files, Mapping | tuple) else 0
    return {
        "audit": {
            "arguments_sha256": hashlib.sha256(encoded).hexdigest(),
            "encoded_bytes": len(encoded),
            "file_count": file_count,
            "omitted": True,
        }
    }


def project_zip_evidence_arguments(
    arguments: Mapping[str, JsonValue],
) -> Mapping[str, JsonValue]:
    encoded = _encoded_arguments(arguments)
    if len(encoded) <= PROJECT_ZIP_MODEL_EVIDENCE_INLINE_BYTES:
        return arguments
    return cast(Mapping[str, JsonValue], project_zip_audit_arguments(arguments))


def project_zip_input_schema() -> dict[str, JsonValue]:
    file_content: dict[str, JsonValue] = {
        "type": "string",
        "x-max-utf8-bytes": PROJECT_ZIP_MAX_FILE_BYTES,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ("title", "files"),
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 240},
            "files": {
                "maxProperties": PROJECT_ZIP_MAX_FILES,
                "x-max-file-utf8-bytes": PROJECT_ZIP_MAX_FILE_BYTES,
                "x-max-total-utf8-bytes": PROJECT_ZIP_ABSOLUTE_TOTAL_SOURCE_BYTES,
                "anyOf": (
                    {
                        "type": "object",
                        "minProperties": 1,
                        "maxProperties": PROJECT_ZIP_MAX_FILES,
                        "additionalProperties": file_content,
                    },
                    {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": PROJECT_ZIP_MAX_FILES,
                        "items": {
                            "type": "object",
                            "minProperties": 1,
                            "maxProperties": 1,
                            "additionalProperties": file_content,
                        },
                    },
                ),
            },
            "filename": {"type": "string", "minLength": 1, "maxLength": 240},
            "summary": {"type": "string", "maxLength": 2_000},
            "presentation": {
                "type": "string",
                "enum": ("step_detail", "final_attachment"),
            },
            "project_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "workspace_session_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
            },
        },
    }


def project_zip_operational_limits() -> dict[str, JsonValue]:
    return {
        "max_files": PROJECT_ZIP_MAX_FILES,
        "max_file_bytes": PROJECT_ZIP_MAX_FILE_BYTES,
        "soft_total_source_bytes": PROJECT_ZIP_SOFT_TOTAL_SOURCE_BYTES,
        "absolute_total_source_bytes": PROJECT_ZIP_ABSOLUTE_TOTAL_SOURCE_BYTES,
        "overflow_strategy": {
            "required_tools": PROJECT_ZIP_INCREMENTAL_TOOLS,
            "reason": "incremental_workspace_required",
        },
    }
