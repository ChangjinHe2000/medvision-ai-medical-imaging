from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from xml.etree import ElementTree as ET


WHITESPACE_RE = re.compile(r"\s+")
EMPTY_CITATION_RE = re.compile(r"\[\s*(?:[,;]\s*)*\]")
SPACE_BEFORE_PUNCTUATION_RE = re.compile(r"\s+([,.;:!?%\]\)])")
SPACE_AFTER_OPEN_RE = re.compile(r"([\[\(])\s+")
SKIPPED_SECTION_TITLES = {
    "acknowledgements",
    "associated data",
    "author contributions",
    "competing interests",
    "conflict of interest",
    "conflicts of interest",
    "conflicts of interest disclosure",
    "contributor information",
    "data availability statement",
    "declaration of interest",
    "ethical approval",
    "ethics",
    "funding",
    "funding statement",
    "guarantor",
    "informed consent statement",
    "institutional review board statement",
    "sources of funding",
    "supplementary information",
}


@dataclass
class DocumentSection:
    path: str
    blocks: list[str] = field(default_factory=list)


@dataclass
class ParsedDocument:
    document_id: str
    title: str
    year: Optional[int]
    doi: str
    pmcid: str
    source_url: str
    license: str
    source_file: str
    sections: list[DocumentSection]


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def normalize_text(value: str) -> str:
    text = WHITESPACE_RE.sub(" ", value or " ").strip()
    text = EMPTY_CITATION_RE.sub("", text)
    text = SPACE_BEFORE_PUNCTUATION_RE.sub(r"\1", text)
    text = SPACE_AFTER_OPEN_RE.sub(r"\1", text)
    return text.strip()


def child_by_name(element: ET.Element, name: str) -> Optional[ET.Element]:
    return next((child for child in element if local_name(child.tag) == name), None)


def descendants_by_name(element: ET.Element, name: str) -> Iterable[ET.Element]:
    return (node for node in element.iter() if local_name(node.tag) == name)


def element_text(element: Optional[ET.Element], skip_citations: bool = True) -> str:
    if element is None:
        return ""

    parts: list[str] = []

    def visit(node: ET.Element) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            is_citation = (
                local_name(child.tag) == "xref"
                and child.attrib.get("ref-type") == "bibr"
            )
            if not (skip_citations and is_citation):
                visit(child)
            if child.tail:
                parts.append(child.tail)

    visit(element)
    return normalize_text(" ".join(parts))


def first_descendant_text(element: ET.Element, name: str) -> str:
    node = next(descendants_by_name(element, name), None)
    return element_text(node)


def article_id(article_meta: ET.Element, id_type: str) -> str:
    for node in descendants_by_name(article_meta, "article-id"):
        if node.attrib.get("pub-id-type") == id_type:
            return element_text(node, skip_citations=False)
    return ""


def publication_year(article_meta: ET.Element) -> Optional[int]:
    dates = list(descendants_by_name(article_meta, "pub-date"))
    dates.sort(key=lambda node: node.attrib.get("pub-type") not in {"epub", "electronic"})
    for date in dates:
        raw_year = first_descendant_text(date, "year")
        if raw_year.isdigit():
            return int(raw_year)
    return None


def license_metadata(article_meta: ET.Element) -> str:
    license_node = next(descendants_by_name(article_meta, "license"), None)
    if license_node is None:
        return ""
    references = [
        normalize_text(node.text or "")
        for node in license_node.iter()
        if local_name(node.tag) == "license_ref" and normalize_text(node.text or "")
    ]
    license_text = element_text(license_node, skip_citations=False)
    values = references + ([license_text] if license_text else [])
    return " | ".join(dict.fromkeys(values))


def table_text(table_wrap: ET.Element) -> str:
    label = element_text(child_by_name(table_wrap, "label"))
    caption = element_text(child_by_name(table_wrap, "caption"))
    lines = [value for value in (label, caption) if value]
    for row in descendants_by_name(table_wrap, "tr"):
        cells = [
            element_text(cell)
            for cell in row
            if local_name(cell.tag) in {"th", "td"}
        ]
        cells = [cell for cell in cells if cell]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(normalize_text(line) for line in lines if normalize_text(line))


def list_text(list_element: ET.Element) -> str:
    items = []
    for item in descendants_by_name(list_element, "list-item"):
        text = element_text(item)
        if text:
            items.append(f"- {text}")
    return "\n".join(items)


def direct_content_blocks(container: ET.Element) -> list[str]:
    blocks: list[str] = []
    for child in container:
        name = local_name(child.tag)
        if name in {"p", "statement", "disp-quote", "boxed-text"}:
            text = element_text(child)
        elif name == "list":
            text = list_text(child)
        elif name == "table-wrap":
            text = table_text(child)
        else:
            continue
        if text:
            blocks.append(text)
    return blocks


def parse_section(section: ET.Element, parents: list[str]) -> list[DocumentSection]:
    title = element_text(child_by_name(section, "title"))
    if title.casefold() in SKIPPED_SECTION_TITLES:
        return []
    path_parts = parents + ([title] if title else [])
    path = " > ".join(path_parts) if path_parts else "Main text"
    parsed: list[DocumentSection] = []
    blocks = direct_content_blocks(section)
    if blocks:
        parsed.append(DocumentSection(path=path, blocks=blocks))
    for child in section:
        if local_name(child.tag) == "sec":
            parsed.extend(parse_section(child, path_parts))
    return parsed


def parse_jats(path: Path) -> ParsedDocument:
    root = ET.parse(path).getroot()
    article_meta = next(descendants_by_name(root, "article-meta"), None)
    if article_meta is None:
        raise ValueError(f"Not a JATS article: {path.name}")

    title_group = next(descendants_by_name(article_meta, "title-group"), article_meta)
    title = first_descendant_text(title_group, "article-title") or path.stem
    pmcid = article_id(article_meta, "pmcid")
    doi = article_id(article_meta, "doi")
    document_id = pmcid or article_id(article_meta, "pmid") or path.stem
    source_url = f"https://europepmc.org/articles/{pmcid}" if pmcid else ""

    sections: list[DocumentSection] = []
    for abstract in descendants_by_name(article_meta, "abstract"):
        if abstract.attrib.get("abstract-type", "").casefold() == "graphical":
            continue
        blocks = direct_content_blocks(abstract)
        if not blocks:
            abstract_text = element_text(abstract)
            blocks = [abstract_text] if abstract_text else []
        if blocks:
            abstract_title = element_text(child_by_name(abstract, "title")) or "Abstract"
            if abstract_title.casefold() == "graphical abstract":
                continue
            sections.append(DocumentSection(path=abstract_title, blocks=blocks))

    body = next(descendants_by_name(root, "body"), None)
    if body is not None:
        main_blocks = direct_content_blocks(body)
        if main_blocks:
            sections.append(DocumentSection(path="Main text", blocks=main_blocks))
        for child in body:
            if local_name(child.tag) == "sec":
                sections.extend(parse_section(child, []))

    if not sections:
        raise ValueError(f"JATS article has no extractable content: {path.name}")

    return ParsedDocument(
        document_id=document_id,
        title=title,
        year=publication_year(article_meta),
        doi=doi,
        pmcid=pmcid,
        source_url=source_url,
        license=license_metadata(article_meta),
        source_file=path.name,
        sections=sections,
    )


def parse_atom(path: Path) -> ParsedDocument:
    root = ET.parse(path).getroot()
    entry = next(descendants_by_name(root, "entry"), None)
    if entry is None:
        raise ValueError(f"Atom feed has no entry: {path.name}")
    title = first_descendant_text(entry, "title") or path.stem
    summary = first_descendant_text(entry, "summary")
    published = first_descendant_text(entry, "published")
    year = int(published[:4]) if published[:4].isdigit() else None
    source_url = first_descendant_text(entry, "id")
    document_id = path.stem
    return ParsedDocument(
        document_id=document_id,
        title=title,
        year=year,
        doi="",
        pmcid="",
        source_url=source_url,
        license="",
        source_file=path.name,
        sections=[DocumentSection(path="Abstract", blocks=[summary])] if summary else [],
    )


def parse_markdown(path: Path) -> ParsedDocument:
    lines = path.read_text(encoding="utf-8").splitlines()
    title = path.stem
    heading_stack: list[str] = []
    section_path = "Main text"
    blocks_by_section: dict[str, list[str]] = {}
    paragraph: list[str] = []
    in_code_block = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        text = normalize_text(" ".join(paragraph))
        if text:
            blocks_by_section.setdefault(section_path, []).append(text)
        paragraph = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            in_code_block = not in_code_block
            continue
        if in_code_block:
            if stripped:
                paragraph.append(stripped)
            continue
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading_match:
            flush_paragraph()
            level = len(heading_match.group(1))
            heading = normalize_text(heading_match.group(2))
            if level == 1 and title == path.stem:
                title = heading
            heading_stack[:] = heading_stack[: level - 1]
            heading_stack.append(heading)
            section_path = " > ".join(heading_stack)
            continue
        if not stripped:
            flush_paragraph()
            continue
        paragraph.append(stripped)
    flush_paragraph()

    source_url = (
        "https://github.com/wasserth/TotalSegmentator"
        if "totalsegmentator_official_readme" in path.stem
        else ""
    )
    sections = [
        DocumentSection(path=name, blocks=blocks)
        for name, blocks in blocks_by_section.items()
        if blocks
    ]
    return ParsedDocument(
        document_id=path.stem,
        title=title,
        year=None,
        doi="",
        pmcid="",
        source_url=source_url,
        license="",
        source_file=path.name,
        sections=sections,
    )


def parse_document(path: Path) -> ParsedDocument:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return parse_markdown(path)
    if suffix != ".xml":
        raise ValueError(f"Unsupported knowledge document: {path.name}")
    root = ET.parse(path).getroot()
    root_name = local_name(root.tag)
    if root_name == "article":
        return parse_jats(path)
    if root_name == "feed":
        return parse_atom(path)
    raise ValueError(f"Unsupported XML root '{root_name}': {path.name}")
