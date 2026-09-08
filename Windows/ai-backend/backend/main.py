from fastapi import BackgroundTasks, FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union
import hmac
import jwt
import os
import shutil
import subprocess
import uuid
import json
import sys
import traceback
import urllib.error
import urllib.request
import zipfile
from threading import Lock

from backend.config import load_settings
from backend.domain.task_contract import TaskType
from backend.services.inference_coordinator import InferenceCoordinator
from backend.workers.process_runner import WorkerExecutionError
from backend.core.runtime import configured_path, subprocess_runtime_env
from backend.ct.inference_wrapper import run_single_case
from backend.ct.run_totalseg import create_mesh_outputs
from backend.enterprise.privacy import contains_sensitive_text, install_privacy_log_filter, redact_payload
from backend.enterprise.service import (
    AuthenticationRequired,
    AuthorizationDenied,
    CsrfRejected,
    EnterpriseError,
    EnterpriseService,
    EnterpriseServiceConfig,
    InvalidCredentials,
    ResourceConflict,
    ResourceNotFound,
    ValidationRejected,
)
from backend.enterprise.storage import UnifiedCaseStorage
from backend.enterprise.report_service import UnifiedReportError, UnifiedReportService
from backend.report_agent import (
    call_ollama_chat_text,
    call_openai_text,
    llm_provider,
    stream_ollama_chat_text,
    stream_openai_text,
)

app = FastAPI(title="PPGL Internal AI Service")
install_privacy_log_filter()


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, Any]:
    """Process liveness probe; it does not load any GPU model."""
    return {"status": "ok", "service": "medvision-fastapi", "version": "0.8.0"}


@app.get("/ready", include_in_schema=False)
async def ready() -> dict[str, Any]:
    """Local readiness probe used by the Windows launcher."""
    return {
        "status": "ready",
        "service": "medvision-fastapi",
        "database": "initialized",
        "gpu_models_loaded": False,
    }

JWT_SECRET = os.environ.get("PPGL_AUTH_JWT_SECRET", "").strip()
INTERNAL_API_KEY = os.environ.get("PPGL_INTERNAL_API_KEY", "").strip()
CORS_ORIGINS = [
    item.strip()
    for item in os.environ.get(
        "PPGL_CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
    ).split(",")
    if item.strip()
]


# 允许前端访问后端
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_api_authentication(request: Request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    # /api/v1 uses the enterprise HttpOnly Cookie + CSRF security boundary.
    if request.url.path.startswith("/api/v1"):
        return await call_next(request)

    internal_key = request.headers.get("X-PPGL-Internal-Key", "")
    if INTERNAL_API_KEY and internal_key and hmac.compare_digest(internal_key, INTERNAL_API_KEY):
        delegated_user_id = request.headers.get("X-PPGL-User-Id", "").strip()
        delegated_role = request.headers.get("X-PPGL-User-Role", "").strip().upper()
        if delegated_user_id or delegated_role:
            if not delegated_user_id.isdigit() or delegated_role not in {"DOCTOR", "ADMIN"}:
                return JSONResponse(status_code=400, content={"detail": "Invalid delegated user context"})
            request.state.auth = {
                "kind": "delegated",
                "uid": int(delegated_user_id),
                "role": delegated_role,
                "sub": request.headers.get("X-PPGL-Username", "").strip(),
            }
        else:
            request.state.auth = {"kind": "internal"}
        return await call_next(request)

    authorization = request.headers.get("Authorization", "")
    token = ""
    if authorization.lower().startswith("bearer "):
        token = authorization.split(None, 1)[1].strip()
    if not token:
        token = request.cookies.get("PPGL_ACCESS_TOKEN", "").strip()
    if not token:
        return JSONResponse(status_code=401, content={"detail": "Missing or invalid authentication token"})
    if not JWT_SECRET:
        return JSONResponse(status_code=503, content={"detail": "PPGL_AUTH_JWT_SECRET is not configured"})

    try:
        claims = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
            issuer="ppgl-analyze",
            options={"require": ["exp", "iat", "iss", "sub", "uid", "role"]},
        )
    except jwt.PyJWTError:
        return JSONResponse(status_code=401, content={"detail": "Authentication token is invalid or expired"})

    if claims.get("role") not in {"DOCTOR", "ADMIN"}:
        return JSONResponse(status_code=403, content={"detail": "Doctor access is required"})
    request.state.auth = claims
    path_parts = request.url.path.strip("/").split("/")
    if len(path_parts) >= 3 and path_parts[:2] == ["api", "cases"] and path_parts[2] != "upload":
        case_info_path = CASES_DIR / path_parts[2] / "case_info.json"
        if claims.get("role") != "ADMIN":
            if not case_info_path.is_file():
                return JSONResponse(status_code=403, content={"detail": "Case ownership cannot be verified"})
            try:
                case_info = json.loads(case_info_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return JSONResponse(status_code=403, content={"detail": "Case ownership cannot be verified"})
            owner_id = case_info.get("owner_user_id")
            if owner_id != claims.get("uid"):
                return JSONResponse(status_code=403, content={"detail": "You do not have access to this case"})
    return await call_next(request)

# 代码目录从当前文件定位；运行数据默认放到项目外，避免和源码混放。
BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent
SETTINGS = load_settings()
DATA_ROOT = SETTINGS.data_root

# 病例保存目录：$PPGL_DATA_ROOT/cases
CASES_DIR = SETTINGS.cases_dir
CASES_DIR.mkdir(parents=True, exist_ok=True)
RUNNING_CASES = set()
RUNNING_PPGL_CASES = set()
SEGMENTATION_LOCK = Lock()
INFERENCE_COORDINATOR = InferenceCoordinator(SETTINGS, execution_lock=SEGMENTATION_LOCK)

ENTERPRISE_SERVICE = EnterpriseService(
    EnterpriseServiceConfig(
        database_url=SETTINGS.auth_database_url,
        session_hours=SETTINGS.auth_session_hours,
        session_cookie_name=SETTINGS.auth_session_cookie_name,
        csrf_cookie_name=SETTINGS.auth_csrf_cookie_name,
        cookie_secure=SETTINGS.auth_cookie_secure,
    )
)
ENTERPRISE_SERVICE.initialize()
ENTERPRISE_STORAGE = UnifiedCaseStorage(
    ct_root=SETTINGS.cases_dir,
    ct_trash_root=SETTINGS.case_trash_dir,
    brain_root=SETTINGS.brain_cases_dir,
    brain_trash_root=SETTINGS.brain_trash_dir,
)
UNIFIED_REPORT_SERVICE = UnifiedReportService(ENTERPRISE_STORAGE)

_ENTERPRISE_PUBLIC_PATHS = {
    "/api/v1/auth/setup/status",
    "/api/v1/auth/setup",
    "/api/v1/auth/login",
}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def enterprise_security_boundary(request: Request, call_next):
    path = request.url.path.rstrip("/") or "/"
    if not path.startswith("/api/v1"):
        response = await call_next(request)
    elif request.method == "OPTIONS" or path in _ENTERPRISE_PUBLIC_PATHS:
        response = await call_next(request)
    else:
        try:
            context = ENTERPRISE_SERVICE.resolve_session(
                request.cookies.get(SETTINGS.auth_session_cookie_name)
            )
            request.state.enterprise_auth = context
            if request.method not in _SAFE_METHODS:
                ENTERPRISE_SERVICE.validate_csrf(
                    context,
                    request.cookies.get(SETTINGS.auth_csrf_cookie_name),
                    request.headers.get("X-CSRF-Token"),
                )
            response = await call_next(request)
        except EnterpriseError as exc:
            status_code = 403 if isinstance(exc, (AuthorizationDenied, CsrfRejected)) else 401
            ENTERPRISE_SERVICE.record_audit(
                None,
                "security.request.denied",
                "failure",
                "route",
                path,
                {"code": exc.code},
            )
            response = JSONResponse(
                status_code=status_code,
                content={"detail": {"code": exc.code, "message": str(exc)}},
            )

    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(EnterpriseError)
async def enterprise_error_handler(request: Request, exc: EnterpriseError):
    if isinstance(exc, (AuthenticationRequired, InvalidCredentials)):
        status_code = 401
    elif isinstance(exc, (AuthorizationDenied, CsrfRejected)):
        status_code = 403
    elif isinstance(exc, ResourceNotFound):
        status_code = 404
    elif isinstance(exc, ResourceConflict):
        status_code = 409
    elif isinstance(exc, ValidationRejected):
        status_code = 422
    else:
        status_code = 500
    context = getattr(request.state, "enterprise_auth", None)
    ENTERPRISE_SERVICE.record_audit(
        context.user.id if context else None,
        "api.request.failed",
        "failure",
        "route",
        request.url.path,
        redact_payload({"code": exc.code}),
    )
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": exc.code, "message": str(exc)}},
    )


PPGL_V5_DIR = SETTINGS.ppgl_model_dir
PPGL_V5_CHECKPOINT = SETTINGS.ppgl_checkpoint
PPGL_V5_MODEL_CONFIG = SETTINGS.ppgl_model_config


class GenericLlmStreamChatRequest(BaseModel):
    question: str = ""
    context: Any = Field(default_factory=dict)
    history: List[Dict[str, Any]] = Field(default_factory=list)
    messages: Optional[List[Dict[str, Any]]] = None
    max_tokens: int = Field(default=800, ge=64, le=2000)


class RagSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=20)
    retrieve_k: int = Field(default=20, ge=5, le=50)
    retrieval_mode: Literal["dense", "hybrid", "hybrid_rerank"] = "hybrid_rerank"


class RagQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=6, ge=1, le=10)
    retrieve_k: int = Field(default=20, ge=5, le=50)
    retrieval_mode: Literal["dense", "hybrid", "hybrid_rerank"] = "hybrid_rerank"
    max_tokens: int = Field(default=900, ge=128, le=1600)


class RenameCaseRequest(BaseModel):
    new_case_id: str = Field(min_length=1, max_length=120)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path: Path) -> dict:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path.name}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ollama_chat_config() -> Optional[Dict[str, Any]]:
    enabled = os.environ.get("PPGL_ENABLE_OLLAMA", "false").strip().lower() in {"1", "true", "yes", "on"}
    base_url = os.environ.get("CHAT_OPENAI_BASE_URL", os.environ.get("PPGL_OLLAMA_BASE_URL", ""))
    if "11434" not in base_url:
        return None
    model = os.environ.get("CHAT_OPENAI_MODEL", os.environ.get("PPGL_OLLAMA_MODEL", "")).strip()
    if not model:
        return None
    if not enabled and "CHAT_OPENAI_MODEL" not in os.environ:
        return None
    native_base_url = base_url.rstrip("/")
    if native_base_url.endswith("/v1"):
        native_base_url = native_base_url[:-3]
    keep_alive = os.environ.get("OLLAMA_KEEP_ALIVE", os.environ.get("PPGL_OLLAMA_KEEP_ALIVE", "-1")).strip() or "-1"
    try:
        keep_alive_value: Union[int, str] = int(keep_alive)
    except ValueError:
        keep_alive_value = keep_alive
    return {
        "base_url": native_base_url,
        "model": model,
        "keep_alive": keep_alive_value,
    }


def post_ollama_json(url: str, payload: Dict[str, Any], timeout: float) -> Optional[Dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return None
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


def unload_chat_model_for_segmentation(log_path: Optional[Path] = None) -> None:
    config = ollama_chat_config()
    if config is None:
        return
    payload = {"model": config["model"], "keep_alive": 0}
    response = post_ollama_json(f"{config['base_url']}/api/generate", payload, timeout=30)
    if log_path is not None:
        message = (
            f"[{now_text()}] Ollama chat model unloaded before segmentation: {config['model']}"
            if response is not None
            else f"[{now_text()}] Ollama chat model unload skipped/failed before segmentation: {config['model']}"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(message + "\n")


def preload_chat_model_after_segmentation(log_path: Optional[Path] = None) -> None:
    config = ollama_chat_config()
    if config is None:
        return
    payload = {
        "model": config["model"],
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
        "think": False,
        "keep_alive": config["keep_alive"],
        "options": {"num_predict": 1, "temperature": 0},
    }
    response = post_ollama_json(f"{config['base_url']}/api/chat", payload, timeout=180)
    if log_path is not None:
        message = (
            f"[{now_text()}] Ollama chat model preloaded after segmentation: {config['model']}"
            if response is not None
            else f"[{now_text()}] Ollama chat model preload failed after segmentation: {config['model']}"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(message + "\n")


def stream_event(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def text_from_payload_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, indent=2)


def has_payload_context(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def build_generic_llm_prompt(payload: GenericLlmStreamChatRequest) -> str:
    if payload.messages:
        message_lines = []
        for item in payload.messages:
            role = str(item.get("role", "user")).strip() or "user"
            content = text_from_payload_value(item.get("content", ""))
            if content:
                message_lines.append(f"{role}: {content}")
        prompt = "\n\n".join(message_lines).strip()
        if prompt:
            return prompt

    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    has_context = has_payload_context(payload.context)
    context_text = text_from_payload_value(payload.context) if has_context else ""
    history_text = text_from_payload_value(payload.history[-6:]) if payload.history else ""
    if not has_context and not history_text:
        return question

    parts = []
    if history_text:
        parts.append(f"【最近对话历史】\n{history_text}")
    if has_context:
        parts.append(f"【上下文】\n{context_text}")
    parts.append(f"【用户输入】\n{question}")
    return "\n\n".join(parts)


def parse_status_time(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def validated_case_dir_path(case_id: str) -> Path:
    if (
        not case_id
        or not case_id.strip()
        or case_id in {".", ".."}
        or "/" in case_id
        or "\\" in case_id
    ):
        raise HTTPException(status_code=400, detail="Invalid case_id")

    cases_root = CASES_DIR.resolve()
    case_dir = (CASES_DIR / case_id).resolve()
    if case_dir.parent != cases_root:
        raise HTTPException(status_code=400, detail="Invalid case_id")
    return case_dir


def case_dir_for(case_id: str) -> Path:
    case_dir = validated_case_dir_path(case_id)
    if not case_dir.is_dir():
        raise HTTPException(status_code=404, detail="Case not found")
    return case_dir


def rewrite_case_metadata_value(value: Any, old_case_id: str, new_case_id: str, old_dir: Path, new_dir: Path) -> Any:
    if isinstance(value, dict):
        updated = {}
        for key, item in value.items():
            if key == "case_id" and isinstance(item, str):
                updated[key] = new_case_id
            else:
                updated[key] = rewrite_case_metadata_value(item, old_case_id, new_case_id, old_dir, new_dir)
        return updated
    if isinstance(value, list):
        return [rewrite_case_metadata_value(item, old_case_id, new_case_id, old_dir, new_dir) for item in value]
    if isinstance(value, str):
        return value.replace(str(old_dir), str(new_dir))
    return value


def rewrite_case_metadata_files(case_dir: Path, old_case_id: str, new_case_id: str, old_dir: Path) -> None:
    for json_path in case_dir.rglob("*.json"):
        try:
            data = read_json(json_path)
        except Exception:
            continue
        updated = rewrite_case_metadata_value(data, old_case_id, new_case_id, old_dir, case_dir)
        write_json(json_path, updated)


def update_status(case_dir: Path, status: str, message: str, progress: int, **extra) -> None:
    payload = {
        "case_id": case_dir.name,
        "status": status,
        "message": message,
        "progress": int(progress),
        "updated_at": now_text(),
        **extra,
    }
    write_json(case_dir / "status.json", payload)


def update_ppgl_status(case_dir: Path, status: str, message: str, progress: int, **extra) -> None:
    payload = {
        "case_id": case_dir.name,
        "task": "ppgl",
        "status": status,
        "message": message,
        "progress": int(progress),
        "updated_at": now_text(),
        **extra,
    }
    write_json(case_dir / "ppgl_status.json", payload)


def organ_task_observer(case_dir: Path):
    def observe(record: dict[str, Any]) -> None:
        status, message, progress, extra = INFERENCE_COORDINATOR.legacy_progress(record)
        if status == "running":
            RUNNING_CASES.add(case_dir.name)
            message = "正在进行全器官分割"
        elif status in {"completed", "failed", "cancelled", "timed_out"}:
            RUNNING_CASES.discard(case_dir.name)
        if status == "completed":
            message = "全器官分割完成"
        update_status(case_dir, status, message, progress, **extra)

    return observe


def ppgl_task_observer(case_dir: Path):
    def observe(record: dict[str, Any]) -> None:
        status, message, progress, extra = INFERENCE_COORDINATOR.legacy_progress(record)
        if status == "running":
            RUNNING_PPGL_CASES.add(case_dir.name)
            message = "正在进行肿瘤分割"
        elif status in {"completed", "failed", "cancelled", "timed_out"}:
            RUNNING_PPGL_CASES.discard(case_dir.name)
        if status == "completed":
            message = "肿瘤分割完成"
        update_ppgl_status(case_dir, status, message, progress, **extra)

    return observe


def ppgl_status(case_dir: Path) -> dict:
    status_path = case_dir / "ppgl_status.json"
    status = read_json(status_path) if status_path.exists() else {
        "case_id": case_dir.name,
        "task": "ppgl",
        "status": "uploaded",
        "message": "等待启动 PPGL 分割",
        "progress": 0,
        "updated_at": "",
    }
    result_path = case_dir / "output" / "ppgl" / "result.json"
    if result_path.exists() and status.get("status") != "failed":
        completed = dict(status)
        completed.update(
            {
                "status": "completed",
                "message": status.get("message") or "PPGL 肿瘤分割完成",
                "progress": 100,
                "result_path": str(result_path),
            }
        )
        return completed
    return status


def status_with_pipeline_progress(case_dir: Path) -> dict:
    status_path = case_dir / "status.json"
    status = read_json(status_path) if status_path.exists() else {
        "case_id": case_dir.name,
        "status": "unknown",
        "message": "",
        "progress": 0,
        "updated_at": "",
    }

    result_path = case_dir / "output" / "result.json"
    if result_path.exists() and status.get("status") != "failed":
        enriched = dict(status)
        enriched.update(
            {
                "status": "completed",
                "message": status.get("message") or "全器官分割完成",
                "progress": 100,
                "result_path": str(result_path),
            }
        )
        return enriched

    if status.get("status") in {"completed", "failed"}:
        return status

    log_path = case_dir / "output" / "totalseg" / "logs" / "pipeline.log"
    if not log_path.exists():
        status_time = parse_status_time(status.get("updated_at", ""))
        queued_age = (datetime.now() - status_time).total_seconds() if status_time else 0
        if (
            status.get("status") == "queued"
            and case_dir.name not in RUNNING_CASES
            and queued_age > 120
        ):
            stale = dict(status)
            stale.update(
                {
                    "status": "uploaded",
                    "message": "上一次任务未实际启动，请重新点击启动分割",
                    "progress": 0,
                    "stale_queue": True,
                }
            )
            return stale
        return status

    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")[-30000:]
    except OSError:
        return status

    rules = [
        ("[DONE] 2/2", 98, "TotalSegmentator 结果整理完成", "output_done"),
        ("[START] 2/2", 90, "正在合并器官标签并生成结果", "output"),
        ("[DONE] 1/2", 85, "全器官分割完成", "totalseg_done"),
        ("[START] 1/2", 15, "正在进行全器官分割", "totalseg"),
    ]

    enriched = dict(status)
    for pattern, progress, message, stage in rules:
        if pattern in text:
            enriched["status"] = "running"
            enriched["message"] = message
            enriched["progress"] = max(int(enriched.get("progress") or 0), progress)
            enriched["stage"] = stage
            enriched["pipeline_log"] = str(log_path)
            break

    return enriched


def output_file(case_id: str, filename: str) -> Path:
    case_dir = case_dir_for(case_id)
    path = case_dir / "output" / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{filename} not found")
    return path


def result_output_path(case_id: str, key: str, fallback: Path) -> Path:
    case_dir = case_dir_for(case_id)
    result_path = case_dir / "output" / "result.json"
    path = fallback
    if result_path.exists():
        result = read_json(result_path)
        value = result.get("outputs", {}).get(key, "")
        if value:
            path = Path(value)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{path.name} not found")
    return path


def build_ppgl_mesh(output_dir: Path) -> dict:
    mask_path = output_dir / "tumor_mask.nii.gz"
    if not mask_path.is_file():
        raise FileNotFoundError(f"PPGL tumor mask not found: {mask_path}")
    label_map_path = output_dir / "label_map.json"
    write_json(label_map_path, {"label_map": {"0": "background", "1": "tumor:ppgl"}})
    return create_mesh_outputs(mask_path, label_map_path, output_dir)


def finalize_ppgl_worker_output(output_dir: Path) -> dict[str, Any]:
    result_path = output_dir / "result.json"
    result = read_json(result_path)
    mesh_outputs = build_ppgl_mesh(output_dir)
    result.setdefault("outputs", {}).update({
        "label_map_path": str(output_dir / "label_map.json"),
        "mesh_glb_path": mesh_outputs["glb_path"],
        "mesh_manifest_path": mesh_outputs["manifest_path"],
    })
    write_json(result_path, result)
    return mesh_outputs


def ensure_ppgl_mesh(case_dir: Path) -> dict:
    output_dir = case_dir / "output" / "ppgl"
    manifest_path = output_dir / "meshes" / "mesh_manifest.json"
    if manifest_path.is_file():
        return read_json(manifest_path)
    result_path = output_dir / "result.json"
    if not result_path.is_file():
        raise HTTPException(status_code=404, detail="PPGL segmentation result not found")
    mesh_outputs = build_ppgl_mesh(output_dir)
    result = read_json(result_path)
    result.setdefault("outputs", {}).update({
        "label_map_path": str(output_dir / "label_map.json"),
        "mesh_glb_path": mesh_outputs["glb_path"],
        "mesh_manifest_path": mesh_outputs["manifest_path"],
    })
    write_json(result_path, result)
    return read_json(manifest_path)


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    dest_root = dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise HTTPException(status_code=400, detail=f"Invalid zip member: {info.filename}")
                target = (dest_dir / member).resolve()
                if dest_root not in target.parents and target != dest_root:
                    raise HTTPException(status_code=400, detail=f"Invalid zip member: {info.filename}")
            archive.extractall(dest_dir)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid TotalSegmentator zip file")


def normalize_totalseg_existing_dir(totalseg_dir: Path) -> int:
    root_masks = sorted(totalseg_dir.glob("*.nii.gz"))
    if root_masks:
        return len(root_masks)

    nested_masks = sorted(totalseg_dir.rglob("*.nii.gz"))
    for src in nested_masks:
        dst = totalseg_dir / src.name
        if dst.exists():
            raise HTTPException(status_code=400, detail=f"Duplicate mask filename in zip: {src.name}")
        shutil.copy2(src, dst)
    return len(nested_masks)


def run_segmentation_task(
    case_id: str,
    mode: str,
    device: str,
    force: bool,
    totalseg_fast: bool,
    totalseg_fastest: bool,
) -> None:
    case_dir = CASES_DIR / case_id
    input_path = case_dir / "input" / "ct.nii.gz"
    output_dir = case_dir / "output"
    model_lifecycle_log = output_dir / "model_lifecycle.log"

    try:
        RUNNING_CASES.add(case_id)
        with SEGMENTATION_LOCK:
            if force and output_dir.exists():
                for child in output_dir.iterdir():
                    if child.name == "ppgl":
                        continue
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            output_dir.mkdir(parents=True, exist_ok=True)

            update_status(case_dir, "running", "正在进行全器官分割", 10)
            unload_chat_model_for_segmentation(model_lifecycle_log)
            try:
                result = run_single_case(
                    input_path=str(input_path),
                    output_dir=str(output_dir),
                    device=device,
                    case_id=case_id,
                    mode=mode,
                    totalseg_fast=totalseg_fast,
                    totalseg_fastest=totalseg_fastest,
                )
            finally:
                preload_chat_model_after_segmentation(model_lifecycle_log)
            update_status(
                case_dir,
                "completed",
                "全器官分割完成",
                100,
                result=result,
            )
    except Exception as exc:
        error_path = case_dir / "error.log"
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        update_status(
            case_dir,
            "failed",
            "全器官分割失败",
            100,
            error=str(exc),
            error_log=str(error_path),
        )
    finally:
        RUNNING_CASES.discard(case_id)


def run_ppgl_segmentation_task(
    case_id: str,
    device: str,
    force: bool,
) -> None:
    case_dir = CASES_DIR / case_id
    input_path = case_dir / "input" / "ct.nii.gz"
    output_dir = case_dir / "output" / "ppgl"
    log_path = output_dir / "inference.log"
    model_lifecycle_log = output_dir / "model_lifecycle.log"

    try:
        RUNNING_PPGL_CASES.add(case_id)
        with SEGMENTATION_LOCK:
            if force and output_dir.exists():
                shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            inference_script = PPGL_V5_DIR / "infer_single_case.py"
            if not inference_script.is_file():
                raise FileNotFoundError(f"PPGL V5 inference script not found: {inference_script}")
            if not PPGL_V5_CHECKPOINT.is_file():
                raise FileNotFoundError(f"PPGL V5 checkpoint not found: {PPGL_V5_CHECKPOINT}")
            if not PPGL_V5_MODEL_CONFIG.is_file():
                raise FileNotFoundError(f"PPGL V5 model config not found: {PPGL_V5_MODEL_CONFIG}")

            update_ppgl_status(case_dir, "running", "正在进行肿瘤分割", 10)
            cmd = [
                sys.executable,
                str(inference_script),
                "--image",
                str(input_path),
                "--output-dir",
                str(output_dir),
                "--case-id",
                case_id,
                "--checkpoint",
                str(PPGL_V5_CHECKPOINT),
                "--model-config",
                str(PPGL_V5_MODEL_CONFIG),
                "--device",
                device,
            ]
            unload_chat_model_for_segmentation(model_lifecycle_log)
            try:
                completed = subprocess.run(
                    cmd,
                    cwd=str(PPGL_V5_DIR),
                    env=subprocess_runtime_env(),
                    text=True,
                    capture_output=True,
                )
            finally:
                preload_chat_model_after_segmentation(model_lifecycle_log)
            log_path.write_text(
                "COMMAND:\n"
                + " ".join(cmd)
                + "\n\nSTDOUT:\n"
                + completed.stdout
                + "\n\nSTDERR:\n"
                + completed.stderr,
                encoding="utf-8",
            )
            if completed.returncode != 0:
                raise RuntimeError(f"ProgressPatchV5 inference failed. See log: {log_path}")
            result_path = output_dir / "result.json"
            if not result_path.is_file():
                raise RuntimeError("ProgressPatchV5 inference did not produce result.json")
            result = read_json(result_path)
            mesh_outputs = build_ppgl_mesh(output_dir)
            result.setdefault("outputs", {}).update({
                "label_map_path": str(output_dir / "label_map.json"),
                "mesh_glb_path": mesh_outputs["glb_path"],
                "mesh_manifest_path": mesh_outputs["manifest_path"],
            })
            write_json(result_path, result)
            update_ppgl_status(
                case_dir,
                "completed",
                "肿瘤分割完成",
                100,
                result=result,
            )
    except Exception as exc:
        error_path = case_dir / "ppgl_error.log"
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        update_ppgl_status(
            case_dir,
            "failed",
            "PPGL 肿瘤分割失败",
            100,
            error=str(exc),
            error_log=str(error_path),
        )
    finally:
        RUNNING_PPGL_CASES.discard(case_id)


def run_existing_totalseg_task(
    case_id: str,
    mode: str,
    device: str,
    totalseg_existing_dir: str,
) -> None:
    case_dir = CASES_DIR / case_id
    input_path = case_dir / "input" / "ct.nii.gz"
    output_dir = case_dir / "output"
    log_path = output_dir / "run_with_existing_totalseg.log"
    model_lifecycle_log = output_dir / "model_lifecycle.log"

    try:
        RUNNING_CASES.add(case_id)
        with SEGMENTATION_LOCK:
            output_dir.mkdir(parents=True, exist_ok=True)
            update_status(case_dir, "running", "AI 分割正在运行（复用已有 TotalSegmentator 结果）", 10)

            cmd = [
                sys.executable,
                "-m",
                "backend.ct.run_totalseg",
                "--input",
                str(input_path),
                "--output",
                str(output_dir),
                "--case-id",
                case_id,
                "--mode",
                mode,
                "--device",
                device,
                "--totalseg-existing-dir",
                str(totalseg_existing_dir),
                "--force",
            ]
            unload_chat_model_for_segmentation(model_lifecycle_log)
            try:
                completed = subprocess.run(
                    cmd,
                    cwd=str(BASE_DIR / "backend"),
                    env=subprocess_runtime_env(),
                    text=True,
                    capture_output=True,
                )
            finally:
                preload_chat_model_after_segmentation(model_lifecycle_log)
            log_path.write_text(
                "COMMAND:\n"
                + " ".join(cmd)
                + "\n\nSTDOUT:\n"
                + completed.stdout
                + "\n\nSTDERR:\n"
                + completed.stderr,
                encoding="utf-8",
            )
            if completed.returncode != 0:
                raise RuntimeError(f"TotalSegmentator pipeline failed. See log: {log_path}")

            result_path = output_dir / "result.json"
            result = read_json(result_path) if result_path.exists() else {}
            update_status(
                case_dir,
                "completed",
                "TotalSegmentator 结果整理完成（已复用外部结果）",
                100,
                result=result,
            )
    except Exception as exc:
        error_path = case_dir / "error.log"
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        update_status(
            case_dir,
            "failed",
            "TotalSegmentator 结果整理失败",
            100,
            error=str(exc),
            error_log=str(error_path),
        )
    finally:
        RUNNING_CASES.discard(case_id)


@app.get("/")
async def root():
    return {
        "message": "TotalSegmentator full-organ segmentation API is running"
    }


@app.post("/api/cases/upload")
async def upload_ct(request: Request, file: UploadFile = File(...)):
    """
    上传 .nii.gz CT 文件。
    前端字段名必须是 file。
    """

    if not (file.filename or "").endswith(".nii.gz"):
        raise HTTPException(
            status_code=400,
            detail="当前仅支持 .nii.gz 文件"
        )

    case_id = f"case_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    case_dir = CASES_DIR / case_id
    input_dir = case_dir / "input"
    output_dir = case_dir / "output"

    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    input_path = input_dir / "ct.nii.gz"

    with open(input_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    case_info = {
        "case_id": case_id,
        "original_filename": file.filename,
        "input_path": str(input_path),
        "created_at": now_text()
    }
    if getattr(request.state, "auth", {}).get("kind") != "internal":
        case_info["owner_user_id"] = request.state.auth.get("uid")

    status_info = {
        "case_id": case_id,
        "status": "uploaded",
        "message": "CT 文件上传成功",
        "progress": 0,
        "updated_at": now_text()
    }

    write_json(case_dir / "case_info.json", case_info)
    write_json(case_dir / "status.json", status_info)
    update_ppgl_status(case_dir, "uploaded", "等待启动 PPGL 分割", 0)

    return {
        "case_id": case_id,
        "status": "uploaded",
        "message": "CT 文件上传成功"
    }


@app.post("/api/cases/{case_id}/run-with-existing-totalseg")
async def run_case_with_existing_totalseg(
    case_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
    file: UploadFile = File(...),
    totalseg_zip: UploadFile = File(...),
    mode: str = Form("full_total"),
    device: str = Form("cuda"),
):
    if not (file.filename or "").endswith(".nii.gz"):
        raise HTTPException(status_code=400, detail="当前仅支持 ct.nii.gz")
    if not (totalseg_zip.filename or "").endswith(".zip"):
        raise HTTPException(status_code=400, detail="totalseg_zip must be a .zip file")
    if mode not in {"jetson_fast", "abdomen", "full_total"}:
        raise HTTPException(status_code=400, detail="mode must be jetson_fast, abdomen, or full_total")
    if case_id in RUNNING_CASES:
        raise HTTPException(status_code=409, detail="Case is running")

    case_dir = validated_case_dir_path(case_id)
    input_dir = case_dir / "input"
    output_dir = case_dir / "output"
    totalseg_existing_dir = input_dir / "totalseg_existing"
    ct_path = input_dir / "ct.nii.gz"
    zip_path = input_dir / "totalseg_existing.zip"

    if output_dir.exists():
        for child in output_dir.iterdir():
            if child.name == "ppgl":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    if totalseg_existing_dir.exists():
        shutil.rmtree(totalseg_existing_dir)
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(ct_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    with open(zip_path, "wb") as buffer:
        shutil.copyfileobj(totalseg_zip.file, buffer)

    safe_extract_zip(zip_path, totalseg_existing_dir)
    mask_count = normalize_totalseg_existing_dir(totalseg_existing_dir)
    if mask_count <= 0:
        raise HTTPException(status_code=400, detail="No .nii.gz TotalSegmentator masks found in zip")

    case_info = {
        "case_id": case_id,
        "original_filename": file.filename,
        "input_path": str(ct_path),
        "totalseg_zip_filename": totalseg_zip.filename,
        "totalseg_existing_dir": str(totalseg_existing_dir),
        "totalseg_mask_count": mask_count,
        "mode": mode,
        "device": device,
        "created_at": now_text(),
        "source": "external_totalseg_zip",
    }
    if getattr(request.state, "auth", {}).get("kind") != "internal":
        case_info["owner_user_id"] = request.state.auth.get("uid")
    write_json(case_dir / "case_info.json", case_info)

    del background_tasks
    try:
        INFERENCE_COORDINATOR.submit_totalsegmentator(
            case_id=case_id,
            image_path=ct_path,
            output_dir=output_dir,
            mode=mode,
            device=device,
            existing_masks_dir=totalseg_existing_dir,
            force=True,
            observer=organ_task_observer(case_dir),
            before_inference=unload_chat_model_for_segmentation,
            after_inference=preload_chat_model_after_segmentation,
        )
    except WorkerExecutionError as exc:
        raise HTTPException(
            status_code=409 if exc.retryable else 422,
            detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
        ) from exc
    return read_json(case_dir / "status.json")


@app.get("/api/cases")
async def list_cases(request: Request):
    rows = []
    for case_dir in sorted(CASES_DIR.iterdir(), reverse=True):
        if not case_dir.is_dir():
            continue
        status_path = case_dir / "status.json"
        case_info_path = case_dir / "case_info.json"
        status = status_with_pipeline_progress(case_dir) if status_path.exists() else {}
        case_info = read_json(case_info_path) if case_info_path.exists() else {}
        auth = request.state.auth
        if auth.get("kind") != "internal" and auth.get("role") != "ADMIN":
            if case_info.get("owner_user_id") != auth.get("uid"):
                continue
        rows.append(
            {
                "case_id": case_dir.name,
                "status": status.get("status", "unknown"),
                "message": status.get("message", ""),
                "progress": status.get("progress", 0),
                "organ_status": status,
                "ppgl_status": ppgl_status(case_dir),
                "original_filename": case_info.get("original_filename", ""),
                "created_at": case_info.get("created_at", ""),
                "updated_at": status.get("updated_at", ""),
            }
        )
    return {"cases": rows}


@app.delete("/api/cases/{case_id}")
async def delete_case(case_id: str):
    case_dir = case_dir_for(case_id)
    status = status_with_pipeline_progress(case_dir)

    if (
        case_id in RUNNING_CASES
        or case_id in RUNNING_PPGL_CASES
        or status.get("status") in {"queued", "running"}
        or ppgl_status(case_dir).get("status") in {"queued", "running"}
    ):
        raise HTTPException(status_code=409, detail="Case is running, cannot delete")

    try:
        shutil.rmtree(case_dir)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete case: {exc}")

    return {
        "case_id": case_id,
        "status": "deleted",
        "message": "病例已删除",
    }


@app.patch("/api/cases/{case_id}/case-id")
async def rename_case(case_id: str, payload: RenameCaseRequest):
    case_dir = case_dir_for(case_id)
    status = status_with_pipeline_progress(case_dir)
    new_case_id = payload.new_case_id.strip()
    new_case_dir = validated_case_dir_path(new_case_id)

    if (
        case_id in RUNNING_CASES
        or case_id in RUNNING_PPGL_CASES
        or status.get("status") in {"queued", "running"}
        or ppgl_status(case_dir).get("status") in {"queued", "running"}
    ):
        raise HTTPException(status_code=409, detail="Case is running, cannot rename")
    if new_case_id != payload.new_case_id:
        raise HTTPException(status_code=400, detail="病例编号前后不能包含空格")
    if new_case_id == case_id:
        return {
            "case_id": case_id,
            "new_case_id": new_case_id,
            "status": "unchanged",
            "message": "病例编号未变化",
        }
    if new_case_dir.exists():
        raise HTTPException(status_code=409, detail="病例编号已存在")

    old_case_dir = case_dir
    try:
        case_dir.rename(new_case_dir)
        rewrite_case_metadata_files(new_case_dir, case_id, new_case_id, old_case_dir)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to rename case: {exc}")

    return {
        "case_id": new_case_id,
        "old_case_id": case_id,
        "status": "renamed",
        "message": "病例编号已修改",
    }


@app.post("/api/cases/{case_id}/segment")
@app.post("/api/cases/{case_id}/segment/organs")
async def start_segmentation(
    case_id: str,
    background_tasks: BackgroundTasks,
    mode: str = "full_total",
    device: str = "cuda",
    force: bool = False,
    totalseg_fast: bool = False,
    totalseg_fastest: bool = False,
):
    case_dir = case_dir_for(case_id)
    input_path = case_dir / "input" / "ct.nii.gz"
    result_path = case_dir / "output" / "result.json"

    if not input_path.exists():
        raise HTTPException(status_code=404, detail="Input CT not found")
    if mode not in {"jetson_fast", "abdomen", "full_total"}:
        raise HTTPException(status_code=400, detail="mode must be jetson_fast, abdomen, or full_total")
    if case_id in RUNNING_CASES:
        return read_json(case_dir / "status.json")
    if result_path.exists() and not force:
        update_status(case_dir, "completed", "全器官分割完成", 100)
        return read_json(case_dir / "status.json")

    del background_tasks
    try:
        INFERENCE_COORDINATOR.submit_totalsegmentator(
            case_id=case_id,
            image_path=input_path,
            output_dir=case_dir / "output",
            mode=mode,
            device=device,
            fast=totalseg_fast,
            fastest=totalseg_fastest,
            force=force,
            observer=organ_task_observer(case_dir),
            before_inference=unload_chat_model_for_segmentation,
            after_inference=preload_chat_model_after_segmentation,
        )
    except WorkerExecutionError as exc:
        raise HTTPException(
            status_code=409 if exc.retryable else 422,
            detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
        ) from exc
    return read_json(case_dir / "status.json")


@app.post("/api/cases/{case_id}/segment/ppgl")
async def start_ppgl_segmentation(
    case_id: str,
    background_tasks: BackgroundTasks,
    device: str = "cuda:0",
    force: bool = False,
):
    case_dir = case_dir_for(case_id)
    input_path = case_dir / "input" / "ct.nii.gz"
    result_path = case_dir / "output" / "ppgl" / "result.json"
    if not input_path.is_file():
        raise HTTPException(status_code=404, detail="Input CT not found")
    if case_id in RUNNING_PPGL_CASES:
        return ppgl_status(case_dir)
    if result_path.is_file() and not force:
        update_ppgl_status(case_dir, "completed", "PPGL 肿瘤分割已完成", 100)
        return ppgl_status(case_dir)

    del background_tasks
    output_dir = case_dir / "output" / "ppgl"
    if force and output_dir.exists():
        shutil.rmtree(output_dir)
    try:
        INFERENCE_COORDINATOR.submit_ppgl(
            case_id=case_id,
            image_path=input_path,
            output_dir=output_dir,
            device=device,
            observer=ppgl_task_observer(case_dir),
            before_inference=unload_chat_model_for_segmentation,
            after_inference=preload_chat_model_after_segmentation,
            result_hook=finalize_ppgl_worker_output,
        )
    except WorkerExecutionError as exc:
        raise HTTPException(
            status_code=409 if exc.retryable else 422,
            detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
        ) from exc
    return ppgl_status(case_dir)


@app.post("/api/cases/{case_id}/segment/cancel")
@app.post("/api/cases/{case_id}/segment/organs/cancel")
async def cancel_segmentation(case_id: str):
    case_dir = case_dir_for(case_id)
    try:
        record = INFERENCE_COORDINATOR.cancel(case_id, TaskType.TOTALSEGMENTATOR)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail="该病例没有可取消的全器官分割任务") from exc
    organ_task_observer(case_dir)(record)
    return read_json(case_dir / "status.json")


@app.post("/api/cases/{case_id}/segment/ppgl/cancel")
async def cancel_ppgl_segmentation(case_id: str):
    case_dir = case_dir_for(case_id)
    try:
        record = INFERENCE_COORDINATOR.cancel(case_id, TaskType.PPGL_V5)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail="该病例没有可取消的 PPGL 分割任务") from exc
    ppgl_task_observer(case_dir)(record)
    return ppgl_status(case_dir)


@app.get("/api/cases/{case_id}/status")
async def get_case_status(case_id: str):
    case_dir = case_dir_for(case_id)
    organ = status_with_pipeline_progress(case_dir)
    return {
        **organ,
        "organ": organ,
        "ppgl": ppgl_status(case_dir),
    }


@app.get("/api/cases/{case_id}/result")
async def get_case_result(case_id: str):
    return read_json(output_file(case_id, "result.json"))


@app.get("/api/cases/{case_id}/ppgl/result")
async def get_case_ppgl_result(case_id: str):
    case_dir = case_dir_for(case_id)
    return read_json(case_dir / "output" / "ppgl" / "result.json")


@app.get("/api/cases/{case_id}/ppgl/mask")
async def get_case_ppgl_mask(case_id: str):
    case_dir = case_dir_for(case_id)
    mask_path = case_dir / "output" / "ppgl" / "tumor_mask.nii.gz"
    if not mask_path.is_file():
        raise HTTPException(status_code=404, detail="PPGL tumor mask not found")
    return FileResponse(
        mask_path,
        media_type="application/gzip",
        filename=f"{case_id}_ppgl_tumor.nii.gz",
    )


@app.get("/api/cases/{case_id}/ppgl/mesh")
async def get_case_ppgl_mesh(case_id: str):
    case_dir = case_dir_for(case_id)
    ensure_ppgl_mesh(case_dir)
    mesh_path = case_dir / "output" / "ppgl" / "meshes" / "scene.glb"
    if not mesh_path.is_file():
        raise HTTPException(status_code=404, detail="PPGL 3D mesh not found")
    return FileResponse(mesh_path, media_type="model/gltf-binary", filename=f"{case_id}_ppgl.glb")


@app.get("/api/cases/{case_id}/ppgl/mesh/{label_id}")
async def get_case_ppgl_high_mesh(case_id: str, label_id: int):
    if label_id < 1:
        raise HTTPException(status_code=400, detail="Invalid mesh label")
    case_dir = case_dir_for(case_id)
    ensure_ppgl_mesh(case_dir)
    mesh_path = case_dir / "output" / "ppgl" / "meshes" / "organs" / f"{label_id}.glb"
    if not mesh_path.is_file():
        raise HTTPException(status_code=404, detail="PPGL high-resolution mesh not found")
    return FileResponse(mesh_path, media_type="model/gltf-binary", filename=f"{case_id}_ppgl_{label_id}.glb")


@app.get("/api/cases/{case_id}/ppgl/mesh-manifest")
async def get_case_ppgl_mesh_manifest(case_id: str):
    return ensure_ppgl_mesh(case_dir_for(case_id))


@app.get("/api/cases/{case_id}/overlay")
async def get_case_overlay(case_id: str):
    return FileResponse(output_file(case_id, "overlay.png"), media_type="image/png")


@app.get("/api/cases/{case_id}/slice-gallery")
async def get_case_slice_gallery(case_id: str):
    case_dir = case_dir_for(case_id)
    manifest_path = result_output_path(
        case_id,
        "slice_gallery_path",
        case_dir / "output" / "slices" / "slice_gallery.json",
    )
    return read_json(manifest_path)


@app.get("/api/cases/{case_id}/slice-gallery/{filename}")
async def get_case_slice_image(case_id: str, filename: str):
    if Path(filename).name != filename or not filename.endswith(".png"):
        raise HTTPException(status_code=400, detail="Invalid slice filename")
    case_dir = case_dir_for(case_id)
    image_path = case_dir / "output" / "slices" / filename
    if not image_path.exists():
        raise HTTPException(status_code=404, detail=f"{filename} not found")
    return FileResponse(image_path, media_type="image/png")


@app.get("/api/cases/{case_id}/ct")
@app.get("/api/cases/{case_id}/ct.nii.gz")
async def get_case_ct(case_id: str):
    case_dir = case_dir_for(case_id)
    ct_path = case_dir / "input" / "ct.nii.gz"
    if not ct_path.is_file():
        raise HTTPException(status_code=404, detail="CT not found")
    return FileResponse(
        ct_path,
        media_type="application/gzip",
        filename=f"{case_id}_ct.nii.gz",
    )


@app.get("/api/cases/{case_id}/mask")
@app.get("/api/cases/{case_id}/mask.nii.gz")
async def get_case_mask(case_id: str):
    return FileResponse(
        output_file(case_id, "mask.nii.gz"),
        media_type="application/gzip",
        filename=f"{case_id}_mask.nii.gz",
    )


@app.get("/api/cases/{case_id}/mesh")
async def get_case_mesh(case_id: str):
    case_dir = case_dir_for(case_id)
    mesh_path = result_output_path(
        case_id,
        "mesh_glb_path",
        case_dir / "output" / "meshes" / "scene.glb",
    )
    return FileResponse(
        mesh_path,
        media_type="model/gltf-binary",
        filename=f"{case_id}_scene.glb",
    )


@app.get("/api/cases/{case_id}/mesh/{label_id}")
async def get_case_organ_mesh(case_id: str, label_id: int):
    if label_id < 1:
        raise HTTPException(status_code=400, detail="Invalid mesh label")
    mesh_path = case_dir_for(case_id) / "output" / "meshes" / "organs" / f"{label_id}.glb"
    if not mesh_path.is_file():
        raise HTTPException(status_code=404, detail="High-resolution organ mesh not found")
    return FileResponse(
        mesh_path,
        media_type="model/gltf-binary",
        filename=f"{case_id}_organ_{label_id}.glb",
    )


@app.get("/api/cases/{case_id}/mesh-manifest")
async def get_case_mesh_manifest(case_id: str):
    case_dir = case_dir_for(case_id)
    manifest_path = result_output_path(
        case_id,
        "mesh_manifest_path",
        case_dir / "output" / "meshes" / "mesh_manifest.json",
    )
    return read_json(manifest_path)


@app.post("/api/llm/chat/stream")
async def stream_generic_llm_chat(payload: GenericLlmStreamChatRequest):
    prompt = build_generic_llm_prompt(payload)

    def generate():
        answer_parts: list[str] = []
        try:
            for delta in stream_openai_text(prompt, max_output_tokens=payload.max_tokens):
                answer_parts.append(delta)
                yield stream_event({"type": "delta", "text": delta})

            answer = "".join(answer_parts).strip()
            if not answer:
                raise RuntimeError("OpenAI-compatible API returned an empty answer")

            yield stream_event(
                {
                    "type": "done",
                    "answer": answer,
                    "metadata": {
                        "provider": llm_provider(),
                        "stream": True,
                    },
                }
            )
        except RuntimeError as exc:
            yield stream_event({"type": "error", "detail": str(exc)})
        except Exception as exc:
            yield stream_event({"type": "error", "detail": f"AI 问答失败：{exc}"})

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/rag/search")
def search_rag_knowledge(payload: RagSearchRequest):
    try:
        from rag.vector_store import search_knowledge_with_metadata

        search_payload = search_knowledge_with_metadata(
            payload.query,
            payload.top_k,
            retrieve_k=payload.retrieve_k,
            retrieval_mode=payload.retrieval_mode,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"RAG 检索失败：{exc}")
    return {
        "query": payload.query.strip(),
        "top_k": payload.top_k,
        "results": search_payload["results"],
        "metadata": search_payload["metadata"],
    }


@app.post("/api/rag/query")
def query_rag_knowledge(payload: RagQueryRequest):
    try:
        from rag.prompt_builder import build_knowledge_rag_prompt, citation_payload, evidence_assessment
        from rag.vector_store import search_knowledge_with_metadata

        search_payload = search_knowledge_with_metadata(
            payload.question,
            payload.top_k,
            retrieve_k=payload.retrieve_k,
            retrieval_mode=payload.retrieval_mode,
        )
        results = search_payload["results"]
        prompt = build_knowledge_rag_prompt(payload.question, results)
        answer, metadata = call_openai_text(prompt, max_output_tokens=payload.max_tokens)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"RAG 问答失败：{exc}")
    return {
        "question": payload.question.strip(),
        "answer": answer,
        "citations": citation_payload(results),
        "metadata": {
            **metadata,
            "retrieval": search_payload["metadata"]["pipeline"],
            "retrieval_latency_ms": search_payload["metadata"]["latency_ms"],
            "top_k": payload.top_k,
            "evidence_assessment": evidence_assessment(answer, results),
        },
    }


@app.post("/api/cases/{case_id}/rag/query")
def query_case_rag_knowledge(case_id: str, payload: RagQueryRequest):
    case_dir = case_dir_for(case_id)
    try:
        from rag.case_context import load_case_context
        from rag.prompt_builder import build_case_rag_prompt, citation_payload, evidence_assessment
        from rag.vector_store import search_knowledge_with_metadata

        case_context = load_case_context(case_dir, payload.question)
        search_payload = search_knowledge_with_metadata(
            payload.question,
            payload.top_k,
            retrieve_k=payload.retrieve_k,
            retrieval_mode=payload.retrieval_mode,
        )
        results = search_payload["results"]
        prompt = build_case_rag_prompt(payload.question, case_context, results)
        answer, metadata = call_openai_text(prompt, max_output_tokens=payload.max_tokens)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"病例 RAG 问答失败：{exc}")
    return {
        "case_id": case_id,
        "question": payload.question.strip(),
        "answer": answer,
        "case_context": case_context,
        "citations": citation_payload(results),
        "metadata": {
            **metadata,
            "retrieval": search_payload["metadata"]["pipeline"],
            "retrieval_latency_ms": search_payload["metadata"]["latency_ms"],
            "top_k": payload.top_k,
            "case_aware": True,
            "evidence_assessment": evidence_assessment(answer, results),
        },
    }


@app.get("/api/cases/{case_id}/metrics")
async def get_case_metrics(case_id: str):
    result = read_json(output_file(case_id, "result.json"))
    metrics_path = Path(result.get("outputs", {}).get("clinical_metrics_path", ""))
    if not metrics_path.exists():
        raise HTTPException(status_code=404, detail="clinical_metrics.json not found")
    return read_json(metrics_path)


@app.get("/api/cases/{case_id}/llm-context")
async def get_case_llm_context(case_id: str):
    result = read_json(output_file(case_id, "result.json"))
    context_path = Path(result.get("outputs", {}).get("llm_context_path", ""))
    if not context_path.exists():
        raise HTTPException(status_code=404, detail="llm_context.json not found")
    return read_json(context_path)


@app.get("/api/cases/{case_id}/risk")
async def get_case_risk(case_id: str):
    result = read_json(output_file(case_id, "result.json"))
    risk = result.get("risk_assessment") or result.get("clinical_metrics", {}).get("risk_assessment")
    if not risk:
        raise HTTPException(status_code=404, detail="risk_assessment not found")
    return risk


@app.get("/api/cases/{case_id}/label-map")
async def get_case_label_map(case_id: str):
    result = read_json(output_file(case_id, "result.json"))
    label_map_path = Path(result.get("outputs", {}).get("label_map_path", ""))
    if not label_map_path.exists():
        raise HTTPException(status_code=404, detail="label_map.json not found")
    return read_json(label_map_path)


# Formal enterprise adapters. They keep the existing LLM/RAG implementation while
# enforcing the /api/v1 Cookie, CSRF and case-permission boundary.
@app.get("/api/v1/llm/health")
async def get_v1_llm_health(request: Request):
    del request
    config = ollama_chat_config()
    if config is None:
        return {"status": "disabled", "model": "", "message": "未配置本地 Ollama 模型"}
    try:
        with urllib.request.urlopen(f"{config['base_url']}/api/tags", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {"status": "unavailable", "model": config["model"], "message": "无法连接本地 Ollama"}
    models = {str(item.get("name", "")) for item in payload.get("models", [])}
    installed = config["model"] in models or f"{config['model']}:latest" in models
    return {
        "status": "ready" if installed else "model_missing",
        "model": config["model"],
        "installed_models": sorted(name for name in models if name),
    }


@app.post("/api/v1/llm/chat/stream")
async def stream_v1_llm_chat(payload: GenericLlmStreamChatRequest, request: Request):
    context = getattr(request.state, "enterprise_auth", None)
    if context is None:
        raise AuthenticationRequired("请先登录")
    question = payload.question.strip()
    history: list[dict[str, str]] = []
    for item in payload.history[-10:]:
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()[:2000]
        if role in {"user", "assistant"} and content:
            history.append({"role": role, "content": content})
    inspected = question + "\n" + "\n".join(item["content"] for item in history)
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    if contains_sensitive_text(inspected):
        ENTERPRISE_SERVICE.record_audit(context.user.id, "ai.general.rejected", "failure", "assistant")
        raise ValidationRejected("输入不能包含患者身份信息、病例号或本机路径")
    prompt = (
        "你是医学知识与影像工作流程辅助助手，请使用中文谨慎回答。"
        "不得索取或输出患者身份信息，不替代医生诊断、分级、用药和治疗决策；"
        "证据不足时明确说明，并建议查阅经审核医学知识库或咨询相应专科。\n\n"
        f"【最近对话】\n{json.dumps(history, ensure_ascii=False)}\n\n"
        f"【当前问题】\n{question}"
    )
    ENTERPRISE_SERVICE.record_audit(context.user.id, "ai.general.start", "success", "assistant")

    def generate():
        generated = ""
        sent = 0
        try:
            for delta in stream_ollama_chat_text(prompt, max_output_tokens=payload.max_tokens):
                generated += delta
                if contains_sensitive_text(generated):
                    raise RuntimeError("模型输出触发隐私保护，已停止返回")
                safe_length = max(0, len(generated) - 24)
                if safe_length > sent:
                    yield stream_event({"type": "delta", "text": generated[sent:safe_length]})
                    sent = safe_length
            if contains_sensitive_text(generated):
                raise RuntimeError("模型输出触发隐私保护，已停止返回")
            if len(generated) > sent:
                yield stream_event({"type": "delta", "text": generated[sent:]})
            ENTERPRISE_SERVICE.record_audit(context.user.id, "ai.general.complete", "success", "assistant")
            yield stream_event({"type": "done", "metadata": {"provider": "ollama", "stream": True}})
        except Exception as exc:
            ENTERPRISE_SERVICE.record_audit(
                context.user.id,
                "ai.general.complete",
                "failure",
                "assistant",
                details={"error": type(exc).__name__},
            )
            yield stream_event({"type": "error", "detail": str(exc)})

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _v1_rag_user(request: Request):
    context = getattr(request.state, "enterprise_auth", None)
    if context is None:
        raise AuthenticationRequired("请先登录")
    return context.user


def _reject_rag_privacy(text: str) -> None:
    if contains_sensitive_text(text):
        raise ValidationRejected("知识库问题不能包含患者身份信息、病例号或本机路径")


def _formal_rag_search(payload: RagSearchRequest) -> dict[str, Any]:
    from rag.vector_store import search_knowledge_with_metadata
    return search_knowledge_with_metadata(
        payload.query,
        payload.top_k,
        retrieve_k=payload.retrieve_k,
        retrieval_mode=payload.retrieval_mode,
    )


@app.post("/api/v1/rag/search")
def search_v1_rag_knowledge(payload: RagSearchRequest, request: Request):
    user = _v1_rag_user(request)
    try:
        _reject_rag_privacy(payload.query)
        search_payload = _formal_rag_search(payload)
    except FileNotFoundError as exc:
        ENTERPRISE_SERVICE.record_audit(user.id, "rag.search", "failure", "knowledge", details={"error": type(exc).__name__})
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        ENTERPRISE_SERVICE.record_audit(user.id, "rag.search", "failure", "knowledge", details={"error": type(exc).__name__})
        raise
    ENTERPRISE_SERVICE.record_audit(user.id, "rag.search", "success", "knowledge", details={"top_k": payload.top_k})
    return {"query": payload.query.strip(), "top_k": payload.top_k, **search_payload}


def _rag_answer(payload: RagQueryRequest, *, case_context: dict[str, Any] | None = None) -> dict[str, Any]:
    from rag.prompt_builder import build_case_rag_prompt, build_knowledge_rag_prompt, citation_payload, evidence_assessment
    from rag.vector_store import search_knowledge_with_metadata
    search_payload = search_knowledge_with_metadata(
        payload.question,
        payload.top_k,
        retrieve_k=payload.retrieve_k,
        retrieval_mode=payload.retrieval_mode,
    )
    results = search_payload["results"]
    prompt = (
        build_case_rag_prompt(payload.question, case_context, results)
        if case_context is not None
        else build_knowledge_rag_prompt(payload.question, results)
    )
    answer, metadata = call_ollama_chat_text(prompt, max_output_tokens=payload.max_tokens)
    if contains_sensitive_text(answer):
        raise ValidationRejected("模型输出触发隐私保护")
    return {
        "question": payload.question.strip(), "answer": answer,
        "citations": citation_payload(results),
        "metadata": {**metadata, "retrieval": search_payload["metadata"]["pipeline"],
            "retrieval_latency_ms": search_payload["metadata"]["latency_ms"], "top_k": payload.top_k,
            "approved_documents_only": True, "evidence_assessment": evidence_assessment(answer, results)},
    }


@app.post("/api/v1/rag/query")
def query_v1_rag_knowledge(payload: RagQueryRequest, request: Request):
    user = _v1_rag_user(request)
    try:
        _reject_rag_privacy(payload.question)
        result = _rag_answer(payload)
    except FileNotFoundError as exc:
        ENTERPRISE_SERVICE.record_audit(user.id, "rag.query", "failure", "knowledge", details={"error": type(exc).__name__})
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        ENTERPRISE_SERVICE.record_audit(user.id, "rag.query", "failure", "knowledge", details={"error": type(exc).__name__})
        raise
    ENTERPRISE_SERVICE.record_audit(user.id, "rag.query", "success", "knowledge", details={"citations": len(result["citations"])})
    return result


@app.post("/api/v1/cases/{case_id}/rag/query")
def query_v1_case_rag_knowledge(case_id: str, payload: RagQueryRequest, request: Request):
    user = _v1_rag_user(request)
    try:
        _reject_rag_privacy(payload.question)
        ENTERPRISE_SERVICE.authorize_case(user, case_id, "read")
        record = ENTERPRISE_SERVICE.get_case_record(case_id)
        safe_context = UNIFIED_REPORT_SERVICE.safe_context(case_id, record.case_type)
        result = _rag_answer(payload, case_context=safe_context)
    except UnifiedReportError as exc:
        ENTERPRISE_SERVICE.record_audit(user.id, "rag.case.query", "failure", "case", case_id, {"error": type(exc).__name__})
        raise HTTPException(status_code=422, detail="请先生成病例统一报告") from exc
    except FileNotFoundError as exc:
        ENTERPRISE_SERVICE.record_audit(user.id, "rag.case.query", "failure", "case", case_id, {"error": type(exc).__name__})
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        ENTERPRISE_SERVICE.record_audit(user.id, "rag.case.query", "failure", "case", case_id, {"error": type(exc).__name__})
        raise
    ENTERPRISE_SERVICE.record_audit(user.id, "rag.case.query", "success", "case", case_id, {"citations": len(result["citations"])})
    return {"case_id": case_id, "case_context": safe_context, **result}


from backend.glioma.router import build_glioma_router
from backend.enterprise.router import build_enterprise_router
from backend.enterprise.workflow_router import build_enterprise_workflow_router
from backend.report.router import build_report_router
from backend.rag.knowledge_admin import build_knowledge_admin_router, build_knowledge_submission_router


app.include_router(build_report_router(case_dir_for))
app.include_router(build_enterprise_router(ENTERPRISE_SERVICE, ENTERPRISE_STORAGE))
app.include_router(build_knowledge_submission_router(ENTERPRISE_SERVICE))
app.include_router(build_knowledge_admin_router(ENTERPRISE_SERVICE))
app.include_router(
    build_enterprise_workflow_router(
        service=ENTERPRISE_SERVICE,
        storage=ENTERPRISE_STORAGE,
        settings=SETTINGS,
        coordinator=INFERENCE_COORDINATOR,
        execution_lock=SEGMENTATION_LOCK,
        before_inference=unload_chat_model_for_segmentation,
        after_inference=preload_chat_model_after_segmentation,
        ct_start_organs=start_segmentation,
        ct_start_ppgl=start_ppgl_segmentation,
        ct_cancel_organs=cancel_segmentation,
        ct_cancel_ppgl=cancel_ppgl_segmentation,
        ct_status=get_case_status,
    )
)
if os.environ.get("PPGL_ENABLE_LEGACY_BRAIN_API", "false").strip().lower() in {"1", "true", "yes", "on"}:
    app.include_router(
        build_glioma_router(
            DATA_ROOT,
            SEGMENTATION_LOCK,
            unload_chat_model_for_segmentation,
            preload_chat_model_after_segmentation,
            INFERENCE_COORDINATOR.queue,
            INFERENCE_COORDINATOR.runner,
        )
    )
