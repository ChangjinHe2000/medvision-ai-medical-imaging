from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from backend.enterprise.models import UserRole
from backend.enterprise.service import AuthenticationRequired, AuthorizationDenied, EnterpriseError
from backend.rag import knowledge_admin


class FakeEnterpriseService:
    @staticmethod
    def require_role(user, *roles) -> None:
        allowed = {UserRole(role).value for role in roles}
        if user.role not in allowed:
            raise AuthorizationDenied("当前账号没有执行此操作的权限")

    @staticmethod
    def record_audit(*args, **kwargs) -> None:
        del args, kwargs

    @staticmethod
    def list_users() -> list[dict]:
        return []


@pytest.fixture()
def knowledge_client(monkeypatch):
    captured_roles: list[str] = []

    async def fake_store_document(**kwargs):
        captured_roles.append(kwargs["user"].role)
        return {"id": "document-1", "status": "UPLOADED", "message": "等待管理员审核"}

    monkeypatch.setattr(knowledge_admin, "_store_document", fake_store_document)
    service = FakeEnterpriseService()
    app = FastAPI()

    @app.exception_handler(EnterpriseError)
    async def enterprise_error(_: Request, exc: EnterpriseError):
        code = 401 if isinstance(exc, AuthenticationRequired) else 403
        return JSONResponse(status_code=code, content={"detail": str(exc)})

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        role = request.headers.get("X-Test-Role", "").strip()
        if role:
            request.state.enterprise_auth = SimpleNamespace(
                user=SimpleNamespace(id=f"{role}-user", role=role)
            )
        return await call_next(request)

    app.include_router(knowledge_admin.build_knowledge_submission_router(service))
    app.include_router(knowledge_admin.build_knowledge_admin_router(service))
    return TestClient(app), captured_roles


@pytest.mark.parametrize("role", ["viewer", "doctor", "admin"])
def test_every_authenticated_role_can_submit(knowledge_client, role: str) -> None:
    client, captured_roles = knowledge_client
    response = client.post(
        "/api/v1/knowledge/documents",
        headers={"X-Test-Role": role},
        files={"file": ("guideline.txt", b"public medical guidance", "text/plain")},
        data={
            "title": "公开指南", "organization": "测试机构", "document_type": "GUIDELINE",
            "specialty": "IMAGING", "version": "1.0", "source_url": "https://example.org/guideline",
            "license_status": "OPEN_ACCESS",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "UPLOADED"
    assert captured_roles[-1] == role


def test_anonymous_submission_is_rejected(knowledge_client) -> None:
    client, _ = knowledge_client
    response = client.post(
        "/api/v1/knowledge/documents",
        files={"file": ("guideline.txt", b"public medical guidance", "text/plain")},
        data={
            "title": "公开指南", "organization": "测试机构", "document_type": "GUIDELINE",
            "specialty": "IMAGING", "version": "1.0", "source_url": "https://example.org/guideline",
            "license_status": "OPEN_ACCESS",
        },
    )
    assert response.status_code == 401


@pytest.mark.parametrize("role", ["viewer", "doctor"])
def test_non_admin_cannot_run_review_pipeline(knowledge_client, role: str) -> None:
    client, _ = knowledge_client
    response = client.post(
        "/api/v1/admin/knowledge/documents/document-1/parse",
        headers={"X-Test-Role": role},
    )
    assert response.status_code == 403
