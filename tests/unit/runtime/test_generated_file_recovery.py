from __future__ import annotations

from uuid import uuid4

from agent_hub.runtime.contracts import Artifact, JsonValue
from agent_hub.runtime.generated_file_recovery import (
    generated_file_arguments_sha256,
    reusable_generated_file_result,
)


def _zip_artifact(arguments: dict[str, JsonValue]) -> Artifact:
    artifact_id = str(uuid4())
    return Artifact(
        id=uuid4(),
        type="tool_result",
        producer="implementer",
        content={
            "tool_name": "project.generate_zip",
            "arguments_sha256": generated_file_arguments_sha256(arguments),
            "result": {
                "artifact_id": artifact_id,
                "presentation": "final_attachment",
                "file": {
                    "artifact_id": artifact_id,
                    "filename": "project.zip",
                    "mime_type": "application/zip",
                    "download_url": f"/artifacts/{artifact_id}",
                },
            },
        },
    )


def test_generated_zip_reuse_requires_identical_arguments() -> None:
    original: dict[str, JsonValue] = {
        "title": "Original",
        "files": {"main.py": "print('original')\n"},
    }
    changed: dict[str, JsonValue] = {
        "title": "Changed",
        "files": {"main.py": "print('changed')\n"},
    }
    artifact = _zip_artifact(original)

    assert (
        reusable_generated_file_result(
            "project.generate_zip",
            (artifact,),
            arguments=original,
        )
        is not None
    )
    assert (
        reusable_generated_file_result(
            "project.generate_zip",
            (artifact,),
            arguments=changed,
        )
        is None
    )


def test_generated_zip_reuse_rejects_legacy_result_without_argument_digest() -> None:
    arguments: dict[str, JsonValue] = {
        "title": "Current",
        "files": {"main.py": "print('current')\n"},
    }
    legacy = _zip_artifact(arguments)
    legacy = Artifact(
        id=legacy.id,
        type=legacy.type,
        producer=legacy.producer,
        content={"result": legacy.content["result"]},
    )

    assert (
        reusable_generated_file_result(
            "project.generate_zip",
            (legacy,),
            arguments=arguments,
        )
        is None
    )
