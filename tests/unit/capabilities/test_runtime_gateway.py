from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import UUID
from zipfile import ZipFile

import pytest

from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.base import SkillInvocation, SkillResult
from tests.unit.skills.test_package import skill_zip

TENANT_ID = UUID("66666666-6666-4666-8666-666666666666")
RUN_ID = UUID("77777777-7777-4777-8777-777777777777")


class FakeSandbox:
    def __init__(self) -> None:
        self.invocations: list[SkillInvocation] = []

    async def run(self, invocation: SkillInvocation) -> SkillResult:
        self.invocations.append(invocation)
        return SkillResult(
            exit_code=0,
            stdout='{"ok":true}',
            stderr="",
            timed_out=False,
        )

    async def terminate(self, execution_id: str) -> None:
        del execution_id


async def test_runtime_gateway_executes_calculator_without_external_side_effects(tmp_path: Path) -> None:
    gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path)

    result = await gateway.execute(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        actor="planner",
        name="calculator",
        arguments={"expression": "2 + 3 * 4"},
        idempotency_key="calc_1",
    )

    assert result == {"value": "14"}
    assert gateway.is_replay_safe("calculator") is True


async def test_runtime_gateway_reads_only_configured_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("safe", encoding="utf-8")
    gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path / "skills", workspace_root=workspace)

    result = await gateway.execute(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        actor="researcher",
        name="read_context",
        arguments={"path": "note.txt"},
        idempotency_key="read_1",
    )

    assert result == {"path": "note.txt", "text": "safe", "truncated": False}


async def test_runtime_gateway_accepts_harness_builtin_tool_aliases(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("safe", encoding="utf-8")
    gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path / "skills", workspace_root=workspace)

    calculator = await gateway.execute(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        actor="planner",
        name="calculator.evaluate",
        arguments={"expression": "2 + 3 * 4"},
        idempotency_key="calc_1",
    )
    workspace_read = await gateway.execute(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        actor="researcher",
        name="workspace.read",
        arguments={"path": "note.txt"},
        idempotency_key="read_1",
    )

    assert gateway.is_available(TENANT_ID, "calculator.evaluate") is True
    assert gateway.is_replay_safe("workspace.read") is True
    assert calculator == {"value": "14"}
    assert workspace_read == {"path": "note.txt", "text": "safe", "truncated": False}


async def test_runtime_gateway_generates_docx_file_artifact(tmp_path: Path) -> None:
    generated_dir = tmp_path / "generated"
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=generated_dir,
    )

    result = await gateway.execute(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        actor="document_writer",
        name="document.generate_docx",
        arguments={
            "title": "Delivery Plan",
            "sections": ({"heading": "Scope", "paragraphs": ("Ship the harness.",)},),
        },
        idempotency_key="docx_1",
    )

    file_payload = result["file"]
    assert isinstance(file_payload, dict)
    assert file_payload["filename"] == "delivery-plan.docx"
    assert file_payload["mime_type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert file_payload["size_bytes"] > 0
    assert result["presentation"] == "step_detail"
    assert gateway.is_available(TENANT_ID, "document.generate_docx") is True
    stored_path = (
        generated_dir
        / str(TENANT_ID)
        / str(RUN_ID)
        / str(result["artifact_id"])
        / "delivery-plan.docx"
    )
    assert stored_path.is_file()


async def test_runtime_gateway_generates_pptx_file_artifact(tmp_path: Path) -> None:
    generated_dir = tmp_path / "generated"
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=generated_dir,
    )

    result = await gateway.execute(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        actor="presentation_designer",
        name="presentation.generate_pptx",
        arguments={
            "title": "Launch Review",
            "slides": ({"title": "Status", "bullets": ("Ready", "Verified")},),
        },
        idempotency_key="pptx_1",
    )

    file_payload = result["file"]
    assert isinstance(file_payload, dict)
    assert file_payload["filename"] == "launch-review.pptx"
    assert file_payload["mime_type"] == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert file_payload["size_bytes"] > 0
    assert result["presentation"] == "step_detail"
    assert gateway.is_available(TENANT_ID, "presentation.generate_pptx") is True
    stored_path = (
        generated_dir
        / str(TENANT_ID)
        / str(RUN_ID)
        / str(result["artifact_id"])
        / "launch-review.pptx"
    )
    assert stored_path.is_file()


async def test_runtime_gateway_keeps_explicit_final_file_presentation(tmp_path: Path) -> None:
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=tmp_path / "generated",
    )

    result = await gateway.execute(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        actor="final_synthesizer",
        name="document.generate_docx",
        arguments={
            "title": "Delivery Plan",
            "presentation": "final_attachment",
        },
        idempotency_key="docx_final",
    )

    assert result["presentation"] == "final_attachment"


async def test_runtime_gateway_generates_project_zip_final_artifact(tmp_path: Path) -> None:
    generated_dir = tmp_path / "generated"
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=generated_dir,
    )

    result = await gateway.execute(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        actor="engineer",
        name="project.generate_zip",
        arguments={
            "title": "Hello World",
            "files": {
                "main.py": "print('hello world')\n",
                "README.md": "# Hello World\n\nRun `python main.py`.\n",
            },
        },
        idempotency_key="project_zip_1",
    )

    file_payload = result["file"]
    assert isinstance(file_payload, dict)
    assert file_payload["filename"] == "hello-world.zip"
    assert file_payload["mime_type"] == "application/zip"
    assert result["presentation"] == "final_attachment"
    stored_path = (
        generated_dir
        / str(TENANT_ID)
        / str(RUN_ID)
        / str(result["artifact_id"])
        / "hello-world.zip"
    )
    assert stored_path.is_file()
    with ZipFile(stored_path) as archive:
        assert archive.namelist() == ["README.md", "main.py"]
        assert archive.read("main.py") == b"print('hello world')\n"


async def test_runtime_gateway_accepts_common_project_zip_files_item_wrapper(
    tmp_path: Path,
) -> None:
    generated_dir = tmp_path / "generated"
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=generated_dir,
    )

    result = await gateway.execute(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        actor="engineer",
        name="project.generate_zip",
        arguments={
            "title": "Mofang Hello Python",
            "filename": "mofang-hello-python.zip",
            "presentation": "final_attachment",
            "files": {"item": {"main.py": "print(\"hello mofang\")\n"}},
        },
        idempotency_key="project_zip_wrapped_item",
    )

    stored_path = (
        generated_dir
        / str(TENANT_ID)
        / str(RUN_ID)
        / str(result["artifact_id"])
        / "mofang-hello-python.zip"
    )
    assert stored_path.is_file()
    with ZipFile(stored_path) as archive:
        assert archive.namelist() == ["main.py"]
        assert archive.read("main.py") == b'print("hello mofang")\n'


async def test_runtime_gateway_accepts_common_project_zip_files_list(
    tmp_path: Path,
) -> None:
    generated_dir = tmp_path / "generated"
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=generated_dir,
    )

    result = await gateway.execute(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        actor="engineer",
        name="project.generate_zip",
        arguments=cast(
            Mapping[str, JsonValue],
            {
                "title": "Mofang Hello",
                "filename": "mofang-hello.zip",
                "files": [{"main.py": "print(\"hello mofang\")"}],
            },
        ),
        idempotency_key="project_zip_file_list",
    )

    stored_path = (
        generated_dir
        / str(TENANT_ID)
        / str(RUN_ID)
        / str(result["artifact_id"])
        / "mofang-hello.zip"
    )
    assert stored_path.is_file()
    with ZipFile(stored_path) as archive:
        assert archive.namelist() == ["main.py"]
        assert archive.read("main.py") == b'print("hello mofang")'


async def test_runtime_gateway_rejects_unsafe_project_zip_paths(tmp_path: Path) -> None:
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=tmp_path / "generated",
    )

    for index, path in enumerate(("../main.py", "/tmp/main.py", "src/../../main.py", "NUL.txt")):
        with pytest.raises(RuntimeCapabilityError, match="file path|reserved"):
            await gateway.execute(
                tenant_id=TENANT_ID,
                run_id=RUN_ID,
                actor="engineer",
                name="project.generate_zip",
                arguments={"title": "Unsafe", "files": {path: "content"}},
                idempotency_key=f"unsafe_{index}",
            )


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({}, "files must contain 1 to 64 entries"),
        ({f"file-{index}.txt": "x" for index in range(65)}, "files must contain 1 to 64 entries"),
        ({"large.txt": "x" * 256_001}, "file content is too large"),
        ({f"chunk-{index}.txt": "x" * 250_000 for index in range(9)}, "project content is too large"),
    ],
)
async def test_runtime_gateway_rejects_oversized_project_zip_payloads(
    tmp_path: Path, files: dict[str, str], message: str
) -> None:
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=tmp_path / "generated",
    )

    with pytest.raises(RuntimeCapabilityError, match=message):
        await gateway.execute(
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            actor="engineer",
            name="project.generate_zip",
            arguments={"title": "Oversized", "files": files},
            idempotency_key="project_zip_limits",
        )


async def test_runtime_gateway_rejects_invalid_project_zip_presentation(tmp_path: Path) -> None:
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=tmp_path / "generated",
    )

    with pytest.raises(RuntimeCapabilityError, match="presentation must be step_detail or final_attachment"):
        await gateway.execute(
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            actor="engineer",
            name="project.generate_zip",
            arguments={
                "title": "Invalid Presentation",
                "files": {"main.py": "print('hello')\n"},
                "presentation": "chat_inline",
            },
            idempotency_key="project_zip_bad_presentation",
        )


async def test_runtime_gateway_read_context_accepts_query_without_workspace(tmp_path: Path) -> None:
    gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path / "skills")

    result = await gateway.execute(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        actor="planner",
        name="read_context",
        arguments={"query": "activity plan constraints"},
        idempotency_key="context_1",
    )

    assert result["query"] == "activity plan constraints"
    assert result["matches"] == ()
    assert result["truncated"] is False


async def test_runtime_gateway_invokes_installed_skill_through_sandbox(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / str(TENANT_ID)
    skill_dir.mkdir(parents=True)
    (skill_dir / "docx.zip").write_bytes(skill_zip())
    sandbox = FakeSandbox()
    gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path / "skills", skill_sandbox=sandbox)

    assert gateway.is_available(TENANT_ID, "read_context") is True
    assert gateway.is_available(TENANT_ID, "docx") is True
    assert gateway.is_available(TENANT_ID, "pdf") is False
    assert gateway.is_available(TENANT_ID, "web.search") is False

    result = await gateway.execute(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        actor="writer",
        name="docx",
        arguments={"task": "draft"},
        idempotency_key="skill_1",
    )

    assert result["result"] == {"ok": True}
    assert len(sandbox.invocations) == 1
    assert sandbox.invocations[0].package_path == skill_dir / "docx.zip"
    assert sandbox.invocations[0].input["arguments"] == {"task": "draft"}
