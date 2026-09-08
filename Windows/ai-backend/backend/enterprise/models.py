from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UserRole(str, Enum):
    ADMIN = "admin"
    DOCTOR = "doctor"
    VIEWER = "viewer"


class CasePermission(str, Enum):
    READ = "read"
    EDIT = "edit"
    MANAGE = "manage"


class CaseType(str, Enum):
    BRAIN_MRI = "brain_mri"
    PPGL_CT = "ppgl_ct"


class Base(DeclarativeBase):
    pass


class SchemaMigrationRecord(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class UserRecord(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    organization_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class SessionRecord(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class UnifiedCaseRecord(Base):
    __tablename__ = "unified_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    case_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, default="未命名病例")
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    organization_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    workflow_status: Mapped[str] = mapped_column(String(24), nullable=False, default="created", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    retention_profile: Mapped[str] = mapped_column(String(32), nullable=False, default="unclassified")
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    legal_hold_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class CaseGrantRecord(Base):
    __tablename__ = "case_grants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("unified_cases.case_id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    permission: Mapped[str] = mapped_column(String(16), nullable=False)
    granted_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (UniqueConstraint("case_id", "user_id", name="uq_case_grant"),)


class InferenceTaskRecord(Base):
    __tablename__ = "inference_tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("unified_cases.case_id", ondelete="CASCADE"), nullable=False, index=True)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class ResultAssetRecord(Base):
    __tablename__ = "result_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id: Mapped[str] = mapped_column(String(64), ForeignKey("unified_cases.case_id", ondelete="CASCADE"), nullable=False, index=True)
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (UniqueConstraint("case_id", "asset_type", "relative_path", name="uq_result_asset"),)


class DirectMessageRecord(Base):
    __tablename__ = "direct_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    sender_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    recipient_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    __table_args__ = (Index("ix_direct_message_pair", "sender_id", "recipient_id", "created_at"),)


class DeletionRecord(Base):
    __tablename__ = "case_deletions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    reason_code: Mapped[str] = mapped_column(String(48), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class AuditRecord(Base):
    __tablename__ = "audit_logs"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
