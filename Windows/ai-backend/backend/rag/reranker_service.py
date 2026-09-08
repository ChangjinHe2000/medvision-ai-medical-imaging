from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from sentence_transformers import CrossEncoder


AI_BACKEND_DIR = Path(__file__).resolve().parents[2]
LOCAL_MODEL_DIR = AI_BACKEND_DIR / "models" / "bge-reranker-v2-m3"
DEFAULT_MODEL_ID = "BAAI/bge-reranker-v2-m3"


def reranker_model_reference(explicit: Optional[str] = None) -> str:
    configured = (explicit or os.environ.get("PPGL_RERANKER_MODEL", "")).strip()
    if configured:
        return configured
    if LOCAL_MODEL_DIR.exists():
        return str(LOCAL_MODEL_DIR.resolve())
    return DEFAULT_MODEL_ID


def reranker_device(explicit: Optional[str] = None) -> str:
    return (explicit or os.environ.get("PPGL_RERANKER_DEVICE", "cpu")).strip() or "cpu"


@lru_cache(maxsize=2)
def load_reranker(model_reference: str, device: str) -> CrossEncoder:
    return CrossEncoder(model_reference, device=device, max_length=512)


def rerank_results(
    query: str,
    results: list[dict],
    *,
    top_k: int,
    model_reference: Optional[str] = None,
    device: Optional[str] = None,
    batch_size: int = 8,
) -> list[dict]:
    if not results:
        return []
    resolved_model = reranker_model_reference(model_reference)
    resolved_device = reranker_device(device)
    model = load_reranker(resolved_model, resolved_device)
    pairs = [
        (
            query,
            f"Title: {item.get('title', '')}\n"
            f"Section: {item.get('section', '')}\n"
            f"Content: {item.get('content', '')}",
        )
        for item in results
    ]
    scores = model.predict(
        pairs,
        batch_size=batch_size,
        show_progress_bar=False,
    )
    ranked = []
    for item, raw_score in zip(results, scores):
        enriched = dict(item)
        enriched["reranker_score"] = float(raw_score)
        enriched["score"] = float(raw_score)
        ranked.append(enriched)
    ranked.sort(key=lambda item: item["reranker_score"], reverse=True)
    return ranked[:top_k]
