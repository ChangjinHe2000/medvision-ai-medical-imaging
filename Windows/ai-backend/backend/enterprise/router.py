from __future__ import annotations

import csv
import os
from io import StringIO
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from .models import UserRole
from .privacy import redact_payload
from .schemas import (
    CaseGrantRequest,
    CreateCaseRequest,
    CreateUserRequest,
    InitialAdminRequest,
    LegalHoldRequest,
    LoginRequest,
    PurgeCaseRequest,
    RenameCaseRequest,
    RetentionRequest,
    SendMessageRequest,
    TrashCaseRequest,
    UserStatusRequest,
)
from .service import AuthContext, EnterpriseService, ResourceConflict
from .storage import UnifiedCaseStorage


def _auth(request: Request) -> AuthContext:
    context = getattr(request.state, "enterprise_auth", None)
    if context is None:
        raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_REQUIRED", "message": "请先登录"})
    return context


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def build_enterprise_router(service: EnterpriseService, storage: UnifiedCaseStorage) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["enterprise-v1"])

    @router.get("/auth/setup/status")
    async def setup_status() -> dict[str, Any]:
        return {"required": service.count_users() == 0}

    @router.post("/auth/setup", status_code=status.HTTP_201_CREATED)
    async def setup_admin(payload: InitialAdminRequest) -> dict[str, Any]:
        allow_web_setup = os.environ.get("PPGL_ALLOW_WEB_ADMIN_SETUP", "false").strip().lower() in {"1", "true", "yes", "on"}
        if not allow_web_setup:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "WEB_ADMIN_SETUP_DISABLED",
                    "message": "网页初始化管理员已禁用，请由部署人员运行管理员初始化命令",
                },
            )
        user = service.create_user(
            username=payload.username,
            display_name=payload.display_name,
            password=payload.password,
            role=UserRole.ADMIN,
            initial_setup=True,
        )
        return {"user": user, "message": "初始管理员创建成功，请登录"}

    @router.post("/auth/login")
    async def login(payload: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
        user, session_token, csrf_token, expires_at = service.login(
            payload.username,
            payload.password,
            ip_address=_client_ip(request),
        )
        response.set_cookie(
            service.config.session_cookie_name,
            session_token,
            httponly=True,
            secure=service.config.cookie_secure,
            samesite="strict",
            expires=expires_at,
            path="/",
        )
        response.set_cookie(
            service.config.csrf_cookie_name,
            csrf_token,
            httponly=False,
            secure=service.config.cookie_secure,
            samesite="strict",
            expires=expires_at,
            path="/",
        )
        return {"user": user, "csrf_token": csrf_token}

    @router.get("/auth/me")
    async def me(request: Request) -> dict[str, Any]:
        return {"user": service.serialize_user(_auth(request).user)}

    @router.post("/auth/logout")
    async def logout(request: Request, response: Response) -> dict[str, str]:
        service.logout(request.cookies.get(service.config.session_cookie_name))
        response.delete_cookie(service.config.session_cookie_name, path="/")
        response.delete_cookie(service.config.csrf_cookie_name, path="/")
        return {"message": "已退出登录"}

    @router.get("/admin/users")
    async def list_users(request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.require_role(context.user, UserRole.ADMIN)
        users = service.list_users()
        return {"users": users, "total": len(users)}

    @router.post("/admin/users", status_code=status.HTTP_201_CREATED)
    async def create_user(payload: CreateUserRequest, request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.require_role(context.user, UserRole.ADMIN)
        return {"user": service.create_user(**payload.model_dump(), actor_id=context.user.id)}

    @router.patch("/admin/users/{user_id}/status")
    async def set_user_status(user_id: str, payload: UserStatusRequest, request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.require_role(context.user, UserRole.ADMIN)
        return {"user": service.set_user_active(user_id, payload.active, context.user.id)}

    @router.get("/cases")
    async def list_cases(request: Request, include_deleted: bool = False) -> dict[str, Any]:
        context = _auth(request)
        if include_deleted and context.user.role != UserRole.ADMIN.value:
            include_deleted = False
        cases = service.list_cases(context.user, include_deleted=include_deleted)
        return {"cases": cases, "total": len(cases)}

    @router.post("/cases", status_code=status.HTTP_201_CREATED)
    async def create_case(payload: CreateCaseRequest, request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.require_role(context.user, UserRole.ADMIN, UserRole.DOCTOR)
        created = storage.create(payload.case_type, payload.display_name)
        try:
            registered = service.register_case(
                created["case_id"], payload.case_type, payload.display_name, context.user
            )
        except Exception:
            storage.rollback_unregistered(created["case_id"], payload.case_type)
            raise
        return registered

    @router.get("/cases/{case_id}")
    async def get_case(case_id: str, request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.authorize_case(context.user, case_id, "read")
        record = service.get_case_record(case_id)
        return service.serialize_case(record)

    @router.patch("/cases/{case_id}")
    async def rename_case(case_id: str, payload: RenameCaseRequest, request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.authorize_case(context.user, case_id, "edit")
        record = service.get_case_record(case_id)
        old_name = record.display_name
        storage.rename(case_id, record.case_type, payload.display_name)
        try:
            return service.rename_case(case_id, payload.display_name, context.user.id)
        except Exception:
            storage.rename(case_id, record.case_type, old_name)
            raise

    @router.delete("/cases/{case_id}")
    async def trash_case(case_id: str, payload: TrashCaseRequest, request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.authorize_case(context.user, case_id, "manage")
        record = service.get_case_record(case_id)
        storage.move_to_trash(case_id, record.case_type)
        try:
            return service.mark_case_trashed(case_id, context.user.id, payload.reason_code)
        except Exception:
            storage.restore(case_id, record.case_type)
            raise

    @router.get("/trash")
    async def list_trash(request: Request) -> dict[str, Any]:
        context = _auth(request)
        cases = [item for item in service.list_cases(context.user, include_deleted=True) if item["deleted"]]
        return {"cases": cases, "total": len(cases)}

    @router.post("/trash/{case_id}/restore")
    async def restore_case(case_id: str, request: Request) -> dict[str, Any]:
        context = _auth(request)
        record = service.get_case_record(case_id)
        if context.user.role != UserRole.ADMIN.value and record.owner_id != context.user.id:
            raise HTTPException(status_code=403, detail={"code": "AUTHORIZATION_DENIED", "message": "无权恢复该病例"})
        storage.restore(case_id, record.case_type)
        try:
            return service.mark_case_restored(case_id, context.user.id)
        except Exception:
            storage.move_to_trash(case_id, record.case_type)
            raise

    @router.delete("/trash/{case_id}")
    async def purge_case(case_id: str, payload: PurgeCaseRequest, request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.require_role(context.user, UserRole.ADMIN)
        if payload.confirmation != case_id:
            raise ResourceConflict("永久清除确认内容必须与病例编号完全一致")
        record = service.get_case_record(case_id)
        if not record.deleted_at:
            raise ResourceConflict("病例不在回收站")
        service.prepare_case_purge(case_id, context.user.id, payload.reason_code)
        storage.purge(case_id, record.case_type)
        service.mark_case_purged(case_id, context.user.id, payload.reason_code)
        return {"case_id": case_id, "purged": True, "recoverable": False}

    @router.get("/cases/{case_id}/grants")
    async def list_grants(case_id: str, request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.authorize_case(context.user, case_id, "manage")
        return {"grants": service.list_case_grants(case_id)}

    @router.put("/cases/{case_id}/grants")
    async def set_grant(case_id: str, payload: CaseGrantRequest, request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.authorize_case(context.user, case_id, "manage")
        return service.set_case_grant(case_id, payload.user_id, payload.permission, context.user.id)

    @router.delete("/cases/{case_id}/grants/{user_id}")
    async def revoke_grant(case_id: str, user_id: str, request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.authorize_case(context.user, case_id, "manage")
        service.revoke_case_grant(case_id, user_id, context.user.id)
        return {"revoked": True}

    @router.put("/cases/{case_id}/retention")
    async def set_retention(case_id: str, payload: RetentionRequest, request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.require_role(context.user, UserRole.ADMIN)
        return service.set_retention(case_id, payload.profile, payload.retain_until, context.user.id)

    @router.put("/cases/{case_id}/legal-hold")
    async def set_legal_hold(case_id: str, payload: LegalHoldRequest, request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.require_role(context.user, UserRole.ADMIN)
        return service.set_legal_hold(case_id, payload.active, payload.reason, context.user.id)

    @router.get("/cases/{case_id}/assets")
    async def list_assets(case_id: str, request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.authorize_case(context.user, case_id, "read")
        return {"assets": service.list_assets(case_id)}

    @router.get("/messages/contacts")
    async def contacts(request: Request) -> dict[str, Any]:
        context = _auth(request)
        contacts = service.list_contacts(context.user.id)
        return {"contacts": contacts, "total": len(contacts)}

    @router.get("/messages/unread")
    async def unread_messages(request: Request) -> dict[str, Any]:
        context = _auth(request)
        return {"unread_count": service.unread_message_count(context.user.id)}

    @router.get("/messages/{other_user_id}")
    async def messages(other_user_id: str, request: Request, limit: int = 100) -> dict[str, Any]:
        context = _auth(request)
        rows = service.get_messages(context.user.id, other_user_id, max(1, min(limit, 200)))
        return {"messages": rows, "total": len(rows)}

    @router.post("/messages", status_code=status.HTTP_201_CREATED)
    async def send_message(payload: SendMessageRequest, request: Request) -> dict[str, Any]:
        context = _auth(request)
        return service.send_message(context.user.id, payload.recipient_id, payload.content)

    @router.post("/messages/{other_user_id}/read")
    async def mark_read(other_user_id: str, request: Request) -> dict[str, Any]:
        context = _auth(request)
        return {"read_count": service.mark_messages_read(context.user.id, other_user_id)}

    @router.get("/admin/audit")
    async def audit(request: Request, limit: int = 100, action: str | None = None, subject_id: str | None = None) -> dict[str, Any]:
        context = _auth(request)
        service.require_role(context.user, UserRole.ADMIN)
        rows = redact_payload(service.list_audit(max(1, min(limit, 1000)), action, subject_id))
        return {"records": rows, "total": len(rows)}

    @router.get("/admin/audit/integrity")
    async def audit_integrity(request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.require_role(context.user, UserRole.ADMIN)
        return service.verify_audit_integrity()

    @router.get("/admin/audit/export.csv")
    async def export_audit(request: Request) -> StreamingResponse:
        context = _auth(request)
        service.require_role(context.user, UserRole.ADMIN)
        rows = redact_payload(service.list_audit(1000))
        output = StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["sequence", "user_id", "action", "outcome", "subject_type", "subject_id", "created_at", "record_hash"])
        for item in rows:
            writer.writerow([item.get(key, "") for key in ("sequence", "user_id", "action", "outcome", "subject_type", "subject_id", "created_at", "record_hash")])
        service.record_audit(context.user.id, "audit.export", "success", "audit", None, {"row_count": len(rows)})
        return StreamingResponse(
            iter([output.getvalue().encode("utf-8-sig")]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="audit-export.csv"'},
        )

    return router
