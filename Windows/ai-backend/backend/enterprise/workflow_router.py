from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from threading import Lock
from typing import Any, Awaitable, Callable
import uuid

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from backend.config import AppSettings
from backend.domain.task_contract import TaskType
from backend.glioma.metrics_service import MetricsService
from backend.glioma.nnunet_service import NnunetInferenceConfig, NnunetInferenceService
from backend.glioma.upload_service import BratsUploadService
from backend.glioma.visualization_service import VisualizationService
from backend.services.inference_coordinator import InferenceCoordinator
from backend.report_agent import stream_ollama_chat_text

from .models import CaseType, UserRole
from .privacy import contains_sensitive_text
from .report_service import UnifiedReportError, UnifiedReportService
from .service import AuthContext, EnterpriseService, ResourceConflict, ValidationRejected
from .storage import UnifiedCaseStorage


class MriPreflightRequest(BaseModel):
    modalities: dict[str, int]


class CtSegmentationRequest(BaseModel):
    mode: str = "full_total"
    device: str = "cuda"
    force: bool = False
    fast: bool = False
    fastest: bool = False


class PpglSegmentationRequest(BaseModel):
    device: str = "cuda:0"
    force: bool = False


class DoctorReviewRequest(BaseModel):
    opinion: str = Field(min_length=1, max_length=4000)
    doctor_name: str = Field(min_length=1, max_length=80)
    review_date: str = Field(min_length=8, max_length=32)

    @field_validator("opinion")
    @classmethod
    def reject_patient_identifiers(cls, value: str) -> str:
        if contains_sensitive_text(value):
            raise ValueError("复核意见不能包含患者姓名、手机号、证件号或病历号")
        return value


class CaseAssistantRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    history: list[dict[str, Any]] = Field(default_factory=list)
    max_tokens: int = Field(default=900, ge=64, le=1800)

    @field_validator("question")
    @classmethod
    def reject_sensitive_question(cls, value: str) -> str:
        if contains_sensitive_text(value):
            raise ValueError("问题中不能包含患者身份信息、病例号或本机路径")
        return value.strip()


def _auth(request: Request) -> AuthContext:
    context = getattr(request.state, "enterprise_auth", None)
    if context is None:
        raise HTTPException(status_code=401, detail={"code": "AUTHENTICATION_REQUIRED", "message": "请先登录"})
    return context


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name, "true" if default else "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip())
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)).strip())
    except ValueError:
        return default


def _service_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "code": getattr(exc, "code", type(exc).__name__),
            "message": getattr(exc, "message", str(exc)),
            "details": getattr(exc, "details", {}),
        },
    )


def build_enterprise_workflow_router(
    *,
    service: EnterpriseService,
    storage: UnifiedCaseStorage,
    settings: AppSettings,
    coordinator: InferenceCoordinator,
    execution_lock: Lock,
    before_inference: Callable[[Path | None], None] | None,
    after_inference: Callable[[Path | None], None] | None,
    ct_start_organs: Callable[..., Awaitable[dict[str, Any]]],
    ct_start_ppgl: Callable[..., Awaitable[dict[str, Any]]],
    ct_cancel_organs: Callable[..., Awaitable[dict[str, Any]]],
    ct_cancel_ppgl: Callable[..., Awaitable[dict[str, Any]]],
    ct_status: Callable[..., Awaitable[dict[str, Any]]],
) -> APIRouter:
    brain_case_service = storage.brain_service
    upload_service = BratsUploadService(
        brain_case_service,
        _int_env("PPGL_GLIOMA_MAX_UPLOAD_MB", 2048) * 1024 * 1024,
    )
    metrics_service = MetricsService(brain_case_service)
    report_service = UnifiedReportService(storage)
    visualization_service = VisualizationService(brain_case_service)
    inference_service = NnunetInferenceService(
        brain_case_service,
        NnunetInferenceConfig(
            model_dir=settings.brain_model_dir,
            dataset="Dataset001_BrainTumour",
            configuration="3d_fullres",
            fold=0,
            checkpoint="checkpoint_best.pth",
            trainer="nnUNetTrainer",
            plans="nnUNetPlans",
            device=os.environ.get("PPGL_GLIOMA_DEVICE", "cuda").strip() or "cuda",
            step_size=_float_env("PPGL_GLIOMA_STEP_SIZE", 0.5),
            disable_tta=_bool_env("PPGL_GLIOMA_DISABLE_TTA", False),
            preprocess_processes=_int_env("PPGL_GLIOMA_PREPROCESS_PROCESSES", 2),
            export_processes=_int_env("PPGL_GLIOMA_EXPORT_PROCESSES", 2),
            timeout_seconds=_int_env("PPGL_GLIOMA_TIMEOUT_SECONDS", settings.task_timeout_seconds),
            enabled=_bool_env("PPGL_GLIOMA_ENABLED", False),
            gpu_lock_file=settings.gpu_lock_file,
        ),
        metrics_service=metrics_service,
        visualization_service=visualization_service,
        execution_lock=execution_lock,
        before_inference=before_inference,
        after_inference=after_inference,
        unified_queue=coordinator.queue,
        process_runner=coordinator.runner,
        data_root=settings.data_root,
    )
    router = APIRouter(prefix="/api/v1/cases", tags=["case-workflows-v1"])

    @router.get("/models/brain")
    async def brain_model_status(request: Request) -> dict[str, Any]:
        _auth(request)
        try:
            return await run_in_threadpool(inference_service.model_status, False)
        except Exception as exc:
            raise _service_error(exc) from exc

    def authorize(request: Request, case_id: str, case_type: CaseType, permission: str) -> AuthContext:
        context = _auth(request)
        service.authorize_case(context.user, case_id, permission)
        record = service.get_case_record(case_id)
        if record.case_type != case_type.value:
            raise ValidationRejected("病例类型与所请求的工作流不匹配")
        return context

    def sync_task(case_id: str, task_type: TaskType, task: dict[str, Any] | None) -> None:
        if not task:
            return
        task_id = str(task.get("task_id") or task.get("run_id") or "")
        if not task_id:
            return
        service.upsert_task({
            "task_id": task_id,
            "case_id": case_id,
            "task_type": task_type.value,
            "status": task.get("status", "queued"),
            "progress": task.get("progress", 0),
            "error_code": task.get("error_code"),
            "retryable": task.get("retryable", False),
        })

    @router.put("/{case_id}/ct/image")
    async def upload_ct(case_id: str, request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
        context = authorize(request, case_id, CaseType.PPGL_CT, "edit")
        if not (file.filename or "").lower().endswith(".nii.gz"):
            raise HTTPException(status_code=400, detail={"code": "INVALID_CT_FILE", "message": "仅支持 .nii.gz CT 文件"})
        case_dir = storage.case_root(case_id, CaseType.PPGL_CT)
        input_path = case_dir / "input" / "ct.nii.gz"
        temporary = input_path.with_name(f".{input_path.name}.{uuid.uuid4().hex}.upload")
        max_bytes = _int_env("PPGL_CT_MAX_UPLOAD_MB", 4096) * 1024 * 1024
        size = 0
        try:
            with temporary.open("xb") as output:
                while chunk := file.file.read(8 * 1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(status_code=413, detail={"code": "UPLOAD_TOO_LARGE", "message": "CT 文件超过上传大小限制"})
                    output.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail={"code": "EMPTY_FILE", "message": "上传文件为空"})
            os.replace(temporary, input_path)
        finally:
            await file.close()
            if temporary.exists():
                temporary.unlink()
        now = datetime.now(timezone.utc).isoformat()
        storage._atomic_json(case_dir / "status.json", {
            "case_id": case_id,
            "status": "uploaded",
            "message": "CT 文件上传成功",
            "progress": 0,
            "updated_at": now,
        })
        service.record_audit(context.user.id, "case.ct.upload", "success", "case", case_id, {"size_bytes": size})
        return {"case_id": case_id, "status": "uploaded", "size_bytes": size}

    @router.post("/{case_id}/ct/segment/organs", status_code=status.HTTP_202_ACCEPTED)
    async def segment_organs(case_id: str, payload: CtSegmentationRequest, request: Request) -> dict[str, Any]:
        context = authorize(request, case_id, CaseType.PPGL_CT, "edit")
        result = await ct_start_organs(
            case_id,
            BackgroundTasks(),
            payload.mode,
            payload.device,
            payload.force,
            payload.fast,
            payload.fastest,
        )
        task = coordinator.task_state(case_id, TaskType.TOTALSEGMENTATOR)
        sync_task(case_id, TaskType.TOTALSEGMENTATOR, task)
        service.record_audit(context.user.id, "inference.ct.organs.submit", "success", "case", case_id)
        return {"workflow": "ct_organs", "status": result, "task": task}

    @router.post("/{case_id}/ct/segment/ppgl", status_code=status.HTTP_202_ACCEPTED)
    async def segment_ppgl(case_id: str, payload: PpglSegmentationRequest, request: Request) -> dict[str, Any]:
        context = authorize(request, case_id, CaseType.PPGL_CT, "edit")
        result = await ct_start_ppgl(case_id, BackgroundTasks(), payload.device, payload.force)
        task = coordinator.task_state(case_id, TaskType.PPGL_V5)
        sync_task(case_id, TaskType.PPGL_V5, task)
        service.record_audit(context.user.id, "inference.ct.ppgl.submit", "success", "case", case_id)
        return {"workflow": "ct_ppgl", "status": result, "task": task}

    @router.get("/{case_id}/ct/tasks")
    async def ct_tasks(case_id: str, request: Request) -> dict[str, Any]:
        authorize(request, case_id, CaseType.PPGL_CT, "read")
        result = await ct_status(case_id)
        organ_task = coordinator.task_state(case_id, TaskType.TOTALSEGMENTATOR)
        ppgl_task = coordinator.task_state(case_id, TaskType.PPGL_V5)
        sync_task(case_id, TaskType.TOTALSEGMENTATOR, organ_task)
        sync_task(case_id, TaskType.PPGL_V5, ppgl_task)
        return {"case_id": case_id, "status": result, "organ_task": organ_task, "ppgl_task": ppgl_task}

    @router.post("/{case_id}/ct/tasks/organs/cancel")
    async def cancel_organs(case_id: str, request: Request) -> dict[str, Any]:
        context = authorize(request, case_id, CaseType.PPGL_CT, "edit")
        result = await ct_cancel_organs(case_id)
        service.record_audit(context.user.id, "inference.ct.organs.cancel", "success", "case", case_id)
        return result

    @router.post("/{case_id}/ct/tasks/ppgl/cancel")
    async def cancel_ppgl(case_id: str, request: Request) -> dict[str, Any]:
        context = authorize(request, case_id, CaseType.PPGL_CT, "edit")
        result = await ct_cancel_ppgl(case_id)
        service.record_audit(context.user.id, "inference.ct.ppgl.cancel", "success", "case", case_id)
        return result

    @router.get("/{case_id}/ct/results/{workflow}")
    async def ct_result(case_id: str, workflow: str, request: Request) -> dict[str, Any]:
        authorize(request, case_id, CaseType.PPGL_CT, "read")
        if workflow not in {"organs", "ppgl"}:
            raise HTTPException(status_code=400, detail="workflow 必须是 organs 或 ppgl")
        case_dir = storage.case_root(case_id, CaseType.PPGL_CT)
        path = case_dir / "output" / ("result.json" if workflow == "organs" else "ppgl/result.json")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="结果尚未生成")
        relative = path.relative_to(case_dir).as_posix()
        service.register_asset(case_id, f"{workflow}_result", relative)
        return json.loads(path.read_text(encoding="utf-8"))

    @router.get("/{case_id}/ct/assets/{asset_name}")
    async def ct_asset(case_id: str, asset_name: str, request: Request) -> FileResponse:
        authorize(request, case_id, CaseType.PPGL_CT, "read")
        assets = {
            "ct": ("input/ct.nii.gz", "application/gzip"),
            "organ-mask": ("output/mask.nii.gz", "application/gzip"),
            "overlay": ("output/overlay.png", "image/png"),
            "organ-mesh": ("output/meshes/scene.glb", "model/gltf-binary"),
            "ppgl-mask": ("output/ppgl/tumor_mask.nii.gz", "application/gzip"),
            "ppgl-mesh": ("output/ppgl/meshes/scene.glb", "model/gltf-binary"),
        }
        definition = assets.get(asset_name)
        if definition is None:
            raise HTTPException(status_code=404, detail="未知 CT 结果资产")
        case_dir = storage.case_root(case_id, CaseType.PPGL_CT)
        path = case_dir / definition[0]
        if not path.is_file():
            raise HTTPException(status_code=404, detail="结果资产尚未生成")
        service.register_asset(case_id, asset_name.replace("-", "_"), definition[0])
        return FileResponse(path, media_type=definition[1], filename=f"{case_id}_{path.name}")

    @router.get("/{case_id}/ct/slices")
    async def ct_slice_manifest(case_id: str, request: Request) -> dict[str, Any]:
        authorize(request, case_id, CaseType.PPGL_CT, "read")
        case_dir = storage.case_root(case_id, CaseType.PPGL_CT)
        path = case_dir / "output" / "slices" / "slice_gallery.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="二维切片尚未生成")
        service.register_asset(case_id, "slice_manifest", "output/slices/slice_gallery.json")
        return json.loads(path.read_text(encoding="utf-8"))

    @router.get("/{case_id}/ct/slices/{filename}")
    async def ct_slice(case_id: str, filename: str, request: Request) -> FileResponse:
        authorize(request, case_id, CaseType.PPGL_CT, "read")
        if Path(filename).name != filename or not filename.lower().endswith(".png"):
            raise HTTPException(status_code=400, detail="二维切片文件名无效")
        path = storage.case_root(case_id, CaseType.PPGL_CT) / "output" / "slices" / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="二维切片不存在")
        return FileResponse(path, media_type="image/png", filename=filename, content_disposition_type="inline")

    @router.get("/{case_id}/ct/meshes/{workflow}/{label_id}")
    async def ct_label_mesh(case_id: str, workflow: str, label_id: int, request: Request) -> FileResponse:
        authorize(request, case_id, CaseType.PPGL_CT, "read")
        if workflow not in {"organs", "ppgl"} or label_id < 1:
            raise HTTPException(status_code=400, detail="三维标签参数无效")
        prefix = "output/meshes/organs" if workflow == "organs" else "output/ppgl/meshes/organs"
        path = storage.case_root(case_id, CaseType.PPGL_CT) / prefix / f"{label_id}.glb"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="三维标签模型尚未生成")
        return FileResponse(path, media_type="model/gltf-binary", filename=f"{case_id}_{workflow}_{label_id}.glb")

    @router.get("/{case_id}/ct/metrics")
    async def ct_metrics(case_id: str, request: Request) -> dict[str, Any]:
        authorize(request, case_id, CaseType.PPGL_CT, "read")
        case_dir = storage.case_root(case_id, CaseType.PPGL_CT)
        candidates = (
            case_dir / "output" / "clinical_metrics.json",
            case_dir / "output" / "metrics.json",
            case_dir / "output" / "ppgl" / "metrics.json",
        )
        path = next((item for item in candidates if item.is_file()), None)
        if path is None:
            raise HTTPException(status_code=404, detail="CT 定量指标尚未生成")
        service.register_asset(case_id, "metrics", path.relative_to(case_dir).as_posix())
        return json.loads(path.read_text(encoding="utf-8"))

    @router.get("/{case_id}/ct/mesh-manifest/{workflow}")
    async def ct_mesh_manifest(case_id: str, workflow: str, request: Request) -> dict[str, Any]:
        authorize(request, case_id, CaseType.PPGL_CT, "read")
        if workflow not in {"organs", "ppgl"}:
            raise HTTPException(status_code=400, detail="三维工作流必须是 organs 或 ppgl")
        relative = "output/meshes/mesh_manifest.json" if workflow == "organs" else "output/ppgl/meshes/mesh_manifest.json"
        path = storage.case_root(case_id, CaseType.PPGL_CT) / relative
        if not path.is_file():
            raise HTTPException(status_code=404, detail="三维模型清单尚未生成")
        service.register_asset(case_id, f"{workflow}_mesh_manifest", relative)
        return json.loads(path.read_text(encoding="utf-8"))

    @router.get("/{case_id}/ct/label-map")
    async def ct_label_map(case_id: str, request: Request) -> dict[str, Any]:
        authorize(request, case_id, CaseType.PPGL_CT, "read")
        case_dir = storage.case_root(case_id, CaseType.PPGL_CT)
        candidates = (case_dir / "output" / "label_map.json", case_dir / "output" / "meshes" / "label_map.json")
        path = next((item for item in candidates if item.is_file()), None)
        if path is None:
            raise HTTPException(status_code=404, detail="器官标签表尚未生成")
        return json.loads(path.read_text(encoding="utf-8"))

    def report_models(case_type: str) -> dict[str, str]:
        if case_type == CaseType.BRAIN_MRI.value:
            return {
                "segmentation": os.environ.get("PPGL_NNUNET_CHECKPOINT", "checkpoint_best.pth"),
                "configuration": os.environ.get("PPGL_NNUNET_CONFIGURATION", "3d_fullres"),
            }
        return {
            "anatomy": "TotalSegmentator",
            "ppgl": settings.ppgl_checkpoint.name,
        }

    def case_assistant_prompt(payload: CaseAssistantRequest, case_context: dict[str, Any]) -> str:
        safe_history: list[dict[str, str]] = []
        for item in payload.history[-10:]:
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "").strip()[:2000]
            if role not in {"user", "assistant"} or not content:
                continue
            if contains_sensitive_text(content):
                raise ValidationRejected("历史对话中包含患者身份信息、病例号或本机路径")
            safe_history.append({"role": role, "content": content})
        return (
            "你是影像科病例辅助解释助手，只能使用下方已脱敏的确定性结构化结果。\n"
            "请优先说明：影像负荷、医生复核重点、建议补充的临床或影像资料、适合的就诊科室路径。\n"
            "不得重新计算数值，不得宣称良恶性、WHO分级、病理类型、分期、预后，"
            "不得给出确定治疗或用药方案。证据不足时必须明确说明。\n"
            "回答应使用中文，并明确标注‘辅助解释，不构成诊断或治疗建议’。\n\n"
            f"【已脱敏结构化病例摘要】\n{json.dumps(case_context, ensure_ascii=False)}\n\n"
            f"【本账号最近对话】\n{json.dumps(safe_history, ensure_ascii=False)}\n\n"
            f"【当前问题】\n{payload.question}"
        )

    @router.post("/{case_id}/assistant/stream")
    async def stream_case_assistant(
        case_id: str,
        payload: CaseAssistantRequest,
        request: Request,
    ) -> StreamingResponse:
        context = _auth(request)
        # Permissions are resolved on every request so revoked access takes effect immediately.
        service.authorize_case(context.user, case_id, "read")
        record = service.get_case_record(case_id)
        try:
            safe_context = await run_in_threadpool(report_service.safe_context, case_id, record.case_type)
            prompt = case_assistant_prompt(payload, safe_context)
        except (UnifiedReportError, ValidationRejected) as exc:
            raise _service_error(exc) from exc
        service.record_audit(
            context.user.id,
            "ai.case.start",
            "success",
            "case",
            case_id,
            {"case_type": record.case_type},
        )

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
                        text = generated[sent:safe_length]
                        sent = safe_length
                        yield json.dumps({"type": "delta", "text": text}, ensure_ascii=False) + "\n"
                if contains_sensitive_text(generated):
                    raise RuntimeError("模型输出触发隐私保护，已停止返回")
                if len(generated) > sent:
                    yield json.dumps({"type": "delta", "text": generated[sent:]}, ensure_ascii=False) + "\n"
                service.record_audit(context.user.id, "ai.case.complete", "success", "case", case_id)
                yield json.dumps({"type": "done", "provider": "ollama"}, ensure_ascii=False) + "\n"
            except Exception as exc:
                service.record_audit(
                    context.user.id,
                    "ai.case.complete",
                    "failure",
                    "case",
                    case_id,
                    {"error": type(exc).__name__},
                )
                yield json.dumps({"type": "error", "detail": str(exc)}, ensure_ascii=False) + "\n"

        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/{case_id}/report/generate", status_code=status.HTTP_201_CREATED)
    async def generate_case_report(case_id: str, request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.authorize_case(context.user, case_id, "edit")
        service.require_role(context.user, UserRole.ADMIN, UserRole.DOCTOR)
        record = service.get_case_record(case_id)
        try:
            payload = await run_in_threadpool(
                report_service.generate,
                case_id,
                record.case_type,
                model_versions=report_models(record.case_type),
            )
        except UnifiedReportError as exc:
            service.record_audit(context.user.id, "report.generate", "failure", "case", case_id, {"error": type(exc).__name__})
            raise _service_error(exc) from exc
        root = storage.case_root(case_id, record.case_type)
        for kind, filename in payload["files"].items():
            if filename:
                service.register_asset(case_id, f"report_{kind}", (root / "output" / filename).relative_to(root).as_posix())
        service.record_audit(context.user.id, "report.generate", "success", "case", case_id, {"case_type": record.case_type})
        return payload

    @router.get("/{case_id}/report/json")
    async def case_report_json(case_id: str, request: Request) -> dict[str, Any]:
        context = _auth(request)
        service.authorize_case(context.user, case_id, "read")
        record = service.get_case_record(case_id)
        try:
            payload = await run_in_threadpool(report_service.require, case_id, record.case_type)
        except UnifiedReportError as exc:
            raise HTTPException(status_code=404, detail={"code": "REPORT_NOT_GENERATED", "message": str(exc)}) from exc
        service.record_audit(context.user.id, "report.view", "success", "case", case_id, {"format": "json"})
        return payload

    @router.get("/{case_id}/report/{kind}")
    async def download_case_report(case_id: str, kind: str, request: Request) -> FileResponse:
        context = _auth(request)
        service.authorize_case(context.user, case_id, "read")
        record = service.get_case_record(case_id)
        try:
            path = await run_in_threadpool(report_service.file, case_id, record.case_type, kind)
        except UnifiedReportError as exc:
            service.record_audit(context.user.id, "report.download", "failure", "case", case_id, {"format": kind})
            raise HTTPException(status_code=404, detail={"code": "REPORT_FILE_NOT_GENERATED", "message": str(exc)}) from exc
        service.record_audit(context.user.id, "report.download", "success", "case", case_id, {"format": kind})
        media_types = {"markdown": "text/markdown; charset=utf-8", "json": "application/json", "pdf": "application/pdf"}
        return FileResponse(path, media_type=media_types[kind], filename=f"{case_id}_{path.name}")

    @router.get("/{case_id}/report")
    async def case_report(case_id: str, request: Request) -> FileResponse:
        context = _auth(request)
        service.authorize_case(context.user, case_id, "read")
        record = service.get_case_record(case_id)
        case_dir = storage.case_root(case_id, record.case_type)
        path = case_dir / "output" / "report.md"
        if not path.is_file():
            raise HTTPException(status_code=404, detail={"code": "REPORT_NOT_GENERATED", "message": "辅助报告尚未生成"})
        service.register_asset(case_id, "report", path.relative_to(case_dir).as_posix())
        service.record_audit(context.user.id, "report.view", "success", "case", case_id, {"format": "markdown"})
        return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=f"{case_id}.md")

    @router.get("/{case_id}/mri/uploads")
    async def mri_uploads(case_id: str, request: Request) -> dict[str, Any]:
        authorize(request, case_id, CaseType.BRAIN_MRI, "read")
        manifest = await run_in_threadpool(brain_case_service.read_upload_manifest, case_id)
        return manifest.model_dump(mode="json")

    @router.post("/{case_id}/mri/uploads/preflight")
    async def mri_preflight(case_id: str, payload: MriPreflightRequest, request: Request) -> dict[str, Any]:
        authorize(request, case_id, CaseType.BRAIN_MRI, "edit")
        try:
            return await run_in_threadpool(upload_service.preflight, case_id, payload.modalities)
        except Exception as exc:
            raise _service_error(exc) from exc

    @router.put("/{case_id}/mri/images/{modality}")
    async def upload_mri(case_id: str, modality: str, request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
        context = authorize(request, case_id, CaseType.BRAIN_MRI, "edit")
        try:
            result = await run_in_threadpool(upload_service.save_modality, case_id, modality, file.file, file.filename)
        except Exception as exc:
            raise _service_error(exc) from exc
        finally:
            await file.close()
        service.record_audit(context.user.id, "case.mri.upload", "success", "case", case_id, {"modality": modality.lower()})
        return result

    @router.post("/{case_id}/mri/uploads/complete")
    async def complete_mri(case_id: str, request: Request) -> dict[str, Any]:
        context = authorize(request, case_id, CaseType.BRAIN_MRI, "edit")
        try:
            result = await run_in_threadpool(upload_service.complete_upload, case_id)
        except Exception as exc:
            raise _service_error(exc) from exc
        service.record_audit(context.user.id, "case.mri.validate", "success", "case", case_id)
        return result

    @router.post("/{case_id}/mri/segment", status_code=status.HTTP_202_ACCEPTED)
    async def segment_mri(case_id: str, request: Request) -> dict[str, Any]:
        context = authorize(request, case_id, CaseType.BRAIN_MRI, "edit")
        try:
            result = await run_in_threadpool(inference_service.submit, case_id)
        except Exception as exc:
            raise _service_error(exc) from exc
        sync_task(case_id, TaskType.BRAIN_NNUNET, {**result, "task_id": result.get("run_id"), "task_type": TaskType.BRAIN_NNUNET.value})
        service.record_audit(context.user.id, "inference.mri.submit", "success", "case", case_id)
        return result

    @router.get("/{case_id}/mri/task")
    async def mri_task(case_id: str, request: Request) -> dict[str, Any]:
        authorize(request, case_id, CaseType.BRAIN_MRI, "read")
        try:
            result = await run_in_threadpool(inference_service.task_state, case_id)
        except Exception as exc:
            raise _service_error(exc) from exc
        sync_task(case_id, TaskType.BRAIN_NNUNET, result.get("task") or {**result, "task_id": result.get("run_id")})
        return result

    @router.post("/{case_id}/mri/task/cancel")
    async def cancel_mri(case_id: str, request: Request) -> dict[str, Any]:
        context = authorize(request, case_id, CaseType.BRAIN_MRI, "edit")
        try:
            result = await run_in_threadpool(inference_service.cancel, case_id)
        except Exception as exc:
            raise _service_error(exc) from exc
        service.record_audit(context.user.id, "inference.mri.cancel", "success", "case", case_id)
        return result

    @router.get("/{case_id}/mri/metrics")
    async def mri_metrics(case_id: str, request: Request) -> dict[str, Any]:
        authorize(request, case_id, CaseType.BRAIN_MRI, "read")
        try:
            result = await run_in_threadpool(metrics_service.require_existing, case_id)
        except Exception as exc:
            raise _service_error(exc) from exc
        service.register_asset(case_id, "metrics", "output/metrics.json")
        return result

    @router.get("/{case_id}/mri/segmentation")
    async def mri_segmentation(case_id: str, request: Request) -> FileResponse:
        authorize(request, case_id, CaseType.BRAIN_MRI, "read")
        path = brain_case_service.paths_for(case_id, require_exists=True).output / "segmentation.nii.gz"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="MRI 分割结果尚未生成")
        service.register_asset(case_id, "segmentation", "output/segmentation.nii.gz")
        return FileResponse(path, media_type="application/gzip", filename=f"{case_id}_segmentation.nii.gz")

    @router.get("/{case_id}/mri/visualizations")
    async def mri_visualizations(case_id: str, request: Request) -> dict[str, Any]:
        authorize(request, case_id, CaseType.BRAIN_MRI, "read")
        try:
            result = await run_in_threadpool(visualization_service.require_existing, case_id)
        except Exception as exc:
            raise _service_error(exc) from exc
        service.register_asset(case_id, "visualization_manifest", "output/visualizations.json")
        return result

    @router.post("/{case_id}/mri/visualizations", status_code=status.HTTP_201_CREATED)
    async def generate_mri_visualizations(case_id: str, request: Request) -> dict[str, Any]:
        context = authorize(request, case_id, CaseType.BRAIN_MRI, "edit")
        try:
            result = await run_in_threadpool(visualization_service.generate, case_id)
        except Exception as exc:
            raise _service_error(exc) from exc
        service.register_asset(case_id, "visualization_manifest", "output/visualizations.json")
        service.record_audit(context.user.id, "visualization.mri.generate", "success", "case", case_id)
        return result

    @router.get("/{case_id}/mri/visualizations/slices/{plane}/{index}/{layer}.png")
    async def mri_slice(case_id: str, plane: str, index: int, layer: str, request: Request) -> FileResponse:
        authorize(request, case_id, CaseType.BRAIN_MRI, "read")
        try:
            path = await run_in_threadpool(visualization_service.slice_asset_path, case_id, plane, index, layer)
        except Exception as exc:
            raise _service_error(exc) from exc
        return FileResponse(path, media_type="image/png", filename=path.name, content_disposition_type="inline")

    @router.get("/{case_id}/mri/visualizations/meshes/{filename}")
    async def mri_mesh(case_id: str, filename: str, request: Request) -> FileResponse:
        authorize(request, case_id, CaseType.BRAIN_MRI, "read")
        if Path(filename).name != filename or not filename.lower().endswith((".glb", ".json")):
            raise HTTPException(status_code=400, detail="三维文件名无效")
        path = brain_case_service.paths_for(case_id, require_exists=True).meshes / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="三维模型尚未生成")
        service.register_asset(case_id, "mesh", path.relative_to(path.parents[2]).as_posix())
        return FileResponse(path, media_type="model/gltf-binary" if path.suffix == ".glb" else "application/json", filename=path.name)

    @router.put("/{case_id}/review")
    async def save_review(case_id: str, payload: DoctorReviewRequest, request: Request) -> dict[str, Any]:
        context = authorize(request, case_id, CaseType(service.get_case_record(case_id).case_type), "edit")
        service.require_role(context.user, UserRole.ADMIN, UserRole.DOCTOR)
        record = service.get_case_record(case_id)
        case_root = storage.case_root(case_id, record.case_type)
        review = {
            "case_id": case_id,
            "opinion": payload.opinion.strip(),
            "doctor_name": payload.doctor_name.strip(),
            "review_date": payload.review_date,
            "reviewer_user_id": context.user.id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        output_dir = case_root / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            await run_in_threadpool(report_service.apply_review, case_id, record.case_type, review)
        except UnifiedReportError as exc:
            raise HTTPException(status_code=409, detail={"code": "REPORT_NOT_GENERATED", "message": str(exc)}) from exc
        storage._atomic_json(output_dir / "doctor_review.json", review)
        service.register_asset(case_id, "doctor_review", "output/doctor_review.json")
        service.record_audit(context.user.id, "report.doctor_review", "success", "case", case_id)
        return review

    return router
