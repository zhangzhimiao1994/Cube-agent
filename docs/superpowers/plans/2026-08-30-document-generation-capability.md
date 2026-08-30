# Document Generation Capability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add offline-capable Word and PowerPoint generation as real downloadable run artifacts with built-in PPT templates.

**Architecture:** Implement deterministic OOXML builders and a tenant/run-scoped generated file store, expose them through the existing runtime capability gateway, project downloadable artifact metadata through the admin run API, and render file artifact cards in the frontend. Treat DOCX/PPTX as tool capabilities, not model capabilities.

**Tech Stack:** Python 3.12 standard library OOXML generation (`zipfile`, XML escaping), existing FastAPI/FileResponse APIs, existing runtime `Artifact` JSON contract, React/Vitest frontend.

---

## File Structure

- Create `src/agent_hub/documents/__init__.py`: package marker.
- Create `src/agent_hub/documents/ooxml.py`: shared OOXML zip helpers, XML escaping, safe filenames, file metadata.
- Create `src/agent_hub/documents/docx.py`: DOCX blueprint validation and builder.
- Create `src/agent_hub/documents/pptx.py`: PPTX blueprint validation, built-in templates, and builder.
- Create `src/agent_hub/documents/store.py`: tenant/run/artifact-scoped generated file store.
- Modify `src/agent_hub/capabilities/runtime.py`: register and execute `document.generate_docx` and `presentation.generate_pptx`.
- Modify `src/agent_hub/capabilities/tools/registry.py`: expose built-in tool names.
- Modify `src/agent_hub/runtime/worker.py`: pass generated artifact store path into the capability gateway.
- Modify `src/agent_hub/api/routers/admin.py`: project file artifact metadata and add download endpoint.
- Modify `web/src/api/client.ts`: extend run artifact schema.
- Modify `web/src/pages/RunsPage.tsx`: render file artifact cards in chat and workforce details.
- Modify `web/src/pages/RunDetailPage.tsx`: render file artifact cards in run detail.
- Modify `README.md` and `README.zh-CN.md`: document offline Word/PPT generation and templates.

## Task 1: Deterministic OOXML builders

- [ ] Write failing tests in `tests/unit/documents/test_docx_generation.py`:

```python
from zipfile import ZipFile

from agent_hub.documents.docx import DocxBlueprint, build_docx


def test_build_docx_creates_real_word_package(tmp_path):
    output = tmp_path / "report.docx"
    build_docx(
        DocxBlueprint(
            title="项目周报",
            subtitle="Phase 18",
            sections=[{"heading": "结论", "paragraphs": ["系统可以生成真实 Word 文件。"], "bullets": ["离线可用"]}],
        ),
        output,
    )

    with ZipFile(output) as package:
        names = set(package.namelist())
        document = package.read("word/document.xml").decode("utf-8")

    assert "[Content_Types].xml" in names
    assert "word/document.xml" in names
    assert "项目周报" in document
    assert "离线可用" in document
```

- [ ] Write failing tests in `tests/unit/documents/test_pptx_generation.py`:

```python
from zipfile import ZipFile

from agent_hub.documents.pptx import PptxBlueprint, build_pptx


def test_build_pptx_uses_requested_builtin_template(tmp_path):
    output = tmp_path / "deck.pptx"
    build_pptx(
        PptxBlueprint(
            title="产品发布",
            template_id="technical-blueprint",
            slides=[
                {"title": "目标", "bullets": ["生成真实 PPTX", "使用内置模板"]},
                {"title": "验收", "bullets": ["可下载", "可离线"]},
            ],
        ),
        output,
    )

    with ZipFile(output) as package:
        names = set(package.namelist())
        slide = package.read("ppt/slides/slide1.xml").decode("utf-8")

    assert "[Content_Types].xml" in names
    assert "ppt/presentation.xml" in names
    assert "ppt/slides/slide1.xml" in names
    assert "产品发布" in slide
    assert "technical-blueprint" in slide
```

- [ ] Run each test and confirm it fails because `agent_hub.documents` does not exist.
- [ ] Implement the smallest DOCX/PPTX builders using Python standard library only.
- [ ] Re-run both document tests until they pass.

## Task 2: Generated file store and capability gateway

- [ ] Write failing tests in `tests/unit/capabilities/test_runtime_gateway.py` proving:
  - `document.generate_docx` is available and replay-safe.
  - `presentation.generate_pptx` is available and replay-safe.
  - each capability returns `artifact_id`, `filename`, `mime_type`, `size_bytes`, `sha256`, `storage_key`, and `download_url`.
  - invalid empty titles are rejected.
- [ ] Implement `GeneratedArtifactStore` in `src/agent_hub/documents/store.py`.
- [ ] Modify `RuntimeCapabilityGateway` to accept `generated_artifact_dir`.
- [ ] Add `_execute_generate_docx()` and `_execute_generate_pptx()`.
- [ ] Add both capability names to `_REPLAY_SAFE` and built-in registry.
- [ ] Re-run capability tests until they pass.

## Task 3: Runtime artifact metadata preservation and API download

- [ ] Write failing tests in `tests/api/test_admin_resources.py` proving `_admin_run_artifact()` exposes safe file metadata for `tool_result` content and that the new download endpoint returns the file with correct media type.
- [ ] Add optional fields to `RunArtifactResponse`.
- [ ] Extend `_admin_run_artifact()` to project safe file metadata.
- [ ] Add `GET /api/v1/admin/runs/{run_id}/artifacts/{artifact_id}/download`.
- [ ] Ensure the download endpoint resolves only files under the generated artifact root and enforces tenant/run ownership.
- [ ] Re-run admin API tests until they pass.

## Task 4: Frontend artifact cards

- [ ] Write failing tests in `web/src/pages/OperationalPages.test.tsx` and `web/src/pages/RunDetailPage.test.tsx` proving DOCX/PPTX artifacts render as file cards with type badge, filename, size, and download link.
- [ ] Extend `RunArtifactSchema` and event artifact schema in `web/src/api/client.ts`.
- [ ] Add a shared local file-card rendering helper/component in the touched page files or an existing component location if one exists.
- [ ] Render file artifacts in chat messages, run detail, and sub-agent workforce drawer.
- [ ] Re-run targeted frontend tests until they pass.

## Task 5: Routing, docs, and verification

- [ ] Add role/task routing hints for Word/PPT generation without adding new `ModelCapability` values.
- [ ] Update README files with:
  - capability names,
  - built-in templates,
  - offline/Docker behavior,
  - how generated artifacts are downloaded.
- [ ] Run focused backend tests:

```powershell
E:\code_x\agent-hub\.venv\Scripts\python.exe -m pytest tests\unit\documents tests\unit\capabilities\test_runtime_gateway.py tests\api\test_admin_resources.py -q
```

- [ ] Run frontend tests:

```powershell
npm --prefix web test -- --run src/pages/OperationalPages.test.tsx src/pages/RunDetailPage.test.tsx --reporter=dot
```

- [ ] Run quality gates:

```powershell
E:\code_x\agent-hub\.venv\Scripts\python.exe -m ruff check .
E:\code_x\agent-hub\.venv\Scripts\python.exe -m mypy --strict src tests
npm --prefix web test -- --run --reporter=dot
npm --prefix web run lint
npm --prefix web run build
git diff --check
```

- [ ] Commit, push to `CubeAgent/main`, monitor GitHub Actions, deploy to `prod-web-01`, run production DOCX/PPTX generation probes, clean temporary files, and update local handoff.
