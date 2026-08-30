from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_readmes_document_office_generation_capabilities_and_download_paths() -> None:
    for filename in ("README.md", "README.zh-CN.md"):
        text = (ROOT / filename).read_text(encoding="utf-8")

        for marker in (
            "document.generate_docx",
            "presentation.generate_pptx",
            "consulting-clean",
            "technical-blueprint",
            "dark-launch",
            "generated_artifact_dir",
        ):
            assert marker in text


def test_skills_and_mcp_documents_office_generation_tools() -> None:
    text = (ROOT / "docs" / "skills-and-mcp.md").read_text(encoding="utf-8")

    assert "document.generate_docx" in text
    assert "presentation.generate_pptx" in text
    assert "generated_artifact_dir" in text
