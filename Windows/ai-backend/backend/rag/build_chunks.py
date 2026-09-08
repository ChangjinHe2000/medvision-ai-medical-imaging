from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

from .chunker import build_document_chunks
from .jats_parser import parse_document


AI_BACKEND_DIR = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("PPGL_DATA_ROOT", str(Path.home() / "ppgl-assist-data"))).expanduser().resolve()
DEFAULT_DOCUMENTS_DIR = Path(
    os.environ.get("PPGL_RAG_DOCUMENTS_DIR", str(DATA_ROOT / "rag-documents"))
).expanduser().resolve()
DEFAULT_OUTPUT_PATH = Path(
    os.environ.get("PPGL_RAG_CHUNKS_PATH", str(DATA_ROOT / "rag-parsed" / "chunks.jsonl"))
).expanduser().resolve()
SUPPORTED_SUFFIXES = {".xml", ".md", ".markdown"}


def build_chunks(
    documents_dir: Path,
    output_path: Path,
    max_tokens: int,
    overlap_tokens: int,
) -> tuple[int, int, Counter[str]]:
    document_paths = sorted(
        path
        for path in documents_dir.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )
    if not document_paths:
        raise FileNotFoundError(f"No supported documents found in {documents_dir}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[dict] = []
    per_document: Counter[str] = Counter()
    for path in document_paths:
        document = parse_document(path)
        document_chunks = list(
            build_document_chunks(
                document,
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
            )
        )
        if not document_chunks:
            raise ValueError(f"No chunks generated for {path.name}")
        chunks.extend(document_chunks)
        per_document[document.document_id] += len(document_chunks)

    with output_path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    return len(document_paths), len(chunks), per_document


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse and chunk PPGL RAG knowledge documents")
    parser.add_argument("--documents", type=Path, default=DEFAULT_DOCUMENTS_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--max-tokens", type=int, default=600)
    parser.add_argument("--overlap-tokens", type=int, default=90)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.max_tokens < 100:
        raise ValueError("--max-tokens must be at least 100")
    if args.overlap_tokens < 0 or args.overlap_tokens >= args.max_tokens:
        raise ValueError("--overlap-tokens must be non-negative and smaller than --max-tokens")

    document_count, chunk_count, per_document = build_chunks(
        documents_dir=args.documents.resolve(),
        output_path=args.output.resolve(),
        max_tokens=args.max_tokens,
        overlap_tokens=args.overlap_tokens,
    )
    print(f"documents={document_count}")
    print(f"chunks={chunk_count}")
    print(f"output={args.output.resolve()}")
    for document_id, count in sorted(per_document.items()):
        print(f"{document_id}: {count}")


if __name__ == "__main__":
    main()
