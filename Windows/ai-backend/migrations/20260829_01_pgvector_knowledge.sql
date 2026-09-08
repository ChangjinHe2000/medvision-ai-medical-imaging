BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS rag;

CREATE TABLE IF NOT EXISTS rag.knowledge_documents (
    id varchar(36) PRIMARY KEY,
    title varchar(500) NOT NULL,
    organization varchar(255) NOT NULL,
    document_type varchar(64) NOT NULL,
    specialty varchar(64) NOT NULL,
    version varchar(128) NOT NULL,
    published_at date,
    effective_at date,
    source_url text NOT NULL,
    license_status varchar(64) NOT NULL,
    internal_file_key varchar(255) NOT NULL UNIQUE,
    media_type varchar(127) NOT NULL,
    file_size_bytes bigint NOT NULL CHECK (file_size_bytes >= 0),
    checksum varchar(64) NOT NULL UNIQUE,
    status varchar(32) NOT NULL,
    uploaded_by varchar(36) NOT NULL,
    reviewed_by varchar(36),
    reviewed_at timestamptz,
    failure_code varchar(64),
    failure_message text,
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_knowledge_documents_status CHECK
      (status IN ('UPLOADED','PARSING','CHUNKING','EMBEDDING','PENDING_REVIEW','ACTIVE','DISABLED','FAILED','DELETED'))
);

CREATE TABLE IF NOT EXISTS rag.knowledge_chunks (
    id varchar(36) PRIMARY KEY,
    document_id varchar(36) NOT NULL REFERENCES rag.knowledge_documents(id) ON DELETE CASCADE,
    section_title varchar(500),
    page_start integer,
    page_end integer,
    chunk_index integer NOT NULL CHECK (chunk_index >= 0),
    content text NOT NULL,
    content_checksum varchar(64) NOT NULL,
    embedding vector(1024),
    embedding_model varchar(128),
    embedding_dimension integer,
    enabled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index),
    UNIQUE (document_id, content_checksum),
    CONSTRAINT ck_knowledge_chunks_page CHECK
      (page_start IS NULL OR page_end IS NULL OR (page_start >= 1 AND page_end >= page_start)),
    CONSTRAINT ck_knowledge_chunks_embedding CHECK
      (embedding_dimension IS NULL OR embedding_dimension = 1024)
);

CREATE INDEX IF NOT EXISTS ix_knowledge_documents_status
    ON rag.knowledge_documents(status) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_document
    ON rag.knowledge_chunks(document_id, enabled);
CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_text
    ON rag.knowledge_chunks USING gin(to_tsvector('simple', content));
CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_embedding
    ON rag.knowledge_chunks USING hnsw (embedding vector_cosine_ops)
    WHERE enabled = true AND embedding IS NOT NULL;

COMMIT;
