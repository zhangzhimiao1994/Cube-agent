from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_hub.documents.ooxml import write_ooxml_package, xml_document, xml_text

Section = dict[str, Any]


@dataclass(frozen=True)
class DocxBlueprint:
    title: str
    subtitle: str | None = None
    sections: list[Section] = field(default_factory=list)


def build_docx(blueprint: DocxBlueprint, output: Path) -> None:
    title = _required_text(blueprint.title, "title")
    parts = {
        "[Content_Types].xml": _content_types(),
        "_rels/.rels": _package_relationships(),
        "docProps/app.xml": _app_properties(),
        "docProps/core.xml": _core_properties(title),
        "word/_rels/document.xml.rels": _document_relationships(),
        "word/document.xml": _document_xml(blueprint),
        "word/numbering.xml": _numbering_xml(),
        "word/styles.xml": _styles_xml(),
    }
    write_ooxml_package(parts, output)


def _document_xml(blueprint: DocxBlueprint) -> str:
    body = [_paragraph(_required_text(blueprint.title, "title"), style="Title")]
    if blueprint.subtitle:
        body.append(_paragraph(blueprint.subtitle, style="Subtitle"))
    for section in blueprint.sections:
        heading = section.get("heading")
        if heading:
            body.append(_paragraph(heading, style="Heading1"))
        for paragraph in section.get("paragraphs", ()):
            body.append(_paragraph(paragraph))
        for bullet in section.get("bullets", ()):
            body.append(_paragraph(bullet, bullet=True))
    body.append(
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" '
        'w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
    )
    return xml_document(
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}</w:body></w:document>"
    )


def _paragraph(text: object, *, style: str | None = None, bullet: bool = False) -> str:
    properties = ""
    if style:
        properties += f'<w:pStyle w:val="{style}"/>'
    if bullet:
        properties += '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
    ppr = f"<w:pPr>{properties}</w:pPr>" if properties else ""
    return f"<w:p>{ppr}<w:r><w:t>{xml_text(text)}</w:t></w:r></w:p>"


def _required_text(value: str, field_name: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field_name} must not be empty")
    return clean


def _content_types() -> str:
    return xml_document(
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/numbering.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
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
        'Target="word/document.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
        'Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" '
        'Target="docProps/app.xml"/>'
        "</Relationships>"
    )


def _document_relationships() -> str:
    return xml_document(
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" '
        'Target="numbering.xml"/>'
        "</Relationships>"
    )


def _styles_xml() -> str:
    return xml_document(
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
        '<w:rPr><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>'
        '<w:basedOn w:val="Normal"/><w:rPr><w:b/><w:sz w:val="40"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/>'
        '<w:basedOn w:val="Normal"/><w:rPr><w:color w:val="666666"/><w:sz w:val="24"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
        '<w:basedOn w:val="Normal"/><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>'
        "</w:styles>"
    )


def _numbering_xml() -> str:
    return xml_document(
        '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
        '<w:numFmt w:val="bullet"/><w:lvlText w:val="•"/></w:lvl></w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        "</w:numbering>"
    )


def _core_properties(title: str) -> str:
    return xml_document(
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{xml_text(title)}</dc:title><dc:creator>Agent Hub</dc:creator>"
        "</cp:coreProperties>"
    )


def _app_properties() -> str:
    return xml_document(
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        "<Application>Agent Hub</Application></Properties>"
    )
