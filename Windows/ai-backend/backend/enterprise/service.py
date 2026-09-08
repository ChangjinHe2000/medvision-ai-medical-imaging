from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
from threading import RLock
from typing import Any

from pwdlib import PasswordHash
from sqlalchemy import create_engine, event, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    AuditRecord,
    Base,
    CaseGrantRecord,
    CasePermission,
    CaseType,
    DeletionRecord,
    DirectMessageRecord,
    InferenceTaskRecord,
    ResultAssetRecord,
    SchemaMigrationRecord,
    SessionRecord,
    UnifiedCaseRecord,
    UserRecord,
    UserRole,
    utc_now,
)


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
PERMISSION_LEVEL = {"read": 1, "edit": 2, "manage": 3}


class EnterpriseError(RuntimeError):
    code = "ENTERPRISE_ERROR"


class AuthenticationRequired(EnterpriseError):
    code = "AUTHENTICATION_REQUIRED"


class InvalidCredentials(EnterpriseError):
    code = "INVALID_CREDENTIALS"


class AuthorizationDenied(EnterpriseError):
    code = "AUTHORIZATION_DENIED"


class CsrfRejected(EnterpriseError):
    code = "CSRF_VALIDATION_FAILED"


class ValidationRejected(EnterpriseError):
    code = "VALIDATION_FAILED"


class ResourceNotFound(EnterpriseError):
    code = "RESOURCE_NOT_FOUND"


class ResourceConflict(EnterpriseError):
    code = "RESOURCE_CONFLICT"


@dataclass(frozen=True)
class EnterpriseServiceConfig:
    database_url: str
    session_hours: int = 8
    session_cookie_name: str = "medvision_session"
    csrf_cookie_name: str = "medvision_csrf"
    cookie_secure: bool = False


@dataclass(frozen=True)
class AuthContext:
    user: UserRecord
    session: SessionRecord


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class EnterpriseService:
    def __init__(self, config: EnterpriseServiceConfig):
        self.config = config
        connect_args: dict[str, Any] = {}
        if config.database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            raw = config.database_url.removeprefix("sqlite:///")
            if raw and raw != ":memory:":
                Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(config.database_url, connect_args=connect_args, pool_pre_ping=True)
        if config.database_url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._enable_sqlite_foreign_keys)
        self.sessions = sessionmaker(bind=self.engine, class_=Session, expire_on_commit=False)
        self.password_hash = PasswordHash.recommended()
        self._audit_lock = RLock()

    @staticmethod
    def _enable_sqlite_foreign_keys(connection: Any, _: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.sessions() as db:
            if db.get(SchemaMigrationRecord, "0001_enterprise_baseline") is None:
                db.add(SchemaMigrationRecord(
                    version="0001_enterprise_baseline",
                    description="用户、会话、统一病例、授权、任务、资产、私聊、回收站与审计基线",
                ))
                db.commit()

    def close(self) -> None:
        self.engine.dispose()

    @staticmethod
    def serialize_user(user: UserRecord) -> dict[str, Any]:
        return {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "organization_id": user.organization_id,
            "is_active": user.is_active,
            "created_at": _utc(user.created_at).isoformat(),
        }

    def count_users(self) -> int:
        with self.sessions() as db:
            return len(db.scalars(select(UserRecord.id)).all())

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        role: str | UserRole,
        organization_id: str | None = None,
        actor_id: str | None = None,
        initial_setup: bool = False,
    ) -> dict[str, Any]:
        normalized = username.strip().casefold()
        username = username.strip()
        display_name = " ".join(display_name.split())
        if not USERNAME_PATTERN.fullmatch(username):
            raise ValidationRejected("用户名只能包含字母、数字、点、横线和下划线")
        if len(password) < 12:
            raise ValidationRejected("密码至少需要 12 个字符")
        role_value = UserRole(role).value
        with self.sessions() as db:
            if initial_setup:
                if db.scalar(select(UserRecord.id).limit(1)) is not None:
                    raise ResourceConflict("初始管理员已经创建")
                role_value = UserRole.ADMIN.value
            elif actor_id is None:
                raise AuthorizationDenied("创建账号需要管理员身份")
            record = UserRecord(
                username=username,
                normalized_username=normalized,
                display_name=display_name,
                password_hash=self.password_hash.hash(password),
                role=role_value,
                organization_id=organization_id,
            )
            db.add(record)
            try:
                db.flush()
            except IntegrityError as exc:
                raise ResourceConflict("用户名已经存在") from exc
            self._add_audit(db, actor_id or record.id, "auth.user.create", "success", "user", record.id)
            db.commit()
            return self.serialize_user(record)

    def list_users(self) -> list[dict[str, Any]]:
        with self.sessions() as db:
            return [self.serialize_user(item) for item in db.scalars(select(UserRecord).order_by(UserRecord.username)).all()]

    def set_user_active(self, user_id: str, active: bool, actor_id: str) -> dict[str, Any]:
        if user_id == actor_id and not active:
            raise ValidationRejected("不能停用当前登录账号")
        with self.sessions() as db:
            user = db.get(UserRecord, user_id)
            if user is None:
                raise ResourceNotFound("账号不存在")
            user.is_active = active
            if not active:
                for session in db.scalars(select(SessionRecord).where(SessionRecord.user_id == user_id, SessionRecord.revoked_at.is_(None))):
                    session.revoked_at = utc_now()
            self._add_audit(db, actor_id, "auth.user.status", "success", "user", user_id, {"is_active": active})
            db.commit()
            return self.serialize_user(user)

    def login(self, username: str, password: str, *, ip_address: str | None = None) -> tuple[dict[str, Any], str, str, datetime]:
        normalized = username.strip().casefold()
        with self.sessions() as db:
            user = db.scalar(select(UserRecord).where(UserRecord.normalized_username == normalized))
            if user is None or not user.is_active or not self.password_hash.verify(password, user.password_hash):
                self._add_audit(db, user.id if user else None, "auth.login", "failure", "user", None, {"ip": ip_address})
                db.commit()
                raise InvalidCredentials("用户名或密码错误")
            raw_session = secrets.token_urlsafe(48)
            raw_csrf = secrets.token_urlsafe(32)
            expires = utc_now() + timedelta(hours=self.config.session_hours)
            db.add(SessionRecord(user_id=user.id, token_hash=_digest(raw_session), csrf_hash=_digest(raw_csrf), expires_at=expires))
            self._add_audit(db, user.id, "auth.login", "success", "session", None, {"ip": ip_address})
            db.commit()
            return self.serialize_user(user), raw_session, raw_csrf, expires

    def resolve_session(self, raw_token: str | None) -> AuthContext:
        if not raw_token:
            raise AuthenticationRequired("请先登录")
        with self.sessions() as db:
            session = db.scalar(select(SessionRecord).where(SessionRecord.token_hash == _digest(raw_token)))
            if session is None or session.revoked_at is not None or _utc(session.expires_at) <= utc_now():
                raise AuthenticationRequired("登录状态已失效")
            user = db.get(UserRecord, session.user_id)
            if user is None or not user.is_active:
                raise AuthenticationRequired("账号不存在或已停用")
            return AuthContext(user=user, session=session)

    def validate_csrf(self, context: AuthContext, cookie: str | None, header: str | None) -> None:
        if not cookie or not header or not secrets.compare_digest(cookie, header):
            raise CsrfRejected("CSRF 校验失败")
        if not secrets.compare_digest(_digest(cookie), context.session.csrf_hash):
            raise CsrfRejected("CSRF 校验失败")

    def logout(self, raw_token: str | None) -> None:
        if not raw_token:
            return
        with self.sessions() as db:
            record = db.scalar(select(SessionRecord).where(SessionRecord.token_hash == _digest(raw_token)))
            if record and record.revoked_at is None:
                record.revoked_at = utc_now()
                self._add_audit(db, record.user_id, "auth.logout", "success", "session", record.id)
                db.commit()

    @staticmethod
    def require_role(user: UserRecord, *roles: str | UserRole) -> None:
        if user.role not in {UserRole(role).value for role in roles}:
            raise AuthorizationDenied("当前账号没有执行此操作的权限")

    @staticmethod
    def serialize_case(case: UnifiedCaseRecord, permission: str = "manage") -> dict[str, Any]:
        return {
            "id": case.id,
            "case_id": case.case_id,
            "case_type": case.case_type,
            "display_name": case.display_name,
            "owner_id": case.owner_id,
            "organization_id": case.organization_id,
            "workflow_status": case.workflow_status,
            "access_permission": permission,
            "deleted": case.deleted_at is not None,
            "retention_profile": case.retention_profile,
            "retain_until": _utc(case.retain_until).isoformat() if case.retain_until else None,
            "legal_hold": case.legal_hold,
            "legal_hold_reason": case.legal_hold_reason,
            "created_at": _utc(case.created_at).isoformat(),
            "updated_at": _utc(case.updated_at).isoformat(),
        }

    def register_case(self, case_id: str, case_type: str | CaseType, display_name: str, owner: UserRecord) -> dict[str, Any]:
        with self.sessions() as db:
            if db.scalar(select(UnifiedCaseRecord).where(UnifiedCaseRecord.case_id == case_id)):
                raise ResourceConflict("病例已经登记")
            record = UnifiedCaseRecord(
                case_id=case_id,
                case_type=CaseType(case_type).value,
                display_name=display_name.strip() or "未命名病例",
                owner_id=owner.id,
                organization_id=owner.organization_id,
            )
            db.add(record)
            self._add_audit(db, owner.id, "case.create", "success", "case", case_id, {"case_type": record.case_type})
            db.commit()
            return self.serialize_case(record)

    def get_case_record(self, case_id: str) -> UnifiedCaseRecord:
        with self.sessions() as db:
            record = db.scalar(select(UnifiedCaseRecord).where(UnifiedCaseRecord.case_id == case_id))
            if record is None:
                raise ResourceNotFound("病例不存在")
            db.expunge(record)
            return record

    def authorize_case(self, user: UserRecord, case_id: str, required: str | CasePermission = "read") -> None:
        required_value = CasePermission(required).value
        if user.role == UserRole.ADMIN.value:
            return
        if user.role == UserRole.VIEWER.value and required_value != "read":
            raise AuthorizationDenied("只读账号不能修改病例")
        with self.sessions() as db:
            case = db.scalar(select(UnifiedCaseRecord).where(UnifiedCaseRecord.case_id == case_id, UnifiedCaseRecord.deleted_at.is_(None)))
            if case is None:
                raise ResourceNotFound("病例不存在或位于回收站")
            if case.owner_id == user.id:
                return
            grant = db.scalar(select(CaseGrantRecord).where(CaseGrantRecord.case_id == case_id, CaseGrantRecord.user_id == user.id))
            if grant is None or PERMISSION_LEVEL[grant.permission] < PERMISSION_LEVEL[required_value]:
                raise AuthorizationDenied("无权访问该病例")

    def list_cases(self, user: UserRecord, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        with self.sessions() as db:
            query = select(UnifiedCaseRecord)
            if not include_deleted:
                query = query.where(UnifiedCaseRecord.deleted_at.is_(None))
            if user.role != UserRole.ADMIN.value:
                granted = select(CaseGrantRecord.case_id).where(CaseGrantRecord.user_id == user.id)
                query = query.where(or_(UnifiedCaseRecord.owner_id == user.id, UnifiedCaseRecord.case_id.in_(granted)))
            records = db.scalars(query.order_by(UnifiedCaseRecord.created_at.desc())).all()
            grants = {
                item.case_id: item.permission
                for item in db.scalars(select(CaseGrantRecord).where(CaseGrantRecord.user_id == user.id)).all()
            }
            return [self.serialize_case(item, "manage" if item.owner_id == user.id or user.role == "admin" else grants.get(item.case_id, "read")) for item in records]

    def rename_case(self, case_id: str, display_name: str, actor_id: str) -> dict[str, Any]:
        normalized = " ".join(display_name.split())
        if not normalized or len(normalized) > 120:
            raise ValidationRejected("病例显示名长度无效")
        with self.sessions() as db:
            case = db.scalar(select(UnifiedCaseRecord).where(UnifiedCaseRecord.case_id == case_id))
            if case is None:
                raise ResourceNotFound("病例不存在")
            case.display_name = normalized
            case.updated_at = utc_now()
            self._add_audit(db, actor_id, "case.rename", "success", "case", case_id)
            db.commit()
            return self.serialize_case(case)

    def set_case_grant(self, case_id: str, user_id: str, permission: str, actor_id: str) -> dict[str, Any]:
        permission = CasePermission(permission).value
        with self.sessions() as db:
            case = db.scalar(select(UnifiedCaseRecord).where(UnifiedCaseRecord.case_id == case_id))
            user = db.get(UserRecord, user_id)
            if case is None or user is None or not user.is_active:
                raise ResourceNotFound("病例或授权用户不存在")
            if case.owner_id == user_id:
                raise ValidationRejected("病例所有者不需要重复授权")
            grant = db.scalar(select(CaseGrantRecord).where(CaseGrantRecord.case_id == case_id, CaseGrantRecord.user_id == user_id))
            if grant is None:
                grant = CaseGrantRecord(case_id=case_id, user_id=user_id, permission=permission, granted_by=actor_id)
                db.add(grant)
            else:
                grant.permission = permission
                grant.granted_by = actor_id
            self._add_audit(db, actor_id, "case.grant", "success", "case", case_id, {"user_id": user_id, "permission": permission})
            db.commit()
            return {"case_id": case_id, "user_id": user_id, "permission": permission}

    def list_case_grants(self, case_id: str) -> list[dict[str, Any]]:
        with self.sessions() as db:
            rows = db.execute(select(CaseGrantRecord, UserRecord).join(UserRecord, UserRecord.id == CaseGrantRecord.user_id).where(CaseGrantRecord.case_id == case_id)).all()
            return [{"user": self.serialize_user(user), "permission": grant.permission} for grant, user in rows]

    def revoke_case_grant(self, case_id: str, user_id: str, actor_id: str) -> None:
        with self.sessions() as db:
            grant = db.scalar(select(CaseGrantRecord).where(CaseGrantRecord.case_id == case_id, CaseGrantRecord.user_id == user_id))
            if grant:
                db.delete(grant)
            self._add_audit(db, actor_id, "case.grant.revoke", "success", "case", case_id, {"user_id": user_id})
            db.commit()

    def mark_case_trashed(self, case_id: str, actor_id: str, reason_code: str) -> dict[str, Any]:
        with self.sessions() as db:
            case = db.scalar(select(UnifiedCaseRecord).where(UnifiedCaseRecord.case_id == case_id))
            if case is None or case.deleted_at is not None:
                raise ResourceConflict("病例不存在或已经删除")
            if case.legal_hold:
                raise ResourceConflict("病例处于法律保留状态，禁止删除")
            case.deleted_at = utc_now()
            db.add(DeletionRecord(case_id=case_id, state="trashed", reason_code=reason_code, actor_id=actor_id))
            self._add_audit(db, actor_id, "case.trash", "success", "case", case_id, {"reason_code": reason_code})
            db.commit()
            return self.serialize_case(case)

    def mark_case_restored(self, case_id: str, actor_id: str) -> dict[str, Any]:
        with self.sessions() as db:
            case = db.scalar(select(UnifiedCaseRecord).where(UnifiedCaseRecord.case_id == case_id))
            if case is None or case.deleted_at is None:
                raise ResourceConflict("病例不在回收站")
            case.deleted_at = None
            db.add(DeletionRecord(case_id=case_id, state="restored", reason_code="authorized_restore", actor_id=actor_id))
            self._add_audit(db, actor_id, "case.restore", "success", "case", case_id)
            db.commit()
            return self.serialize_case(case)

    def mark_case_purged(self, case_id: str, actor_id: str, reason_code: str) -> None:
        with self.sessions() as db:
            case = db.scalar(select(UnifiedCaseRecord).where(UnifiedCaseRecord.case_id == case_id))
            if case is None or case.deleted_at is None:
                raise ResourceConflict("病例不在回收站")
            if case.legal_hold:
                raise ResourceConflict("病例处于法律保留状态")
            self._add_audit(db, actor_id, "case.purge", "success", "case", case_id, {"reason_code": reason_code})
            db.delete(case)
            db.commit()

    def prepare_case_purge(self, case_id: str, actor_id: str, reason_code: str) -> None:
        """Persist purge intent before the irreversible filesystem operation."""
        with self.sessions() as db:
            case = db.scalar(select(UnifiedCaseRecord).where(UnifiedCaseRecord.case_id == case_id))
            if case is None or case.deleted_at is None:
                raise ResourceConflict("病例不在回收站")
            if case.legal_hold:
                raise ResourceConflict("病例处于法律保留状态")
            db.add(DeletionRecord(case_id=case_id, state="purge_pending", reason_code=reason_code, actor_id=actor_id))
            self._add_audit(db, actor_id, "case.purge.prepare", "success", "case", case_id, {"reason_code": reason_code})
            db.commit()

    def set_retention(self, case_id: str, profile: str, retain_until: datetime | None, actor_id: str) -> dict[str, Any]:
        if profile not in {"unclassified", "development_test", "research", "clinical_outpatient", "clinical_inpatient"}:
            raise ValidationRejected("未知留存类型")
        with self.sessions() as db:
            case = db.scalar(select(UnifiedCaseRecord).where(UnifiedCaseRecord.case_id == case_id))
            if case is None:
                raise ResourceNotFound("病例不存在")
            case.retention_profile = profile
            case.retain_until = retain_until
            self._add_audit(db, actor_id, "case.retention.update", "success", "case", case_id, {"profile": profile})
            db.commit()
            return self.serialize_case(case)

    def set_legal_hold(self, case_id: str, active: bool, reason: str | None, actor_id: str) -> dict[str, Any]:
        if active and not reason:
            raise ValidationRejected("启用法律保留时必须填写原因")
        with self.sessions() as db:
            case = db.scalar(select(UnifiedCaseRecord).where(UnifiedCaseRecord.case_id == case_id))
            if case is None:
                raise ResourceNotFound("病例不存在")
            case.legal_hold = active
            case.legal_hold_reason = reason if active else None
            self._add_audit(db, actor_id, "case.legal_hold", "success", "case", case_id, {"active": active, "reason": reason})
            db.commit()
            return self.serialize_case(case)

    def upsert_task(self, task: dict[str, Any]) -> None:
        with self.sessions() as db:
            record = db.get(InferenceTaskRecord, str(task["task_id"]))
            if record is None:
                record = InferenceTaskRecord(
                    task_id=str(task["task_id"]), case_id=str(task["case_id"]),
                    task_type=str(task["task_type"]), run_id=str(task["task_id"]), status=str(task["status"]),
                )
                db.add(record)
            record.status = str(task["status"])
            record.progress = int(task.get("progress", 0))
            record.error_code = task.get("error_code")
            record.retryable = bool(task.get("retryable", False))
            record.updated_at = utc_now()
            db.commit()

    def register_asset(self, case_id: str, asset_type: str, relative_path: str) -> None:
        if Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
            raise ValidationRejected("结果路径必须相对运行目录")
        with self.sessions() as db:
            exists = db.scalar(select(ResultAssetRecord).where(ResultAssetRecord.case_id == case_id, ResultAssetRecord.asset_type == asset_type, ResultAssetRecord.relative_path == relative_path))
            if not exists:
                db.add(ResultAssetRecord(case_id=case_id, asset_type=asset_type, relative_path=relative_path))
                db.commit()

    def list_assets(self, case_id: str) -> list[dict[str, Any]]:
        with self.sessions() as db:
            return [{"type": item.asset_type, "path": item.relative_path} for item in db.scalars(select(ResultAssetRecord).where(ResultAssetRecord.case_id == case_id)).all()]

    def list_contacts(self, actor_id: str) -> list[dict[str, Any]]:
        with self.sessions() as db:
            users = db.scalars(select(UserRecord).where(UserRecord.id != actor_id, UserRecord.is_active.is_(True)).order_by(UserRecord.display_name)).all()
            unread = dict(db.execute(
                select(DirectMessageRecord.sender_id, func.count(DirectMessageRecord.id))
                .where(DirectMessageRecord.recipient_id == actor_id, DirectMessageRecord.read_at.is_(None))
                .group_by(DirectMessageRecord.sender_id)
            ).all())
            return [{**self.serialize_user(item), "unread_count": int(unread.get(item.id, 0))} for item in users]

    def unread_message_count(self, actor_id: str) -> int:
        with self.sessions() as db:
            return int(db.scalar(
                select(func.count(DirectMessageRecord.id)).where(
                    DirectMessageRecord.recipient_id == actor_id,
                    DirectMessageRecord.read_at.is_(None),
                )
            ) or 0)

    def send_message(self, sender_id: str, recipient_id: str, content: str) -> dict[str, Any]:
        content = content.strip()
        if not content or len(content) > 2000:
            raise ValidationRejected("消息长度无效")
        with self.sessions() as db:
            recipient = db.get(UserRecord, recipient_id)
            if recipient is None or not recipient.is_active or recipient_id == sender_id:
                raise ValidationRejected("接收账号无效")
            item = DirectMessageRecord(sender_id=sender_id, recipient_id=recipient_id, content=content)
            db.add(item)
            self._add_audit(db, sender_id, "message.send", "success", "user", recipient_id)
            db.commit()
            return {"id": item.id, "sender_id": sender_id, "recipient_id": recipient_id, "content": content, "created_at": _utc(item.created_at).isoformat(), "read_at": None}

    def get_messages(self, actor_id: str, other_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.sessions() as db:
            rows = db.scalars(select(DirectMessageRecord).where(or_(
                (DirectMessageRecord.sender_id == actor_id) & (DirectMessageRecord.recipient_id == other_id),
                (DirectMessageRecord.sender_id == other_id) & (DirectMessageRecord.recipient_id == actor_id),
            )).order_by(DirectMessageRecord.created_at.desc()).limit(limit)).all()
            return [{"id": item.id, "sender_id": item.sender_id, "recipient_id": item.recipient_id, "content": item.content, "created_at": _utc(item.created_at).isoformat(), "read_at": _utc(item.read_at).isoformat() if item.read_at else None} for item in reversed(rows)]

    def mark_messages_read(self, actor_id: str, other_id: str) -> int:
        with self.sessions() as db:
            rows = db.scalars(select(DirectMessageRecord).where(DirectMessageRecord.sender_id == other_id, DirectMessageRecord.recipient_id == actor_id, DirectMessageRecord.read_at.is_(None))).all()
            for item in rows:
                item.read_at = utc_now()
            db.commit()
            return len(rows)

    def list_audit(self, limit: int = 100, action: str | None = None, subject_id: str | None = None) -> list[dict[str, Any]]:
        with self.sessions() as db:
            query = select(AuditRecord)
            if action:
                query = query.where(AuditRecord.action == action)
            if subject_id:
                query = query.where(AuditRecord.subject_id == subject_id)
            records = db.scalars(query.order_by(AuditRecord.sequence.desc()).limit(limit)).all()
            return [{"sequence": item.sequence, "id": item.id, "user_id": item.user_id, "action": item.action, "outcome": item.outcome, "subject_type": item.subject_type, "subject_id": item.subject_id, "details": json.loads(item.details_json), "created_at": _utc(item.created_at).isoformat(), "record_hash": item.record_hash} for item in records]

    def verify_audit_integrity(self) -> dict[str, Any]:
        previous = "0" * 64
        checked = 0
        with self.sessions() as db:
            for item in db.scalars(select(AuditRecord).order_by(AuditRecord.sequence)).all():
                snapshot = self._audit_snapshot(item.user_id, item.action, item.outcome, item.subject_type, item.subject_id, item.details_json, _utc(item.created_at).isoformat())
                expected = _digest(previous + snapshot)
                if item.previous_hash != previous or item.record_hash != expected:
                    return {"valid": False, "checked": checked, "broken_sequence": item.sequence}
                previous = item.record_hash
                checked += 1
        return {"valid": True, "checked": checked, "head_hash": previous}

    def record_audit(self, user_id: str | None, action: str, outcome: str, subject_type: str | None = None, subject_id: str | None = None, details: dict[str, Any] | None = None) -> None:
        with self.sessions() as db:
            self._add_audit(db, user_id, action, outcome, subject_type, subject_id, details)
            db.commit()

    @staticmethod
    def _audit_snapshot(user_id: str | None, action: str, outcome: str, subject_type: str | None, subject_id: str | None, details_json: str, created_at: str) -> str:
        return json.dumps({"user_id": user_id, "action": action, "outcome": outcome, "subject_type": subject_type, "subject_id": subject_id, "details_json": details_json, "created_at": created_at}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _add_audit(self, db: Session, user_id: str | None, action: str, outcome: str, subject_type: str | None = None, subject_id: str | None = None, details: dict[str, Any] | None = None) -> None:
        with self._audit_lock:
            previous_record = db.scalar(select(AuditRecord).order_by(AuditRecord.sequence.desc()).limit(1))
            previous = previous_record.record_hash if previous_record else "0" * 64
            created = utc_now()
            details_json = json.dumps(details or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            snapshot = self._audit_snapshot(user_id, action, outcome, subject_type, subject_id, details_json, created.isoformat())
            db.add(AuditRecord(user_id=user_id, action=action, outcome=outcome, subject_type=subject_type, subject_id=subject_id, details_json=details_json, previous_hash=previous, record_hash=_digest(previous + snapshot), created_at=created))
