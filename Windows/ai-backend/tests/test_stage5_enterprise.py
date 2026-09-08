from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from backend.enterprise.router import build_enterprise_router
from backend.enterprise.service import EnterpriseError, EnterpriseService, EnterpriseServiceConfig
from backend.enterprise.storage import UnifiedCaseStorage


@pytest.fixture()
def enterprise_app(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PPGL_ALLOW_WEB_ADMIN_SETUP", "true")
    service = EnterpriseService(
        EnterpriseServiceConfig(
            database_url=f"sqlite:///{(tmp_path / 'database' / 'test.db').as_posix()}",
            session_cookie_name="test_session",
            csrf_cookie_name="test_csrf",
        )
    )
    service.initialize()
    storage = UnifiedCaseStorage(
        ct_root=tmp_path / "cases",
        ct_trash_root=tmp_path / "trash" / "ct",
        brain_root=tmp_path / "brain-cases",
        brain_trash_root=tmp_path / "trash" / "brain",
    )
    app = FastAPI()

    @app.exception_handler(EnterpriseError)
    async def handle_enterprise(_: Request, exc: EnterpriseError):
        status = {
            "AUTHENTICATION_REQUIRED": 401,
            "INVALID_CREDENTIALS": 401,
            "AUTHORIZATION_DENIED": 403,
            "CSRF_VALIDATION_FAILED": 403,
            "RESOURCE_NOT_FOUND": 404,
            "RESOURCE_CONFLICT": 409,
            "VALIDATION_FAILED": 422,
        }.get(exc.code, 500)
        return JSONResponse(status_code=status, content={"detail": {"code": exc.code, "message": str(exc)}})

    public = {"/api/v1/auth/setup/status", "/api/v1/auth/setup", "/api/v1/auth/login"}

    @app.middleware("http")
    async def auth_boundary(request: Request, call_next):
        path = request.url.path.rstrip("/")
        if path.startswith("/api/v1") and path not in public:
            try:
                context = service.resolve_session(request.cookies.get("test_session"))
                request.state.enterprise_auth = context
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    service.validate_csrf(
                        context,
                        request.cookies.get("test_csrf"),
                        request.headers.get("X-CSRF-Token"),
                    )
            except EnterpriseError as exc:
                status = 403 if exc.code in {"AUTHORIZATION_DENIED", "CSRF_VALIDATION_FAILED"} else 401
                return JSONResponse(status_code=status, content={"detail": {"code": exc.code, "message": str(exc)}})
        response = await call_next(request)
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(build_enterprise_router(service, storage))
    try:
        yield TestClient(app), service, storage
    finally:
        service.close()


def setup_and_login(client: TestClient, username: str = "admin") -> dict[str, str]:
    setup = client.post(
        "/api/v1/auth/setup",
        json={"username": username, "display_name": "系统管理员", "password": "Enterprise!2026"},
    )
    assert setup.status_code == 201, setup.text
    login = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "Enterprise!2026"},
    )
    assert login.status_code == 200, login.text
    return {"X-CSRF-Token": login.json()["csrf_token"]}


def login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def test_setup_cookie_csrf_and_no_cache(enterprise_app):
    client, _, _ = enterprise_app
    assert client.get("/api/v1/auth/setup/status").json() == {"required": True}
    assert client.get("/api/v1/auth/me").status_code == 401
    headers = setup_and_login(client)
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200
    assert response.json()["user"]["role"] == "admin"
    assert response.headers["cache-control"] == "no-store"
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


def test_two_case_types_isolation_grants_and_private_storage(enterprise_app):
    client, service, storage = enterprise_app
    admin_headers = setup_and_login(client)
    user = client.post(
        "/api/v1/admin/users",
        headers=admin_headers,
        json={
            "username": "doctor2",
            "display_name": "第二位医生",
            "password": "DoctorPassword!2026",
            "role": "doctor",
        },
    ).json()["user"]
    ct = client.post(
        "/api/v1/cases",
        headers=admin_headers,
        json={"case_type": "ppgl_ct", "display_name": "CT 测试病例"},
    )
    mri = client.post(
        "/api/v1/cases",
        headers=admin_headers,
        json={"case_type": "brain_mri", "display_name": "MRI 测试病例"},
    )
    assert ct.status_code == 201 and mri.status_code == 201
    ct_case = ct.json()
    mri_case = mri.json()
    assert storage.case_root(ct_case["case_id"], "ppgl_ct").parent == storage.ct_root
    assert storage.case_root(mri_case["case_id"], "brain_mri").parent == storage.brain_service.cases_root

    doctor_headers = login(client, "doctor2", "DoctorPassword!2026")
    assert client.get(f"/api/v1/cases/{ct_case['case_id']}").status_code == 403
    admin_headers = login(client, "admin", "Enterprise!2026")
    grant = client.put(
        f"/api/v1/cases/{ct_case['case_id']}/grants",
        headers=admin_headers,
        json={"user_id": user["id"], "permission": "read"},
    )
    assert grant.status_code == 200
    doctor_headers = login(client, "doctor2", "DoctorPassword!2026")
    assert client.get(f"/api/v1/cases/{ct_case['case_id']}").status_code == 200
    assert client.patch(
        f"/api/v1/cases/{ct_case['case_id']}",
        headers=doctor_headers,
        json={"display_name": "越权改名"},
    ).status_code == 403
    assert service.verify_audit_integrity()["valid"] is True


def test_trash_restore_purge_messages_and_privacy_guard(enterprise_app):
    client, service, storage = enterprise_app
    admin_headers = setup_and_login(client)
    doctor = client.post(
        "/api/v1/admin/users",
        headers=admin_headers,
        json={
            "username": "doctor3",
            "display_name": "第三位医生",
            "password": "DoctorPassword!2027",
            "role": "doctor",
        },
    ).json()["user"]
    rejected = client.post(
        "/api/v1/cases",
        headers=admin_headers,
        json={"case_type": "ppgl_ct", "display_name": "患者姓名：张三"},
    )
    assert rejected.status_code == 422
    created = client.post(
        "/api/v1/cases",
        headers=admin_headers,
        json={"case_type": "ppgl_ct", "display_name": "脱敏病例"},
    ).json()
    case_id = created["case_id"]
    trashed = client.request(
        "DELETE",
        f"/api/v1/cases/{case_id}",
        headers=admin_headers,
        json={"reason_code": "test_cleanup"},
    )
    assert trashed.status_code == 200
    assert storage.case_root(case_id, "ppgl_ct", trash=True).is_dir()
    assert client.post(f"/api/v1/trash/{case_id}/restore", headers=admin_headers).status_code == 200
    assert storage.case_root(case_id, "ppgl_ct").is_dir()
    client.request(
        "DELETE",
        f"/api/v1/cases/{case_id}",
        headers=admin_headers,
        json={"reason_code": "test_cleanup"},
    )
    purge = client.request(
        "DELETE",
        f"/api/v1/trash/{case_id}",
        headers=admin_headers,
        json={"reason_code": "approved_purge", "confirmation": case_id},
    )
    assert purge.status_code == 200

    sent = client.post(
        "/api/v1/messages",
        headers=admin_headers,
        json={"recipient_id": doctor["id"], "content": "请复核测试病例"},
    )
    assert sent.status_code == 201
    doctor_headers = login(client, "doctor3", "DoctorPassword!2027")
    conversation = client.get(f"/api/v1/messages/{sent.json()['sender_id']}")
    assert conversation.json()["messages"][0]["content"] == "请复核测试病例"
    assert client.post(f"/api/v1/messages/{sent.json()['sender_id']}/read", headers=doctor_headers).json()["read_count"] == 1
    assert service.verify_audit_integrity()["valid"] is True
