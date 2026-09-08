from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path

from .vector_store import search_knowledge_with_metadata


AI_BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_QUESTIONS = AI_BACKEND_DIR / "knowledge_base" / "evaluation" / "questions.json"
DEFAULT_OUTPUT = AI_BACKEND_DIR / "knowledge_base" / "evaluation" / "results.json"


def relevant_target_index(result: dict, targets: list[dict], excluded: set[int] | None = None) -> int | None:
    excluded = excluded or set()
    for index, target in enumerate(targets):
        if index in excluded:
            continue
        if result.get("document_id") != target.get("document_id"):
            continue
        section_contains = str(target.get("section_contains", "")).casefold()
        if not section_contains or section_contains in str(result.get("section", "")).casefold():
            return index
    return None


def query_metrics(results: list[dict], targets: list[dict]) -> dict:
    matched_targets: set[int] = set()
    relevance = []
    for result in results:
        target_index = relevant_target_index(result, targets, matched_targets)
        relevance.append(target_index is not None)
        if target_index is not None:
            matched_targets.add(target_index)
    first_rank = next((index for index, value in enumerate(relevance, start=1) if value), None)
    dcg5 = sum((1 / math.log2(index + 1)) for index, value in enumerate(relevance[:5], start=1) if value)
    ideal_relevant = min(5, len(targets))
    idcg5 = sum(1 / math.log2(index + 1) for index in range(1, ideal_relevant + 1))
    return {
        "recall_at_1": float(any(relevance[:1])),
        "recall_at_5": float(any(relevance[:5])),
        "recall_at_10": float(any(relevance[:10])),
        "reciprocal_rank": 1 / first_rank if first_rank else 0.0,
        "ndcg_at_5": dcg5 / idcg5 if idcg5 else 0.0,
        "first_relevant_rank": first_rank,
    }


def retrieve(mode: str, question: str, top_k: int, retrieve_k: int) -> list[dict]:
    payload = search_knowledge_with_metadata(
        question,
        top_k,
        retrieve_k=retrieve_k,
        retrieval_mode=mode,
    )
    return payload["results"]


def evaluate_mode(mode: str, questions: list[dict], top_k: int, retrieve_k: int) -> dict:
    rows = []
    for item in questions:
        started = time.perf_counter()
        results = retrieve(mode, item["question"], top_k, retrieve_k)
        latency_ms = (time.perf_counter() - started) * 1000
        metrics = query_metrics(results, item["relevant"])
        rows.append(
            {
                "id": item["id"],
                "question": item["question"],
                "latency_ms": round(latency_ms, 2),
                **metrics,
                "top_results": [
                    {
                        "chunk_id": result.get("chunk_id"),
                        "document_id": result.get("document_id"),
                        "section": result.get("section"),
                        "score": result.get("score"),
                    }
                    for result in results[:5]
                ],
            }
        )

    metric_names = ["recall_at_1", "recall_at_5", "recall_at_10", "reciprocal_rank", "ndcg_at_5"]
    summary = {
        name: round(statistics.mean(row[name] for row in rows), 4)
        for name in metric_names
    }
    latencies = [row["latency_ms"] for row in rows]
    summary.update(
        {
            "questions": len(rows),
            "mean_latency_ms": round(statistics.mean(latencies), 2),
            "p95_latency_ms": round(sorted(latencies)[max(0, math.ceil(len(latencies) * 0.95) - 1)], 2),
        }
    )
    return {"summary": summary, "questions": rows}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate PPGL RAG retrieval pipelines")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--retrieve-k", type=int, default=20)
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=["dense", "hybrid", "hybrid_rerank"],
        default=["dense", "hybrid", "hybrid_rerank"],
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    payload = {
        "dataset": str(args.questions.resolve()),
        "top_k": args.top_k,
        "retrieve_k": args.retrieve_k,
        "pipelines": {},
    }
    for mode in args.modes:
        print(f"Evaluating {mode}...")
        payload["pipelines"][mode] = evaluate_mode(mode, questions, args.top_k, args.retrieve_k)
        print(json.dumps(payload["pipelines"][mode]["summary"], ensure_ascii=False))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"output={args.output.resolve()}")


if __name__ == "__main__":
    main()
