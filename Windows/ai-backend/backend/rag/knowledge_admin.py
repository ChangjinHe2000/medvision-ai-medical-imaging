from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid
from typing import Any

import fitz
import psycopg
from psycopg.rows import dict_row
from docx import Document
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool

from backend.enterprise.models import UserRole
from backend.enterprise.privacy import contains_sensitive_text
from backend.enterprise.service import AuthenticationRequired, EnterpriseService

from .embedding_service import encode_texts, embedding_model_reference


ALLOWED_SUFFIXES = {".pdf", ".docx", ".txt", ".md", ".html", ".htm"}
ALLOWED_LICENSES = {"PUBLIC_DOMAIN", "OPEN_ACCESS", "PERMISSION_GRANTED", "INSTITUTION_AUTHORIZED"}


def _database_url() -> str:
    value = os.environ.get("PPGL_RAG_DATABASE_URL", "").strip()
    if not value:
        raise HTTPException(status_code=503, detail="尚未配置 PostgreSQL/pgvector 知识库")
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def _connection():
    return psycopg.connect(_database_url(), connect_timeout=5, row_factory=dict_row)


def _indexing_enabled() -> bool:
    return os.environ.get("PPGL_RAG_INDEXING_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _require_indexing_enabled() -> None:
    if not _indexing_enabled():
        raise HTTPException(status_code=503, detail="RAG 文档解析与向量化功能当前未启用")


def _knowledge_root() -> Path:
    value = os.environ.get("PPGL_RAG_KNOWLEDGE_DIR", "").strip()
    if not value:
        data = Path(os.environ.get("PPGL_DATA_ROOT", str(Path.home() / "ppgl-assist-data")))
        value = str(data / "knowledge-base")
    root = Path(value).expanduser().resolve()
    (root / "source").mkdir(parents=True, exist_ok=True)
    return root


def _admin(request: Request, service: EnterpriseService):
    user = _authenticated(request, service)
    service.require_role(user, UserRole.ADMIN)
    return user


def _authenticated(request: Request, service: EnterpriseService):
    del service
    context = getattr(request.state, "enterprise_auth", None)
    if context is None:
        raise AuthenticationRequired("请先登录")
    return context.user


def _document_path(file_key: str) -> Path:
    root = (_knowledge_root() / "source").resolve()
    path = (root / file_key).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="知识文档路径无效") from exc
    return path


def _extract(path: Path) -> list[tuple[int | None, str]]:
    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        with fitz.open(path) as document:
            return [(index + 1, page.get_text("text")) for index, page in enumerate(document)]
    if suffix == ".docx":
        document = Document(path)
        return [(None, "\n".join(paragraph.text for paragraph in document.paragraphs))]
    text = path.read_text(encoding="utf-8", errors="strict")
    if suffix in {".html", ".htm"}:
        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    return [(None, text)]


def _clean(text: str) -> str:
    value = text.replace("\x00", " ").replace("\r\n", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _chunks(pages: list[tuple[int | None, str]]) -> list[dict[str, Any]]:
    size = max(300, int(os.environ.get("PPGL_RAG_CHUNK_SIZE", "700")))
    overlap = min(size // 3, max(0, int(os.environ.get("PPGL_RAG_CHUNK_OVERLAP", "100"))))
    result: list[dict[str, Any]] = []
    for page, raw in pages:
        text = _clean(raw)
        if not text:
            continue
        start = 0
        while start < len(text):
            end = min(len(text), start + size)
            if end < len(text):
                boundary = max(text.rfind("。", start, end), text.rfind("\n", start, end))
                if boundary > start + size // 2:
                    end = boundary + 1
            content = text[start:end].strip()
            if content:
                result.append({"content": content, "page_start": page, "page_end": page})
            if end >= len(text):
                break
            start = max(start + 1, end - overlap)
    return result


async def _store_document(
    *,
    user,
    service: EnterpriseService,
    file: UploadFile,
    title: str,
    organization: str,
    document_type: str,
    specialty: str,
    version: str,
    source_url: str,
    license_status: str,
) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.casefold()
    license_value = license_status.strip().upper()
    if suffix not in ALLOWED_SUFFIXES or license_value not in ALLOWED_LICENSES:
        raise HTTPException(status_code=422, detail="仅允许公开授权的 PDF、DOCX、TXT、MD 或 HTML 文档")
    required = {
        "标题": title, "发布机构": organization, "文档类型": document_type,
        "专业": specialty, "版本": version, "来源地址": source_url,
    }
    missing = [label for label, value in required.items() if not value.strip()]
    if missing:
        raise HTTPException(status_code=422, detail=f"请填写：{'、'.join(missing)}")
    metadata_text = "\n".join(str(value) for value in required.values())
    if contains_sensitive_text(metadata_text):
        raise HTTPException(status_code=422, detail="文档信息不能包含患者身份信息、病例号或本机路径")
    parsed_source = urlsplit(source_url.strip())
    if parsed_source.scheme not in {"http", "https"} or not parsed_source.hostname:
        raise HTTPException(status_code=422, detail="来源地址必须是有效的 HTTP 或 HTTPS 公开地址")
    maximum = int(os.environ.get("PPGL_RAG_MAX_DOCUMENT_MB", "100")) * 1024 * 1024
    data = await file.read(maximum + 1)
    await file.close()
    if not data or len(data) > maximum:
        raise HTTPException(status_code=413, detail="知识文档为空或超过大小限制")
    document_id = str(uuid.uuid4())
    file_key = f"{document_id}{suffix}"
    target = _document_path(file_key)
    target.write_bytes(data)
    checksum = hashlib.sha256(data).hexdigest()
    try:
        with _connection() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO rag.knowledge_documents
                (id,title,organization,document_type,specialty,version,source_url,license_status,
                 internal_file_key,media_type,file_size_bytes,checksum,status,uploaded_by,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'UPLOADED',%s,now(),now())""",
                (document_id,title.strip(),organization.strip(),document_type.strip().upper(),specialty.strip().upper(),
                 version.strip(),source_url.strip(),license_value,file_key,file.content_type or "application/octet-stream",
                 len(data),checksum,user.id))
    except psycopg.errors.UniqueViolation as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=409, detail="该知识文档已经提交过") from exc
    except Exception:
        target.unlink(missing_ok=True)
        raise
    service.record_audit(
        user.id, "knowledge.document.submit", "success", "knowledge_document", document_id,
        {"size_bytes": len(data), "document_type": document_type.strip().upper()},
    )
    return {
        "id": document_id,
        "status": "UPLOADED",
        "title": title.strip(),
        "message": "文档已提交，等待管理员解析和审核",
    }


def build_knowledge_submission_router(service: EnterpriseService) -> APIRouter:
    """Authenticated submission boundary; review and indexing stay admin-only."""
    router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge-submission-v1"])

    @router.post("/documents", status_code=status.HTTP_201_CREATED)
    async def submit_document(
        request: Request, file: UploadFile = File(...), title: str = Form(...),
        organization: str = Form(...), document_type: str = Form(...), specialty: str = Form(...),
        version: str = Form(...), source_url: str = Form(...), license_status: str = Form(...),
    ) -> dict[str, Any]:
        user = _authenticated(request, service)
        return await _store_document(
            user=user, service=service, file=file, title=title, organization=organization,
            document_type=document_type, specialty=specialty, version=version,
            source_url=source_url, license_status=license_status,
        )

    return router


def build_knowledge_admin_router(service: EnterpriseService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin/knowledge", tags=["knowledge-admin-v1"])

    @router.get("/status")
    def knowledge_status(request: Request) -> dict[str, Any]:
        _admin(request, service)
        try:
            with _connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
                vector = cursor.fetchone()
                cursor.execute("SELECT status, count(*) AS count FROM rag.knowledge_documents WHERE deleted_at IS NULL GROUP BY status")
                counts = {row["status"]: row["count"] for row in cursor.fetchall()}
        except Exception as exc:
            raise HTTPException(status_code=503, detail="PostgreSQL/pgvector 知识库不可用") from exc
        return {"ready": bool(vector), "pgvector": vector["extversion"] if vector else None, "documents": counts}

    @router.get("/documents")
    def list_documents(request: Request, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        _admin(request, service)
        limit, offset = min(max(limit, 1), 200), max(offset, 0)
        with _connection() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT id,title,organization,document_type,specialty,version,source_url,
                license_status,status,file_size_bytes,uploaded_by,reviewed_by,reviewed_at,created_at
                FROM rag.knowledge_documents WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT %s OFFSET %s""", (limit, offset))
            documents = cursor.fetchall()
            cursor.execute("SELECT count(*) AS count FROM rag.knowledge_documents WHERE deleted_at IS NULL")
            total = cursor.fetchone()["count"]
        users = {item["id"]: item for item in service.list_users()}
        for document in documents:
            submitter = users.get(document.get("uploaded_by"))
            document["submitter"] = (
                {"username": submitter["username"], "display_name": submitter["display_name"]}
                if submitter else None
            )
        return {"documents": documents, "total": total}

    @router.post("/documents", status_code=status.HTTP_201_CREATED)
    async def upload_document(
        request: Request, file: UploadFile = File(...), title: str = Form(...),
        organization: str = Form(...), document_type: str = Form(...), specialty: str = Form(...),
        version: str = Form(...), source_url: str = Form(...), license_status: str = Form(...),
    ) -> dict[str, Any]:
        user = _admin(request, service)
        return await _store_document(
            user=user, service=service, file=file, title=title, organization=organization,
            document_type=document_type, specialty=specialty, version=version,
            source_url=source_url, license_status=license_status,
        )

    @router.post("/documents/{document_id}/parse")
    async def parse_document(document_id: str, request: Request) -> dict[str, Any]:
        user = _admin(request, service)
        _require_indexing_enabled()
        def operation():
            with _connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT internal_file_key,status FROM rag.knowledge_documents WHERE id=%s AND deleted_at IS NULL", (document_id,))
                document = cursor.fetchone()
                if not document:
                    raise HTTPException(status_code=404, detail="知识文档不存在")
                chunks = _chunks(_extract(_document_path(document["internal_file_key"])))
                if not chunks:
                    raise HTTPException(status_code=422, detail="文档中没有可用文本")
                if any(contains_sensitive_text(item["content"]) for item in chunks):
                    raise HTTPException(status_code=422, detail="公共知识文档包含患者身份信息或本机路径")
                cursor.execute("DELETE FROM rag.knowledge_chunks WHERE document_id=%s", (document_id,))
                for index, item in enumerate(chunks):
                    cursor.execute("""INSERT INTO rag.knowledge_chunks
                        (id,document_id,section_title,page_start,page_end,chunk_index,content,content_checksum,enabled,created_at,updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,FALSE,now(),now())""",
                        (str(uuid.uuid4()),document_id,"正文",item["page_start"],item["page_end"],index,item["content"],
                         hashlib.sha256(item["content"].encode("utf-8")).hexdigest()))
                cursor.execute("UPDATE rag.knowledge_documents SET status='PENDING_REVIEW',updated_at=now() WHERE id=%s", (document_id,))
            return len(chunks)
        count = await run_in_threadpool(operation)
        service.record_audit(user.id, "knowledge.document.parse", "success", "knowledge_document", document_id, {"chunks": count})
        return {"id": document_id, "status": "PENDING_REVIEW", "chunks": count}

    @router.post("/documents/{document_id}/index")
    async def index_document(document_id: str, request: Request) -> dict[str, Any]:
        user = _admin(request, service)
        _require_indexing_enabled()
        def operation():
            with _connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT id,content FROM rag.knowledge_chunks WHERE document_id=%s ORDER BY chunk_index", (document_id,))
                chunks = cursor.fetchall()
                if not chunks:
                    raise HTTPException(status_code=409, detail="请先解析并切片文档")
                batch_size = max(1, int(os.environ.get("PPGL_RAG_EMBEDDING_BATCH_SIZE", "8")))
                for start in range(0, len(chunks), batch_size):
                    batch = chunks[start:start + batch_size]
                    vectors = encode_texts([item["content"] for item in batch], batch_size=batch_size)
                    for item, vector in zip(batch, vectors):
                        cursor.execute("""UPDATE rag.knowledge_chunks SET embedding=%s,embedding_model=%s,
                            embedding_dimension=%s,updated_at=now() WHERE id=%s""",
                            (vector,embedding_model_reference(),len(vector),item["id"]))
                cursor.execute("UPDATE rag.knowledge_documents SET status='PENDING_REVIEW',updated_at=now() WHERE id=%s", (document_id,))
            return len(chunks)
        count = await run_in_threadpool(operation)
        service.record_audit(user.id, "knowledge.document.index", "success", "knowledge_document", document_id, {"chunks": count})
        return {"id": document_id, "status": "PENDING_REVIEW", "embedded_chunks": count}

    def set_status(document_id: str, request: Request, target: str) -> dict[str, Any]:
        user = _admin(request, service)
        with _connection() as connection, connection.cursor() as cursor:
            if target == "ACTIVE":
                cursor.execute("SELECT count(*) AS count FROM rag.knowledge_chunks WHERE document_id=%s AND embedding IS NOT NULL", (document_id,))
                if cursor.fetchone()["count"] == 0:
                    raise HTTPException(status_code=409, detail="文档尚未完成向量化")
                cursor.execute("UPDATE rag.knowledge_chunks SET enabled=TRUE,updated_at=now() WHERE document_id=%s", (document_id,))
                cursor.execute("UPDATE rag.knowledge_documents SET status='ACTIVE',reviewed_by=%s,reviewed_at=now(),updated_at=now() WHERE id=%s AND deleted_at IS NULL", (user.id,document_id))
            else:
                cursor.execute("UPDATE rag.knowledge_chunks SET enabled=FALSE,updated_at=now() WHERE document_id=%s", (document_id,))
                cursor.execute("UPDATE rag.knowledge_documents SET status='DISABLED',updated_at=now() WHERE id=%s AND deleted_at IS NULL", (document_id,))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="知识文档不存在")
        action = "enable" if target == "ACTIVE" else "disable"
        service.record_audit(user.id, f"knowledge.document.{action}", "success", "knowledge_document", document_id)
        return {"id": document_id, "status": target}

    @router.post("/documents/{document_id}/enable")
    def enable_document(document_id: str, request: Request):
        return set_status(document_id, request, "ACTIVE")

    @router.post("/documents/{document_id}/disable")
    def disable_document(document_id: str, request: Request):
        return set_status(document_id, request, "DISABLED")

    @router.delete("/documents/{document_id}")
    def delete_document(document_id: str, request: Request) -> dict[str, Any]:
        user = _admin(request, service)
        with _connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE rag.knowledge_chunks SET enabled=FALSE WHERE document_id=%s", (document_id,))
            cursor.execute("UPDATE rag.knowledge_documents SET status='DELETED',deleted_at=now(),updated_at=now() WHERE id=%s AND deleted_at IS NULL", (document_id,))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="知识文档不存在")
        service.record_audit(user.id, "knowledge.document.delete", "success", "knowledge_document", document_id)
        return {"id": document_id, "status": "DELETED"}

    return router
