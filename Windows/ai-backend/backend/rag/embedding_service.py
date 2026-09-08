from __future__ import annotations

import json
import os
from typing import Optional, Sequence
import urllib.error
import urllib.request

import numpy as np


DEFAULT_MODEL_ID = "qwen3-embedding:0.6b"


def embedding_model_reference(explicit: Optional[str] = None) -> str:
    return (explicit or os.environ.get("PPGL_RAG_EMBEDDING_MODEL", DEFAULT_MODEL_ID)).strip() or DEFAULT_MODEL_ID


def _ollama_base_url() -> str:
    value = os.environ.get("PPGL_OLLAMA_BASE_URL", os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    return value.rstrip("/").removesuffix("/v1")


def _normalize(values: Sequence[float]) -> list[float]:
    vector = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm <= 0:
        raise RuntimeError("Ollama 返回了无效的 Embedding 向量")
    return (vector / norm).tolist()


def encode_texts(
    texts: Sequence[str],
    *,
    model_reference: Optional[str] = None,
    device: Optional[str] = None,
    batch_size: int = 8,
    show_progress_bar: bool = False,
) -> np.ndarray:
    del device, show_progress_bar
    clean = [str(item).strip() for item in texts]
    if not clean:
        return np.empty((0, 0), dtype=np.float32)
    if any(not item for item in clean):
        raise ValueError("Embedding 输入不能包含空文本")
    model = embedding_model_reference(model_reference)
    timeout = float(os.environ.get("PPGL_RAG_REQUEST_TIMEOUT_SECONDS", "60"))
    all_vectors: list[list[float]] = []
    for start in range(0, len(clean), max(1, batch_size)):
        batch = clean[start:start + batch_size]
        request = urllib.request.Request(
            _ollama_base_url() + "/api/embed",
            data=json.dumps({"model": model, "input": batch, "truncate": False}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("无法从本地 Ollama 生成知识库向量") from exc
        vectors = payload.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(batch):
            raise RuntimeError("Ollama Embedding 响应格式无效")
        all_vectors.extend(_normalize(item) for item in vectors)
    dimensions = {len(item) for item in all_vectors}
    expected = int(os.environ.get("PPGL_RAG_EMBEDDING_DIMENSION", "1024"))
    if dimensions != {expected}:
        raise RuntimeError(f"Embedding 维度不匹配：期望 {expected}，实际 {sorted(dimensions)}")
    return np.asarray(all_vectors, dtype=np.float32)


def chunk_embedding_text(chunk: dict) -> str:
    return (
        f"标题：{chunk.get('title', '')}\n"
        f"章节：{chunk.get('section', '')}\n"
        f"正文：{chunk.get('content', '')}"
    )
