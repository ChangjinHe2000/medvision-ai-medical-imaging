from __future__ import annotations

import re
from typing import Iterable

from .jats_parser import DocumentSection, ParsedDocument, normalize_text


TOKEN_RE = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9]+(?:[-_/.'’][A-Za-z0-9]+)*|[^\s]")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？；;])\s+|[\r\n]+")


def estimate_tokens(text: str) -> int:
    return len(TOKEN_RE.findall(text))


def split_long_unit(text: str, max_tokens: int) -> list[str]:
    if estimate_tokens(text) <= max_tokens:
        return [text]
    pieces = [normalize_text(value) for value in text.splitlines() if normalize_text(value)]
    if len(pieces) > 1:
        output: list[str] = []
        for piece in pieces:
            output.extend(split_long_unit(piece, max_tokens))
        return output

    words = text.split()
    if len(words) <= 1:
        characters = list(text)
        step = max(1, max_tokens)
        return ["".join(characters[index:index + step]) for index in range(0, len(characters), step)]

    output = []
    current: list[str] = []
    current_tokens = 0
    for word in words:
        word_tokens = estimate_tokens(word)
        if current and current_tokens + word_tokens > max_tokens:
            output.append(" ".join(current))
            current = []
            current_tokens = 0
        current.append(word)
        current_tokens += word_tokens
    if current:
        output.append(" ".join(current))
    return output


def section_units(section: DocumentSection, max_tokens: int) -> list[str]:
    units: list[str] = []
    for block in section.blocks:
        sentences = [normalize_text(value) for value in SENTENCE_SPLIT_RE.split(block)]
        sentences = [value for value in sentences if value]
        if not sentences:
            continue
        for sentence in sentences:
            units.extend(split_long_unit(sentence, max_tokens))
    return units


def overlap_units(units: list[str], overlap_tokens: int) -> list[str]:
    selected: list[str] = []
    total = 0
    for unit in reversed(units):
        unit_tokens = estimate_tokens(unit)
        if selected and total + unit_tokens > overlap_tokens:
            break
        selected.append(unit)
        total += unit_tokens
        if total >= overlap_tokens:
            break
    return list(reversed(selected))


def chunk_section(
    section: DocumentSection,
    max_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    units = section_units(section, max_tokens)
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for unit in units:
        unit_tokens = estimate_tokens(unit)
        if current and current_tokens + unit_tokens > max_tokens:
            chunks.append(normalize_text(" ".join(current)))
            current = overlap_units(current, overlap_tokens)
            current_tokens = sum(estimate_tokens(value) for value in current)
            while current and current_tokens + unit_tokens > max_tokens:
                removed = current.pop(0)
                current_tokens -= estimate_tokens(removed)
        current.append(unit)
        current_tokens += unit_tokens

    if current:
        final_text = normalize_text(" ".join(current))
        if final_text and (not chunks or final_text != chunks[-1]):
            chunks.append(final_text)
    return chunks


def build_document_chunks(
    document: ParsedDocument,
    max_tokens: int = 600,
    overlap_tokens: int = 90,
) -> Iterable[dict]:
    chunk_index = 0
    for section in document.sections:
        for content in chunk_section(section, max_tokens, overlap_tokens):
            chunk_index += 1
            yield {
                "chunk_id": f"{document.document_id}_{chunk_index:04d}",
                "document_id": document.document_id,
                "title": document.title,
                "section": section.path,
                "year": document.year,
                "doi": document.doi,
                "pmcid": document.pmcid,
                "source_url": document.source_url,
                "license": document.license,
                "source_file": document.source_file,
                "chunk_index": chunk_index,
                "estimated_tokens": estimate_tokens(content),
                "content": content,
            }
