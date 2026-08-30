from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

import pytest

from agent_hub.documents.pptx import PptxBlueprint, build_pptx


def test_build_pptx_uses_requested_builtin_template(tmp_path: Path) -> None:
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
        slide1 = package.read("ppt/slides/slide1.xml").decode("utf-8")
        slide2 = package.read("ppt/slides/slide2.xml").decode("utf-8")
        slide3 = package.read("ppt/slides/slide3.xml").decode("utf-8")

    assert "[Content_Types].xml" in names
    assert "ppt/presentation.xml" in names
    assert "ppt/slides/slide1.xml" in names
    assert "ppt/slides/slide2.xml" in names
    assert "ppt/slides/slide3.xml" in names
    assert "产品发布" in slide1
    assert "technical-blueprint" in slide1
    assert "目标" in slide2
    assert "生成真实 PPTX" in slide2
    assert "使用内置模板" in slide2
    assert "验收" in slide3
    assert "可下载" in slide3
    assert "可离线" in slide3


def test_build_pptx_uses_consulting_clean_by_default(tmp_path: Path) -> None:
    output = tmp_path / "deck.pptx"
    build_pptx(PptxBlueprint(title="默认模板", slides=[]), output)

    with ZipFile(output) as package:
        slide = package.read("ppt/slides/slide1.xml").decode("utf-8")

    assert "默认模板" in slide
    assert "consulting-clean" in slide


def test_build_pptx_uses_dark_launch_template(tmp_path: Path) -> None:
    output = tmp_path / "deck.pptx"
    build_pptx(PptxBlueprint(title="暗色发布", template_id="dark-launch", slides=[]), output)

    with ZipFile(output) as package:
        slide = package.read("ppt/slides/slide1.xml").decode("utf-8")

    assert "暗色发布" in slide
    assert "dark-launch" in slide


def test_build_pptx_uses_unique_shape_ids_within_each_slide(tmp_path: Path) -> None:
    output = tmp_path / "deck.pptx"
    build_pptx(PptxBlueprint(title="产品发布", slides=[]), output)

    with ZipFile(output) as package:
        slide = package.read("ppt/slides/slide1.xml").decode("utf-8")

    ids = [
        element.attrib["id"]
        for element in ET.fromstring(slide).iter()
        if element.tag.endswith("cNvPr")
    ]
    assert len(ids) == len(set(ids))


def test_build_pptx_escapes_xml_text_and_keeps_visible_text(tmp_path: Path) -> None:
    output = tmp_path / "deck.pptx"
    visible_text = 'A&B <C> "Q"'
    build_pptx(
        PptxBlueprint(
            title=visible_text,
            subtitle=visible_text,
            slides=[{"title": visible_text, "bullets": [visible_text], "paragraphs": [visible_text]}],
        ),
        output,
    )

    with ZipFile(output) as package:
        parsed_text = _parse_all_xml_parts_and_collect_text(package)

    assert visible_text in parsed_text


def test_build_pptx_rejects_invalid_xml_text_without_writing_file(tmp_path: Path) -> None:
    output = tmp_path / "deck.pptx"

    with pytest.raises(ValueError, match="not valid XML"):
        build_pptx(PptxBlueprint(title=f"bad{chr(1)}title", slides=[]), output)

    assert not output.exists()


def test_build_pptx_rejects_unknown_template(tmp_path: Path) -> None:
    output = tmp_path / "deck.pptx"

    with pytest.raises(ValueError, match="unknown PPTX template"):
        build_pptx(PptxBlueprint(title="产品发布", template_id="unknown", slides=[]), output)


def _parse_all_xml_parts_and_collect_text(package: ZipFile) -> str:
    collected: list[str] = []
    for name in package.namelist():
        if name.endswith((".xml", ".rels")):
            root = ET.fromstring(package.read(name).decode("utf-8"))
            collected.extend(root.itertext())
    return "".join(collected)
