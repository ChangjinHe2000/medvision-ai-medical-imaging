from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.enterprise.router import build_enterprise_router
from backend.enterprise.service import EnterpriseError, EnterpriseService, EnterpriseServiceConfig
from backend.enterprise.storage import UnifiedCaseStorage


def main() -> int:
    with TemporaryDirectory(prefix="medvision_stage6_") as temporary:
        root = Path(temporary)
        service = EnterpriseService(
            EnterpriseServiceConfig(
                database_url=f"sqlite:///{(root / 'database' / 'test.db').as_posix()}",
                session_cookie_name="stage6_session",
                csrf_cookie_name="stage6_csrf",
            )
        )
        service.initialize()
        service.create_user(
            username="admin",
            display_name="系统管理员",
            password="Enterprise!2026",
            role="admin",
            initial_setup=True,
        )
        storage = UnifiedCaseStorage(
            ct_root=root / "cases",
            ct_trash_root=root / "trash" / "ct",
            brain_root=root / "brain-cases",
            brain_trash_root=root / "trash" / "brain",
        )
        app = FastAPI()

        @app.exception_handler(EnterpriseError)
        async def handle_enterprise(_: Request, exc: EnterpriseError):
            status = 403 if exc.code in {"AUTHORIZATION_DENIED", "CSRF_VALIDATION_FAILED"} else 401
            return JSONResponse(status_code=status, content={"detail": {"code": exc.code, "message": str(exc)}})

        public = {"/api/v1/auth/setup/status", "/api/v1/auth/setup", "/api/v1/auth/login"}

        @app.middleware("http")
        async def auth_boundary(request: Request, call_next):
            path = request.url.path.rstrip("/")
            if path.startswith("/api/v1") and path not in public:
                context = service.resolve_session(request.cookies.get("stage6_session"))
                request.state.enterprise_auth = context
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    service.validate_csrf(
                        context,
                        request.cookies.get("stage6_csrf"),
                        request.headers.get("X-CSRF-Token"),
                    )
            return await call_next(request)

        app.include_router(build_enterprise_router(service, storage))
        try:
            with TestClient(app) as client:
                assert client.get("/api/v1/auth/setup/status").json() == {"required": False}
                blocked = client.post(
                    "/api/v1/auth/setup",
                    json={"username": "other", "display_name": "其他管理员", "password": "Enterprise!2027"},
                )
                assert blocked.status_code == 403
                login = client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "Enterprise!2026"},
                )
                assert login.status_code == 200
                headers = {"X-CSRF-Token": login.json()["csrf_token"]}
                assert client.get("/api/v1/auth/me").json()["user"]["role"] == "admin"
                viewer = client.post(
                    "/api/v1/admin/users",
                    headers=headers,
                    json={
                        "username": "viewer1",
                        "display_name": "只读用户",
                        "password": "ViewerPassword!2026",
                        "role": "viewer",
                    },
                )
                assert viewer.status_code == 201
                case_types = []
                for case_type, display_name in (("ppgl_ct", "CT 测试病例"), ("brain_mri", "MRI 测试病例")):
                    response = client.post(
                        "/api/v1/cases",
                        headers=headers,
                        json={"case_type": case_type, "display_name": display_name},
                    )
                    assert response.status_code == 201
                    case_types.append(response.json()["case_type"])
                assert set(case_types) == {"ppgl_ct", "brain_mri"}
                sent = client.post(
                    "/api/v1/messages",
                    headers=headers,
                    json={"recipient_id": viewer.json()["user"]["id"], "content": "请查看测试病例"},
                )
                assert sent.status_code == 201
                assert service.verify_audit_integrity()["valid"] is True
        finally:
            service.close()
    print("Stage 6 API contract smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
