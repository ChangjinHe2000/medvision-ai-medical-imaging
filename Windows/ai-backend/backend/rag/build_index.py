from __future__ import annotations

import argparse
import json
from pathlib import Path

from .vector_store import DEFAULT_CHUNKS_PATH, DEFAULT_INDEX_PATH, build_vector_index


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the local Qdrant PPGL knowledge index")
    parser.add_argument("--chunks", type=Path, default=DEFAULT_CHUNKS_PATH)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX_PATH)
    parser.add_argument("--model", default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--batch-size", type=int, default=8)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.batch_size < 1:
        raise ValueError("--batch-size must be at least 1")
    result = build_vector_index(
        chunks_path=args.chunks,
        index_path=args.index,
        model_reference=args.model,
        device=args.device,
        batch_size=args.batch_size,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
