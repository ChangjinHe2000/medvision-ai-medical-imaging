from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from backend.workers.process_runner import ProcessRunner
from backend.workers.task_queue import UnifiedGpuTaskQueue

from .case_service import (
    CaseDeletionConflictError,
    CaseNotFoundError,
    CaseService,
    CaseServiceError,
    InvalidCaseDisplayNameError,
    InvalidCaseIdError,
    InvalidStatusTransitionError,
)
from .metrics_service import (
    MetricsNotGeneratedError,
    MetricsService,
    MetricsServiceError,
    MetricsValidationError,
    SegmentationNotFoundError,
)
from .models import CaseRenameRequest, MRI_MODALITIES, UploadPreflightRequest
from .nifti_validator import NiftiValidationError
from .nnunet_service import (
    InferenceBusyError,
    InferenceDisabledError,
    InferenceTaskNotFoundError,
    ModelPackageError,
    NnunetInferenceConfig,
    NnunetInferenceService,
    NnunetServiceError,
)
from .upload_service import (
    BratsUploadService,
    InsufficientStorageError,
    InvalidModalityError,
    MissingModalitiesError,
    UploadServiceError,
    UploadStateError,
    UploadTooLargeError,
)
from .visualization_service import (
    VisualizationNotGeneratedError,
    VisualizationService,
    VisualizationServiceError,
    VisualizationValidationError,
)


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数") from exc
    if value < minimum:
        raise RuntimeError(f"{name} 不能小于 {minimum}")
    return value


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是数字") from exc


def _path_env(name: str, default: Path, base: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    path = Path(raw).expanduser() if raw else default
    return (path if path.is_absolute() else base / path).resolve()


def _detail(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "details": details or {}}


def _case_error(exc: Exception) -> HTTPException:
    status_code = 500
    code = "BRAIN_CASE_ERROR"
    message = "脑胶质瘤病例处理失败"
    details: dict[str, Any] = {}
    if isinstance(exc, CaseNotFoundError):
        status_code, code, message = 404, "CASE_NOT_FOUND", "脑胶质瘤病例不存在"
    elif isinstance(exc, InvalidCaseIdError):
        status_code, code, message = 400, "INVALID_CASE_ID", "病例编号无效"
    elif isinstance(exc, InvalidCaseDisplayNameError):
        status_code, code, message = 422, "INVALID_CASE_DISPLAY_NAME", str(exc)
    elif isinstance(exc, CaseDeletionConflictError):
        status_code, code, message = 409, "CASE_DELETE_CONFLICT", str(exc)
    elif isinstance(exc, InvalidModalityError):
        status_code, code, message, details = 400, exc.code, exc.message, exc.details
    elif isinstance(exc, UploadTooLargeError):
        status_code, code, message, details = 413, exc.code, exc.message, exc.details
    elif isinstance(exc, InsufficientStorageError):
        status_code, code, message, details = 507, exc.code, exc.message, exc.details
    elif isinstance(exc, (MissingModalitiesError, NiftiValidationError)):
        status_code = 422
        code = getattr(exc, "code", "INVALID_NIFTI")
        message = getattr(exc, "message", str(exc))
        details = getattr(exc, "details", {})
    elif isinstance(exc, UploadStateError):
        status_code, code, message, details = 409, exc.code, exc.message, exc.details
    elif isinstance(exc, UploadServiceError):
        status_code, code, message, details = 422, exc.code, exc.message, exc.details
    elif isinstance(exc, InvalidStatusTransitionError):
        status_code, code, message = 409, "INVALID_CASE_STATUS", str(exc)
    elif isinstance(exc, CaseServiceError):
        status_code, code, message = 400, "CASE_STORAGE_ERROR", str(exc)
    return HTTPException(status_code=status_code, detail=_detail(code, message, details))


def _nnunet_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (InferenceDisabledError, ModelPackageError)):
        status_code = 503
    elif isinstance(exc, (InferenceBusyError, InferenceTaskNotFoundError)):
        status_code = 409
    elif isinstance(exc, NnunetServiceError):
        status_code = 422
    else:
        return _case_error(exc)
    return HTTPException(
        status_code=status_code,
        detail=_detail(exc.code, exc.message, exc.details),
    )


def _metrics_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MetricsNotGeneratedError):
        status_code = 404
    elif isinstance(exc, SegmentationNotFoundError):
        status_code = 409
    elif isinstance(exc, MetricsValidationError):
        status_code = 422
    elif isinstance(exc, MetricsServiceError):
        status_code = 500
    else:
        return _case_error(exc)
    return HTTPException(status_code=status_code, detail=_detail(exc.code, exc.message, exc.details))


def _visualization_error(exc: Exception) -> HTTPException:
    if isinstance(exc, VisualizationNotGeneratedError):
        status_code = 404
    elif isinstance(exc, VisualizationValidationError):
        status_code = 422
    elif isinstance(exc, VisualizationServiceError):
        status_code = 500
    else:
        return _case_error(exc)
    return HTTPException(status_code=status_code, detail=_detail(exc.code, exc.message, exc.details))


def build_glioma_router(
    data_root: Path,
    execution_lock: Lock,
    before_inference: Any | None = None,
    after_inference: Any | None = None,
    unified_queue: UnifiedGpuTaskQueue | None = None,
    process_runner: ProcessRunner | None = None,
) -> APIRouter:
    cases_dir = _path_env("PPGL_GLIOMA_CASES_DIR", data_root / "brain-cases", data_root)
    trash_dir = _path_env("PPGL_GLIOMA_TRASH_DIR", data_root / "brain-trash", data_root)
    model_dir = _path_env(
        "PPGL_GLIOMA_MODEL_DIR",
        data_root
        / "models"
        / "brain-glioma"
        / "nnUNetTrainer__nnUNetPlans__3d_fullres",
        data_root,
    )
    cases_dir.mkdir(parents=True, exist_ok=True)
    trash_dir.mkdir(parents=True, exist_ok=True)

    case_service = CaseService(cases_dir, trash_dir)
    upload_service = BratsUploadService(
        case_service,
        _int_env("PPGL_GLIOMA_MAX_UPLOAD_MB", 2048) * 1024 * 1024,
    )
    metrics_service = MetricsService(case_service)
    visualization_service = VisualizationService(case_service)
    inference_service = NnunetInferenceService(
        case_service,
        NnunetInferenceConfig(
            model_dir=model_dir,
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
            timeout_seconds=_int_env("PPGL_GLIOMA_TIMEOUT_SECONDS", 7200),
            enabled=_bool_env("PPGL_GLIOMA_ENABLED", False),
            gpu_lock_file=data_root / "locks" / "glioma-nnunet.lock",
        ),
        metrics_service=metrics_service,
        visualization_service=visualization_service,
        execution_lock=execution_lock,
        before_inference=before_inference,
        after_inference=after_inference,
        unified_queue=unified_queue,
        process_runner=process_runner,
        data_root=data_root,
    )

    router = APIRouter(prefix="/api/brain", tags=["brain-glioma"])

    def auth(request: Request) -> dict[str, Any]:
        return getattr(request.state, "auth", {}) or {}

    def assert_access(case_id: str, request: Request) -> None:
        context = auth(request)
        if context.get("kind") == "internal" or context.get("role") == "ADMIN":
            return
        info = case_service.read_case_info(case_id)
        if not context.get("uid") or info.owner_user_id != int(context["uid"]):
            raise HTTPException(status_code=403, detail="无权访问该脑胶质瘤病例")

    def public_summary(item: dict[str, Any]) -> dict[str, Any]:
        payload = dict(item)
        payload.pop("owner_user_id", None)
        return payload

    def upload_state(case_id: str) -> dict[str, Any]:
        manifest = case_service.read_upload_manifest(case_id)
        status = case_service.read_status(case_id)
        modalities = {
            key: {
                "uploaded": item.uploaded,
                "size_bytes": item.size_bytes,
                "uploaded_at": item.uploaded_at,
                "nifti": item.nifti.model_dump(mode="json") if item.nifti else None,
            }
            for key, item in manifest.modalities.items()
        }
        return {
            "case_id": case_id,
            "status": status.status.value,
            "message": status.message,
            "progress": status.progress,
            "complete": manifest.complete,
            "ready_for_segmentation": manifest.complete and status.status.value == "uploaded",
            "modalities": modalities,
        }

    @router.get("/model")
    async def model_status() -> dict[str, Any]:
        return await run_in_threadpool(inference_service.model_status, False)

    @router.post("/cases", status_code=201)
    async def create_case(request: Request) -> dict[str, Any]:
        context = auth(request)
        owner_id = int(context["uid"]) if context.get("uid") else None
        try:
            created = await run_in_threadpool(case_service.create_case, owner_id)
        except Exception as exc:
            raise _case_error(exc) from exc
        info = created["case_info"]
        status = created["status"]
        return {
            "case_id": info["case_id"],
            "workflow": info["workflow"],
            "status": status["status"],
            "message": status["message"],
            "required_modalities": info["required_modalities"],
        }

    @router.get("/cases")
    async def list_cases(request: Request) -> dict[str, Any]:
        context = auth(request)
        try:
            items = await run_in_threadpool(case_service.list_cases)
        except Exception as exc:
            raise _case_error(exc) from exc
        if context.get("kind") != "internal" and context.get("role") != "ADMIN":
            items = [item for item in items if item.get("owner_user_id") == context.get("uid")]
        cases = [public_summary(item) for item in items]
        return {"cases": cases, "total": len(cases)}

    @router.get("/cases/{case_id}")
    async def get_case(case_id: str, request: Request) -> dict[str, Any]:
        try:
            assert_access(case_id, request)
            summary = await run_in_threadpool(case_service.case_summary, case_id, True)
            return public_summary(summary)
        except HTTPException:
            raise
        except Exception as exc:
            raise _case_error(exc) from exc

    @router.patch("/cases/{case_id}")
    async def rename_case(case_id: str, payload: CaseRenameRequest, request: Request) -> dict[str, Any]:
        try:
            assert_access(case_id, request)
            result = await run_in_threadpool(case_service.rename_case, case_id, payload.display_name)
            return public_summary(result)
        except HTTPException:
            raise
        except Exception as exc:
            raise _case_error(exc) from exc

    @router.delete("/cases/{case_id}")
    async def delete_case(case_id: str, request: Request) -> dict[str, Any]:
        try:
            assert_access(case_id, request)
            state = await run_in_threadpool(inference_service.task_state, case_id)
            if state.get("active"):
                raise CaseDeletionConflictError("脑胶质瘤分割仍在运行，不能删除")
            return await run_in_threadpool(case_service.purge_case, case_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise _case_error(exc) from exc

    @router.get("/cases/{case_id}/uploads")
    async def get_uploads(case_id: str, request: Request) -> dict[str, Any]:
        try:
            assert_access(case_id, request)
            return await run_in_threadpool(upload_state, case_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise _case_error(exc) from exc

    @router.post("/cases/{case_id}/uploads/preflight")
    async def preflight(case_id: str, payload: UploadPreflightRequest, request: Request) -> dict[str, Any]:
        try:
            assert_access(case_id, request)
            return await run_in_threadpool(upload_service.preflight, case_id, payload.modalities)
        except HTTPException:
            raise
        except Exception as exc:
            raise _case_error(exc) from exc

    @router.put("/cases/{case_id}/images/{modality}")
    async def upload_image(
        case_id: str,
        modality: str,
        request: Request,
        file: UploadFile = File(...),
    ) -> dict[str, Any]:
        try:
            assert_access(case_id, request)
            return await run_in_threadpool(
                upload_service.save_modality,
                case_id,
                modality,
                file.file,
                file.filename,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise _case_error(exc) from exc
        finally:
            await file.close()

    @router.get("/cases/{case_id}/images/{modality}")
    async def download_image(case_id: str, modality: str, request: Request) -> FileResponse:
        assert_access(case_id, request)
        key = modality.strip().lower()
        if key not in MRI_MODALITIES:
            raise HTTPException(status_code=400, detail=_detail("INVALID_MODALITY", "MRI 序列名称无效"))
        paths = case_service.paths_for(case_id, require_exists=True)
        path = paths.original_input / f"{key}.nii.gz"
        if not path.is_file():
            raise HTTPException(status_code=404, detail=_detail("MRI_FILE_NOT_FOUND", "MRI 序列不存在"))
        return FileResponse(path, filename=f"{case_id}_{key}.nii.gz", media_type="application/gzip")

    @router.post("/cases/{case_id}/uploads/complete")
    async def complete_uploads(case_id: str, request: Request) -> dict[str, Any]:
        try:
            assert_access(case_id, request)
            return await run_in_threadpool(upload_service.complete_upload, case_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise _case_error(exc) from exc

    @router.post("/cases/{case_id}/segment", status_code=202)
    async def start_segmentation(case_id: str, request: Request) -> dict[str, Any]:
        try:
            assert_access(case_id, request)
            return await run_in_threadpool(inference_service.submit, case_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise _nnunet_error(exc) from exc

    @router.get("/cases/{case_id}/segmentation")
    async def segmentation_status(case_id: str, request: Request) -> dict[str, Any]:
        try:
            assert_access(case_id, request)
            return await run_in_threadpool(inference_service.task_state, case_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise _nnunet_error(exc) from exc

    @router.post("/cases/{case_id}/segmentation/cancel")
    async def cancel_segmentation(case_id: str, request: Request) -> dict[str, Any]:
        try:
            assert_access(case_id, request)
            return await run_in_threadpool(inference_service.cancel, case_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise _nnunet_error(exc) from exc

    @router.get("/cases/{case_id}/segmentation/file")
    async def segmentation_file(case_id: str, request: Request) -> FileResponse:
        assert_access(case_id, request)
        path = case_service.paths_for(case_id, require_exists=True).output / "segmentation.nii.gz"
        if not path.is_file():
            raise HTTPException(status_code=404, detail=_detail("SEGMENTATION_NOT_FOUND", "分割结果尚未生成"))
        return FileResponse(path, filename=f"{case_id}_segmentation.nii.gz", media_type="application/gzip")

    @router.get("/cases/{case_id}/metrics")
    async def get_metrics(case_id: str, request: Request) -> dict[str, Any]:
        try:
            assert_access(case_id, request)
            return await run_in_threadpool(metrics_service.require_existing, case_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise _metrics_error(exc) from exc

    @router.get("/cases/{case_id}/visualizations")
    async def get_visualizations(case_id: str, request: Request) -> dict[str, Any]:
        try:
            assert_access(case_id, request)
            return await run_in_threadpool(visualization_service.require_existing, case_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise _visualization_error(exc) from exc

    @router.post("/cases/{case_id}/visualizations", status_code=201)
    async def generate_visualizations(case_id: str, request: Request) -> dict[str, Any]:
        try:
            assert_access(case_id, request)
            return await run_in_threadpool(visualization_service.generate, case_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise _visualization_error(exc) from exc

    @router.get("/cases/{case_id}/visualizations/slices/{plane}/{index}/{layer}.png")
    async def visualization_slice(
        case_id: str,
        plane: str,
        index: int,
        layer: str,
        request: Request,
    ) -> FileResponse:
        try:
            assert_access(case_id, request)
            path = await run_in_threadpool(
                visualization_service.slice_asset_path,
                case_id,
                plane,
                index,
                layer,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise _visualization_error(exc) from exc
        return FileResponse(path, filename=path.name, media_type="image/png", content_disposition_type="inline")

    @router.get("/cases/{case_id}/visualizations/meshes")
    async def mesh_manifest(case_id: str, request: Request) -> dict[str, Any]:
        try:
            assert_access(case_id, request)
            manifest = await run_in_threadpool(visualization_service.require_existing, case_id)
            return {
                "case_id": case_id,
                "generated_at": manifest.get("generated_at"),
                "mesh": manifest.get("mesh"),
                "limitations": manifest.get("limitations", []),
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise _visualization_error(exc) from exc

    @router.get("/cases/{case_id}/visualizations/scene.glb")
    async def mesh_file(case_id: str, request: Request) -> FileResponse:
        try:
            assert_access(case_id, request)
            path = await run_in_threadpool(visualization_service.mesh_path, case_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise _visualization_error(exc) from exc
        return FileResponse(path, filename=f"{case_id}_brain_tumour.glb", media_type="model/gltf-binary")

    return router
