from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

import pytest

from agent_hub.documents.docx import DocxBlueprint, build_docx


def test_build_docx_creates_real_word_package(tmp_path: Path) -> None:
    output = tmp_path / "report.docx"
    build_docx(
        DocxBlueprint(
            title="项目周报",
            subtitle="Phase 18",
            sections=[
                {
                    "heading": "结论",
                    "paragraphs": ["系统可以生成真实 Word 文件。"],
                    "bullets": ["离线可用"],
                }
            ],
        ),
        output,
    )

    with ZipFile(output) as package:
        names = set(package.namelist())
        document = package.read("word/document.xml").decode("utf-8")

    assert "[Content_Types].xml" in names
    assert "word/document.xml" in names
    assert "项目周报" in document
    assert "Phase 18" in document
    assert "结论" in document
    assert "系统可以生成真实 Word 文件。" in document
    assert "离线可用" in document


def test_build_docx_escapes_xml_text_and_keeps_visible_text(tmp_path: Path) -> None:
    output = tmp_path / "report.docx"
    visible_text = 'A&B <C> "Q"'
    build_docx(
        DocxBlueprint(
            title=visible_text,
            subtitle=visible_text,
            sections=[
                {
                    "heading": visible_text,
                    "paragraphs": [visible_text],
                    "bullets": [visible_text],
                }
            ],
        ),
        output,
    )

    with ZipFile(output) as package:
        parsed_text = _parse_all_xml_parts_and_collect_text(package)

    assert visible_text in parsed_text


def test_build_docx_rejects_invalid_xml_text_without_writing_file(tmp_path: Path) -> None:
    output = tmp_path / "report.docx"

    with pytest.raises(ValueError, match="not valid XML"):
        build_docx(DocxBlueprint(title=f"bad{chr(1)}title"), output)

    assert not output.exists()


def _parse_all_xml_parts_and_collect_text(package: ZipFile) -> str:
    collected: list[str] = []
    for name in package.namelist():
        if name.endswith((".xml", ".rels")):
            root = ET.fromstring(package.read(name).decode("utf-8"))
            collected.extend(root.itertext())
    return "".join(collected)
