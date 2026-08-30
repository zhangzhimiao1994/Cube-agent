from __future__ import annotations

from pathlib import Path
from uuid import UUID

from agent_hub.capabilities.runtime import RuntimeCapabilityGateway
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
    assert gateway.is_available(TENANT_ID, "presentation.generate_pptx") is True
    stored_path = (
        generated_dir
        / str(TENANT_ID)
        / str(RUN_ID)
        / str(result["artifact_id"])
        / "launch-review.pptx"
    )
    assert stored_path.is_file()


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
