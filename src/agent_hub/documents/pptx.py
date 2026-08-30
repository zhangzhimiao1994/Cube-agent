from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_hub.documents.ooxml import write_ooxml_package, xml_document, xml_text

Slide = dict[str, Any]


@dataclass(frozen=True)
class PptxTemplate:
    template_id: str
    background: str
    accent: str
    text: str
    subtitle: str


TEMPLATES: dict[str, PptxTemplate] = {
    "consulting-clean": PptxTemplate(
        template_id="consulting-clean",
        background="FFFFFF",
        accent="1F4E79",
        text="1F2937",
        subtitle="64748B",
    ),
    "technical-blueprint": PptxTemplate(
        template_id="technical-blueprint",
        background="F4F8FB",
        accent="0F62FE",
        text="102A43",
        subtitle="486581",
    ),
    "dark-launch": PptxTemplate(
        template_id="dark-launch",
        background="111827",
        accent="38BDF8",
        text="F8FAFC",
        subtitle="CBD5E1",
    ),
}


@dataclass(frozen=True)
class PptxBlueprint:
    title: str
    subtitle: str | None = None
    template_id: str = "consulting-clean"
    slides: list[Slide] = field(default_factory=list)


def build_pptx(blueprint: PptxBlueprint, output: Path) -> None:
    title = _required_text(blueprint.title, "title")
    template = _template_for(blueprint.template_id)
    slides = [_cover_slide(title, blueprint.subtitle, template)]
    slides.extend(_content_slide(slide, template, index) for index, slide in enumerate(blueprint.slides, 2))

    parts = {
        "[Content_Types].xml": _content_types(len(slides)),
        "_rels/.rels": _package_relationships(),
        "docProps/app.xml": _app_properties(len(slides)),
        "docProps/core.xml": _core_properties(title),
        "ppt/presentation.xml": _presentation_xml(len(slides)),
        "ppt/_rels/presentation.xml.rels": _presentation_relationships(len(slides)),
    }
    for index, slide_xml in enumerate(slides, 1):
        parts[f"ppt/slides/slide{index}.xml"] = slide_xml

    write_ooxml_package(parts, output)


def _template_for(template_id: str) -> PptxTemplate:
    try:
        return TEMPLATES[template_id or "consulting-clean"]
    except KeyError as exc:
        valid = ", ".join(sorted(TEMPLATES))
        raise ValueError(f"unknown PPTX template: {template_id!r}; expected one of {valid}") from exc


def _cover_slide(title: str, subtitle: str | None, template: PptxTemplate) -> str:
    subtitle_text = subtitle or template.template_id
    marker = f"template:{template.template_id}"
    shapes = [
        _background(2, template.background),
        _text_box(3, title, 700_000, 1_450_000, 8_000_000, 700_000, 42, template.text, bold=True),
        _text_box(4, subtitle_text, 720_000, 2_300_000, 7_000_000, 380_000, 20, template.subtitle),
        _text_box(5, marker, 720_000, 4_850_000, 4_600_000, 240_000, 10, template.accent),
    ]
    return _slide_xml(shapes)


def _content_slide(slide: Slide, template: PptxTemplate, index: int) -> str:
    title = str(slide.get("title") or f"Slide {index - 1}")
    shapes = [
        _background(2, template.background),
        _text_box(3, title, 620_000, 450_000, 8_300_000, 520_000, 28, template.text, bold=True),
        _accent_rule(4, template.accent),
    ]
    top = 1_350_000
    shape_id = 5
    for bullet in slide.get("bullets", ()):
        shapes.append(
            _text_box(shape_id, f"• {bullet}", 850_000, top, 7_900_000, 320_000, 18, template.text)
        )
        shape_id += 1
        top += 460_000
    for paragraph in slide.get("paragraphs", ()):
        shapes.append(
            _text_box(shape_id, paragraph, 850_000, top, 7_900_000, 360_000, 16, template.text)
        )
        shape_id += 1
        top += 420_000
    return _slide_xml(shapes)


def _slide_xml(shapes: list[str]) -> str:
    return xml_document(
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        "<p:cSld><p:spTree>"
        "<p:nvGrpSpPr><p:cNvPr id=\"1\" name=\"\"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>"
        "<p:grpSpPr><a:xfrm><a:off x=\"0\" y=\"0\"/><a:ext cx=\"0\" cy=\"0\"/>"
        "<a:chOff x=\"0\" y=\"0\"/><a:chExt cx=\"0\" cy=\"0\"/></a:xfrm></p:grpSpPr>"
        f"{''.join(shapes)}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
    )


def _background(shape_id: int, color: str) -> str:
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="Background"/><p:cNvSpPr/>'
        "<p:nvPr/></p:nvSpPr>"
        '<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="9144000" cy="5143500"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr>'
        "<p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody></p:sp>"
    )


def _accent_rule(shape_id: int, color: str) -> str:
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="Accent"/><p:cNvSpPr/>'
        "<p:nvPr/></p:nvSpPr>"
        '<p:spPr><a:xfrm><a:off x="620000" y="1040000"/><a:ext cx="1800000" cy="36000"/>'
        '</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr>'
        "<p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody></p:sp>"
    )


def _text_box(
    shape_id: int,
    text: object,
    x: int,
    y: int,
    cx: int,
    cy: int,
    size: int,
    color: str,
    *,
    bold: bool = False,
) -> str:
    bold_attr = ' b="1"' if bold else ""
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="Text"/><p:cNvSpPr txBox="1"/>'
        "<p:nvPr/>"
        '</p:nvSpPr><p:spPr><a:xfrm>'
        f'<a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln>'
        "</p:spPr><p:txBody><a:bodyPr wrap=\"square\"/><a:lstStyle/><a:p>"
        f'<a:r><a:rPr lang="zh-CN" sz="{size * 100}"{bold_attr}>'
        f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill></a:rPr><a:t>{xml_text(text)}</a:t></a:r>'
        "</a:p></p:txBody></p:sp>"
    )


def _presentation_xml(slide_count: int) -> str:
    ids = "".join(
        f'<p:sldId id="{256 + index}" r:id="rId{index}"/>' for index in range(1, slide_count + 1)
    )
    return xml_document(
        '<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        f"<p:sldIdLst>{ids}</p:sldIdLst>"
        '<p:sldSz cx="9144000" cy="5143500" type="screen16x9"/>'
        '<p:notesSz cx="6858000" cy="9144000"/>'
        "</p:presentation>"
    )


def _presentation_relationships(slide_count: int) -> str:
    relationships = "".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
        f'Target="slides/slide{index}.xml"/>'
        for index in range(1, slide_count + 1)
    )
    return xml_document(
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{relationships}</Relationships>"
    )


def _content_types(slide_count: int) -> str:
    slide_overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for index in range(1, slide_count + 1)
    )
    return xml_document(
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        f"{slide_overrides}"
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        "</Types>"
    )


def _package_relationships() -> str:
    return xml_document(
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="ppt/presentation.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
        'Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" '
        'Target="docProps/app.xml"/>'
        "</Relationships>"
    )


def _core_properties(title: str) -> str:
    return xml_document(
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{xml_text(title)}</dc:title><dc:creator>Agent Hub</dc:creator>"
        "</cp:coreProperties>"
    )


def _app_properties(slide_count: int) -> str:
    return xml_document(
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        f"<Application>Agent Hub</Application><Slides>{slide_count}</Slides></Properties>"
    )


def _required_text(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} must not be empty")
    return clean
