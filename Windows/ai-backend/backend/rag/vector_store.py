from __future__ import annotations

import os
import re
import time
from typing import Any

import psycopg
from pgvector.psycopg import register_vector

from .embedding_service import encode_texts


def _database_url() -> str:
    value = os.environ.get("PPGL_RAG_DATABASE_URL", "").strip()
    if not value:
        raise FileNotFoundError("尚未配置 PostgreSQL/pgvector 知识库")
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def _enabled() -> bool:
    return os.environ.get("PPGL_RAG_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _connection():
    if not _enabled():
        raise FileNotFoundError("RAG 当前未启用")
    connection = psycopg.connect(_database_url(), connect_timeout=5)
    register_vector(connection)
    return connection


def _tokens(text: str) -> set[str]:
    latin = re.findall(r"[A-Za-z0-9_-]{2,}", text.casefold())
    chinese = re.findall(r"[\u4e00-\u9fff]{2,6}", text)
    return set(latin + chinese)


def _row_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": str(row["chunk_id"]), "document_id": str(row["document_id"]),
        "title": row["title"], "organization": row["organization"], "version": row["version"],
        "source_url": row["source_url"], "section": row["section_title"] or "",
        "page_start": row["page_start"], "page_end": row["page_end"],
        "chunk_index": row["chunk_index"], "content": row["content"],
        "dense_score": float(row.get("dense_score") or 0.0),
    }


def dense_search(query: str, top_k: int = 30) -> list[dict[str, Any]]:
    clean = query.strip()
    if not clean:
        raise ValueError("查询不能为空")
    vector = encode_texts([clean], batch_size=1)[0]
    sql = """
        SELECT c.id AS chunk_id, c.document_id, c.section_title, c.page_start,
               c.page_end, c.chunk_index, c.content, d.title, d.organization,
               d.version, d.source_url, 1 - (c.embedding <=> %(embedding)s) AS dense_score
        FROM rag.knowledge_chunks c
        JOIN rag.knowledge_documents d ON d.id = c.document_id
        WHERE d.status = 'ACTIVE' AND d.deleted_at IS NULL
          AND c.enabled = TRUE AND c.embedding IS NOT NULL
        ORDER BY c.embedding <=> %(embedding)s LIMIT %(limit)s
    """
    with _connection() as connection, connection.cursor(row_factory=psycopg.rows.dict_row) as cursor:
        cursor.execute(sql, {"embedding": vector, "limit": top_k})
        return [_row_payload(row) for row in cursor.fetchall()]


def _keyword_search(query: str, top_k: int) -> list[dict[str, Any]]:
    sql = """
        SELECT c.id AS chunk_id, c.document_id, c.section_title, c.page_start,
               c.page_end, c.chunk_index, c.content, d.title, d.organization,
               d.version, d.source_url,
               ts_rank_cd(to_tsvector('simple', c.content), plainto_tsquery('simple', %(query)s)) AS keyword_score
        FROM rag.knowledge_chunks c
        JOIN rag.knowledge_documents d ON d.id = c.document_id
        WHERE d.status = 'ACTIVE' AND d.deleted_at IS NULL AND c.enabled = TRUE
          AND to_tsvector('simple', c.content) @@ plainto_tsquery('simple', %(query)s)
        ORDER BY keyword_score DESC LIMIT %(limit)s
    """
    with _connection() as connection, connection.cursor(row_factory=psycopg.rows.dict_row) as cursor:
        cursor.execute(sql, {"query": query, "limit": top_k})
        rows = []
        for row in cursor.fetchall():
            item = _row_payload(row)
            item["bm25_score"] = float(row.get("keyword_score") or 0.0)
            rows.append(item)
        return rows


def _hybrid(query: str, retrieve_k: int, rerank: bool) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for rank, item in enumerate(dense_search(query, retrieve_k), start=1):
        value = dict(item)
        value["rrf_score"] = 1.0 / (60 + rank)
        merged[value["chunk_id"]] = value
    for rank, item in enumerate(_keyword_search(query, retrieve_k), start=1):
        value = merged.setdefault(item["chunk_id"], dict(item))
        value["bm25_score"] = item.get("bm25_score", 0.0)
        value["rrf_score"] = value.get("rrf_score", 0.0) + 1.0 / (60 + rank)
    query_tokens = _tokens(query)
    for item in merged.values():
        overlap = len(query_tokens & _tokens(item.get("content", ""))) / max(1, len(query_tokens))
        item["reranker_score"] = overlap if rerank else None
        item["score"] = (0.75 * overlap + 0.25 * item.get("rrf_score", 0.0)) if rerank else item.get("rrf_score", 0.0)
    return sorted(merged.values(), key=lambda item: item.get("score", 0.0), reverse=True)


def search_knowledge_with_metadata(
    query: str, top_k: int = 5, *, retrieve_k: int = 30, retrieval_mode: str = "hybrid_rerank",
) -> dict[str, Any]:
    mode = retrieval_mode.strip().lower()
    if mode not in {"dense", "hybrid", "hybrid_rerank"}:
        raise ValueError("retrieval_mode 必须是 dense、hybrid 或 hybrid_rerank")
    started = time.perf_counter()
    if mode == "dense":
        candidates = dense_search(query, max(top_k, retrieve_k))
        for item in candidates:
            item["score"] = item.get("dense_score", 0.0)
    else:
        candidates = _hybrid(query, max(top_k, retrieve_k), mode == "hybrid_rerank")
    results = candidates[:top_k]
    elapsed = round((time.perf_counter() - started) * 1000, 2)
    return {"results": results, "metadata": {
        "retrieval_mode": mode,
        "pipeline": "pgvector + hybrid + rerank" if mode != "dense" else "pgvector",
        "dense_candidates": len(candidates), "bm25_candidates": len(candidates) if mode != "dense" else 0,
        "fused_candidates": len(candidates), "reranked_candidates": len(candidates) if mode == "hybrid_rerank" else 0,
        "latency_ms": {"dense": elapsed, "bm25": 0.0, "reranker": 0.0, "total": elapsed},
        "approved_documents_only": True,
    }}


def search_knowledge(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    return search_knowledge_with_metadata(query, top_k)["results"]
