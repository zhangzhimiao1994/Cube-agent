from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

import pytest

import agent_hub.capabilities.runtime as runtime_module
from agent_hub.capabilities.runtime import RuntimeCapabilityError, RuntimeCapabilityGateway
from agent_hub.files.generated import DOCX_MIME_TYPE, PPTX_MIME_TYPE, ZIP_MIME_TYPE
from agent_hub.runtime.contracts import JsonValue
from agent_hub.skills.sandbox.base import SkillInvocation, SkillResult
from tests.unit.skills.test_package import skill_zip

TENANT_ID = UUID("66666666-6666-4666-8666-666666666666")
RUN_ID = UUID("77777777-7777-4777-8777-777777777777")


class FakeSandbox:
    def __init__(self, *, stdout: str = '{"ok":true}') -> None:
        self.invocations: list[SkillInvocation] = []
        self.stdout = stdout

    async def run(self, invocation: SkillInvocation) -> SkillResult:
        self.invocations.append(invocation)
        return SkillResult(
            exit_code=0,
            stdout=self.stdout,
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


async def test_runtime_gateway_normalizes_skill_json_arrays_to_contract(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / str(TENANT_ID)
    skill_dir.mkdir(parents=True)
    (skill_dir / "docx.zip").write_bytes(skill_zip())
    sandbox = FakeSandbox(stdout='{"items":[1,{"nested":["a","b"]}]}')
    gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path / "skills", skill_sandbox=sandbox)

    result = await gateway.execute(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        actor="writer",
        name="docx",
        arguments={"task": "draft"},
        idempotency_key="skill_json_arrays",
    )

    assert result["result"] == {"items": (1, {"nested": ("a", "b")})}


async def test_runtime_gateway_rejects_non_finite_skill_stdout_numbers(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / str(TENANT_ID)
    skill_dir.mkdir(parents=True)
    (skill_dir / "docx.zip").write_bytes(skill_zip())
    sandbox = FakeSandbox(stdout='{"value":NaN}')
    gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path / "skills", skill_sandbox=sandbox)

    with pytest.raises(RuntimeCapabilityError, match="skill stdout is not JSON serializable"):
        await gateway.execute(
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            actor="writer",
            name="docx",
            arguments={"task": "draft"},
            idempotency_key="skill_nan_stdout",
        )


async def test_runtime_gateway_generates_docx_artifact(tmp_path: Path) -> None:
    generated_artifact_dir = tmp_path / "generated"
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=generated_artifact_dir,
    )

    assert gateway.is_available(TENANT_ID, "document.generate_docx") is True
    assert gateway.is_replay_safe("document.generate_docx") is True

    arguments: Mapping[str, JsonValue] = {
        "title": "Launch Memo",
        "subtitle": "Runtime generated",
        "filename": "launch-memo.docx",
        "sections": (
            {
                "heading": "Summary",
                "paragraphs": ("The gateway generated this document.",),
                "bullets": ("Stored as an artifact",),
            },
        ),
    }

    result = await gateway.execute(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        actor="writer",
        name="document.generate_docx",
        arguments=arguments,
        idempotency_key="docx_1",
    )

    file_metadata = _assert_file_result(result, expected_mime_type=DOCX_MIME_TYPE)
    assert file_metadata["filename"] == "launch-memo.docx"
    storage_key = file_metadata["storage_key"]
    assert isinstance(storage_key, str)
    output = generated_artifact_dir / storage_key
    assert output.is_file()
    with ZipFile(output) as package:
        assert "[Content_Types].xml" in package.namelist()
        assert "word/document.xml" in package.namelist()


async def test_runtime_gateway_generates_pptx_artifact(tmp_path: Path) -> None:
    generated_artifact_dir = tmp_path / "generated"
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=generated_artifact_dir,
    )

    assert gateway.is_available(TENANT_ID, "presentation.generate_pptx") is True
    assert gateway.is_replay_safe("presentation.generate_pptx") is True

    arguments: Mapping[str, JsonValue] = {
        "title": "Technical Blueprint",
        "template_id": "technical-blueprint",
        "slides": (
            {
                "title": "Gateway",
                "bullets": ("DOCX and PPTX tools", "Generated artifact storage"),
            },
        ),
    }

    result = await gateway.execute(
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        actor="designer",
        name="presentation.generate_pptx",
        arguments=arguments,
        idempotency_key="pptx_1",
    )

    file_metadata = _assert_file_result(result, expected_mime_type=PPTX_MIME_TYPE)
    assert file_metadata["filename"] == "technical-blueprint.pptx"
    storage_key = file_metadata["storage_key"]
    assert isinstance(storage_key, str)
    output = generated_artifact_dir / storage_key
    assert output.is_file()
    with ZipFile(output) as package:
        assert "[Content_Types].xml" in package.namelist()
        assert "ppt/presentation.xml" in package.namelist()
        assert "ppt/slides/slide1.xml" in package.namelist()


async def test_runtime_gateway_generates_project_zip_final_artifact(tmp_path: Path) -> None:
    generated_artifact_dir = tmp_path / "generated"
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=generated_artifact_dir,
    )

    assert gateway.is_available(TENANT_ID, "project.generate_zip") is True
    assert gateway.is_replay_safe("project.generate_zip") is True

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

    file_metadata = _assert_file_result(result, expected_mime_type=ZIP_MIME_TYPE)
    assert file_metadata["filename"] == "hello-world.zip"
    assert result["presentation"] == "final_attachment"
    storage_key = file_metadata["storage_key"]
    assert isinstance(storage_key, str)
    output = generated_artifact_dir / storage_key
    assert output.is_file()
    with ZipFile(output) as archive:
        assert archive.namelist() == ["README.md", "main.py"]
        assert archive.read("main.py") == b"print('hello world')\n"


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
        (
            {f"chunk-{index}.txt": "x" * 250_000 for index in range(9)},
            "project content is too large",
        ),
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

    with pytest.raises(
        RuntimeCapabilityError,
        match="presentation must be step_detail or final_attachment",
    ):
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


async def test_runtime_gateway_office_tools_require_configured_artifact_store(
    tmp_path: Path,
) -> None:
    gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path / "skills")

    assert gateway.is_available(TENANT_ID, "document.generate_docx") is True

    with pytest.raises(RuntimeCapabilityError, match="generated artifact store is not configured"):
        await gateway.execute(
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            actor="writer",
            name="document.generate_docx",
            arguments={"title": "Launch Memo"},
            idempotency_key="docx_unconfigured",
        )


async def test_runtime_gateway_rejects_unsafe_docx_filename_before_building(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=tmp_path / "generated",
    )
    build_calls: list[Path] = []

    def fake_build_docx(_blueprint: object, output: Path) -> None:
        build_calls.append(output)

    monkeypatch.setattr(runtime_module, "build_docx", fake_build_docx)

    with pytest.raises(RuntimeCapabilityError, match="filename must not contain path segments"):
        await gateway.execute(
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            actor="writer",
            name="document.generate_docx",
            arguments={"title": "Launch Memo", "filename": "../escape.docx"},
            idempotency_key="docx_unsafe_filename",
        )

    assert build_calls == []
    assert not (tmp_path / "generated").exists()


async def test_runtime_gateway_rejects_unsafe_pptx_filename_before_building(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=tmp_path / "generated",
    )
    build_calls: list[Path] = []

    def fake_build_pptx(_blueprint: object, output: Path) -> None:
        build_calls.append(output)

    monkeypatch.setattr(runtime_module, "build_pptx", fake_build_pptx)

    with pytest.raises(RuntimeCapabilityError, match="filename must not contain path segments"):
        await gateway.execute(
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            actor="designer",
            name="presentation.generate_pptx",
            arguments={"title": "Launch Deck", "filename": "..\\escape.pptx"},
            idempotency_key="pptx_unsafe_filename",
        )

    assert build_calls == []
    assert not (tmp_path / "generated").exists()


async def test_runtime_gateway_pptx_template_error_does_not_echo_input(tmp_path: Path) -> None:
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=tmp_path / "generated",
    )

    with pytest.raises(RuntimeCapabilityError) as error:
        await gateway.execute(
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            actor="designer",
            name="presentation.generate_pptx",
            arguments={"title": "Launch Deck", "template_id": "secret-template-token"},
            idempotency_key="pptx_invalid_template",
        )

    assert str(error.value) == "template_id is invalid"
    assert "secret-template-token" not in str(error.value)


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("document.generate_docx", {"title": ""}, "title must not be empty"),
        ("presentation.generate_pptx", {"template_id": "dark-launch"}, "title must be a string"),
    ],
)
async def test_runtime_gateway_office_tools_raise_stable_errors_for_invalid_payloads(
    tmp_path: Path,
    name: str,
    arguments: Mapping[str, JsonValue],
    message: str,
) -> None:
    gateway = RuntimeCapabilityGateway(
        skill_store_dir=tmp_path / "skills",
        generated_artifact_dir=tmp_path / "generated",
    )

    with pytest.raises(RuntimeCapabilityError, match=message):
        await gateway.execute(
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            actor="writer",
            name=name,
            arguments=arguments,
            idempotency_key="invalid_payload",
        )


async def test_runtime_gateway_rejects_unknown_dotted_skill_ids(tmp_path: Path) -> None:
    gateway = RuntimeCapabilityGateway(skill_store_dir=tmp_path / "skills")

    assert gateway.is_available(TENANT_ID, "web.search") is False
    with pytest.raises(RuntimeCapabilityError, match="capability name is invalid"):
        await gateway.execute(
            tenant_id=TENANT_ID,
            run_id=RUN_ID,
            actor="planner",
            name="web.search",
            arguments={"query": "blocked"},
            idempotency_key="blocked_dotted_name",
        )


def _assert_file_result(
    result: object,
    *,
    expected_mime_type: str,
) -> dict[str, str | int]:
    assert isinstance(result, dict)
    assert isinstance(result["artifact_id"], str)
    assert result["summary"]
    assert result["file"] == result["metadata"]
    file_metadata = result["file"]
    assert isinstance(file_metadata, dict)
    for key in (
        "filename",
        "mime_type",
        "size_bytes",
        "sha256",
        "storage_key",
        "download_url",
    ):
        assert key in file_metadata
    assert file_metadata["artifact_id"] == result["artifact_id"]
    assert file_metadata["mime_type"] == expected_mime_type
    assert isinstance(file_metadata["size_bytes"], int)
    assert file_metadata["size_bytes"] > 0
    assert isinstance(file_metadata["sha256"], str)
    assert len(file_metadata["sha256"]) == 64
    assert isinstance(file_metadata["storage_key"], str)
    assert str(TENANT_ID) in file_metadata["storage_key"]
    assert str(RUN_ID) in file_metadata["storage_key"]
    assert isinstance(file_metadata["download_url"], str)
    assert result["artifact_id"] in file_metadata["download_url"]
    return file_metadata
