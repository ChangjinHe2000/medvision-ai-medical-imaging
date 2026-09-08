from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from threading import Event, Lock, RLock, Thread
import time
from typing import Any, Dict, Optional, Sequence
import uuid

from .case_service import CaseService, atomic_write_json, utc_now
from .gpu_guard import GpuFileLock
from .metrics_service import MetricsService
from .visualization_service import VisualizationService
from .nifti_validator import inspect_nifti
from .models import MRI_MODALITIES, CaseStatus
from backend.workers.process_runner import (
    ProcessRunner,
    TaskExecutionContext,
    WorkerExecutionError,
    redact_text as redact_runtime_text,
    terminate_process_tree,
)
from backend.domain.task_contract import TaskErrorCode, TaskStatus, TaskType
from backend.workers.brain_nnunet.worker import BrainNnunetJob, BrainNnunetWorker
from backend.workers.task_queue import UnifiedGpuTaskQueue


def redact_text(value: Any) -> str:
    roots = [Path(__file__).resolve().parents[2]]
    configured_data_root = os.environ.get("PPGL_DATA_ROOT", "").strip()
    if configured_data_root:
        roots.append(Path(configured_data_root).expanduser())
    return redact_runtime_text(value, roots=roots)


MODEL_MANIFEST_FILENAME = "model_manifest.json"


class NnunetServiceError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class InferenceDisabledError(NnunetServiceError):
    pass


class ModelPackageError(NnunetServiceError):
    pass


class InferenceBusyError(NnunetServiceError):
    pass


class InferenceTaskNotFoundError(NnunetServiceError):
    pass


@dataclass(frozen=True)
class NnunetInferenceConfig:
    model_dir: Path
    dataset: str
    configuration: str
    fold: int
    checkpoint: str
    trainer: str
    plans: str
    device: str
    step_size: float
    disable_tta: bool
    preprocess_processes: int
    export_processes: int
    timeout_seconds: int
    enabled: bool
    gpu_lock_file: Path | None = None

    @classmethod
    def from_settings(cls, settings) -> "NnunetInferenceConfig":
        return cls(
            model_dir=settings.nnunet_model_dir,
            dataset=settings.nnunet_dataset,
            configuration=settings.nnunet_configuration,
            fold=settings.nnunet_fold,
            checkpoint=settings.nnunet_checkpoint,
            trainer=settings.nnunet_trainer,
            plans=settings.nnunet_plans,
            device=settings.nnunet_device,
            step_size=settings.nnunet_step_size,
            disable_tta=settings.nnunet_disable_tta,
            preprocess_processes=settings.nnunet_preprocess_processes,
            export_processes=settings.nnunet_export_processes,
            timeout_seconds=settings.task_timeout_seconds,
            enabled=settings.nnunet_inference_enabled,
            gpu_lock_file=settings.gpu_lock_file,
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelPackageError(
            "INVALID_MODEL_METADATA",
            f"模型元数据文件无效：{path.name}",
        ) from exc
    if not isinstance(payload, dict):
        raise ModelPackageError(
            "INVALID_MODEL_METADATA",
            f"模型元数据文件必须包含 JSON 对象：{path.name}",
        )
    return payload


def inspect_model_package(
    config: NnunetInferenceConfig,
    *,
    verify_hashes: bool = False,
) -> Dict[str, Any]:
    model_dir = config.model_dir.expanduser().resolve()
    if any(part.casefold() == "nnunet_results" for part in model_dir.parts):
        raise ModelPackageError(
            "LIVE_TRAINING_MODEL_FORBIDDEN",
            "正式推理禁止直接读取 nnUNet_results 实时训练目录",
        )

    required = {
        "dataset.json": model_dir / "dataset.json",
        "plans.json": model_dir / "plans.json",
        f"fold_{config.fold}/{config.checkpoint}": model_dir
        / f"fold_{config.fold}"
        / config.checkpoint,
        MODEL_MANIFEST_FILENAME: model_dir / MODEL_MANIFEST_FILENAME,
    }
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        raise ModelPackageError(
            "MODEL_PACKAGE_INCOMPLETE",
            "稳定 nnU-Net 模型快照不完整",
            {"missing_files": missing},
        )

    dataset_json = _read_json(required["dataset.json"])
    plans_json = _read_json(required["plans.json"])
    manifest = _read_json(required[MODEL_MANIFEST_FILENAME])

    if config.configuration not in plans_json.get("configurations", {}):
        raise ModelPackageError(
            "MODEL_CONFIGURATION_MISSING",
            "plans.json 中不存在配置的 nnU-Net configuration",
        )
    expected_manifest = {
        "snapshot_complete": True,
        "dataset": config.dataset,
        "configuration": config.configuration,
        "fold": config.fold,
        "checkpoint": config.checkpoint,
        "trainer": config.trainer,
        "plans": config.plans,
    }
    mismatches = {
        key: {"expected": expected, "actual": manifest.get(key)}
        for key, expected in expected_manifest.items()
        if manifest.get(key) != expected
    }
    if mismatches:
        raise ModelPackageError(
            "MODEL_MANIFEST_MISMATCH",
            "模型快照清单与当前推理配置不一致",
            {"mismatches": mismatches},
        )

    if verify_hashes:
        manifest_files = manifest.get("files", {})
        for relative_name in ("dataset.json", "plans.json", f"fold_{config.fold}/{config.checkpoint}"):
            expected_hash = (manifest_files.get(relative_name) or {}).get("sha256")
            path = model_dir / Path(relative_name)
            if not expected_hash or sha256_file(path) != expected_hash:
                raise ModelPackageError(
                    "MODEL_HASH_MISMATCH",
                    "模型快照文件完整性检查失败",
                    {"file": relative_name},
                )

    return {
        "status": "ready",
        "model_version": str(manifest.get("model_version", "")),
        "dataset": config.dataset,
        "configuration": config.configuration,
        "fold": config.fold,
        "checkpoint": config.checkpoint,
        "trainer": config.trainer,
        "plans": config.plans,
        "nnunet_version": manifest.get("nnunet_version"),
        "snapshot_created_at": manifest.get("created_at"),
        "channel_names": dataset_json.get("channel_names", {}),
        "labels": dataset_json.get("labels", {}),
        "hashes_verified": verify_hashes,
    }


def resolve_predict_command() -> list[str]:
    command_name = "nnUNetv2_predict_from_modelfolder"
    found = shutil.which(command_name)
    if found:
        return [found]

    scripts_dir = Path(sys.executable).resolve().parent / ("Scripts" if os.name == "nt" else "bin")
    candidates = [scripts_dir / command_name]
    if os.name == "nt":
        candidates.insert(0, scripts_dir / f"{command_name}.exe")
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]
    raise NnunetServiceError(
        "NNUNET_COMMAND_NOT_FOUND",
        "当前 Python 环境中没有找到 nnUNetv2_predict_from_modelfolder",
    )


def _write_redacted_process_stream(stream: Any, destination: Path) -> None:
    """Persist subprocess output without local paths or patient identifiers."""

    with destination.open("w", encoding="utf-8", newline="\n") as output:
        for line in iter(stream.readline, ""):
            output.write(redact_text(line.rstrip("\r\n")))
            output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    stream.close()


@dataclass
class InferenceTaskHandle:
    case_id: str
    run_id: str
    cancel_event: Event = field(default_factory=Event)
    thread: Optional[Thread] = None
    process: Optional[subprocess.Popen] = None


class NnunetInferenceService:
    def __init__(
        self,
        case_service: CaseService,
        config: NnunetInferenceConfig,
        predict_command: Sequence[str] | None = None,
        metrics_service: MetricsService | None = None,
        visualization_service: VisualizationService | None = None,
        execution_lock: Lock | None = None,
        before_inference: Any | None = None,
        after_inference: Any | None = None,
        unified_queue: UnifiedGpuTaskQueue | None = None,
        process_runner: ProcessRunner | None = None,
        data_root: Path | None = None,
    ):
        self.case_service = case_service
        self.config = config
        self.predict_command = list(predict_command) if predict_command else None
        self.metrics_service = metrics_service
        self.visualization_service = visualization_service
        self.before_inference = before_inference
        self.after_inference = after_inference
        self._tasks: Dict[str, InferenceTaskHandle] = {}
        self._tasks_lock = RLock()
        self._execution_lock = execution_lock or Lock()
        self._unified_queue = unified_queue
        self._process_runner = process_runner
        self._data_root = data_root.expanduser().resolve() if data_root else case_service.cases_root.parent
        self._queue_task_ids: Dict[str, str] = {}

    def model_status(self, verify_hashes: bool = False) -> Dict[str, Any]:
        try:
            status = inspect_model_package(self.config, verify_hashes=verify_hashes)
        except ModelPackageError as exc:
            return {
                "status": "not_ready",
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
                "inference_enabled": self.config.enabled,
            }
        status["inference_enabled"] = self.config.enabled
        return status

    def _require_ready(self) -> Dict[str, Any]:
        if not self.config.enabled:
            raise InferenceDisabledError(
                "NNUNET_INFERENCE_DISABLED",
                "nnU-Net 推理当前被配置锁定；训练完成并制作稳定快照后才能启用",
            )
        return inspect_model_package(self.config, verify_hashes=True)

    def _validate_case_input(self, case_id: str) -> None:
        status = self.case_service.read_status(case_id)
        manifest = self.case_service.read_upload_manifest(case_id)
        if status.status not in {CaseStatus.UPLOADED, CaseStatus.FAILED} or not manifest.complete:
            raise NnunetServiceError(
                "CASE_NOT_READY_FOR_INFERENCE",
                "病例尚未完成四序列上传和空间一致性校验",
                {"status": status.status.value},
            )
        paths = self.case_service.paths_for(case_id, require_exists=True)
        missing = [
            f"{case_id}_{definition.nnunet_suffix}.nii.gz"
            for definition in MRI_MODALITIES.values()
            if not (paths.nnunet_input / f"{case_id}_{definition.nnunet_suffix}.nii.gz").is_file()
        ]
        if missing:
            raise NnunetServiceError(
                "NNUNET_INPUT_INCOMPLETE",
                "病例 nnU-Net 输入文件不完整",
                {"missing_files": missing},
            )

    def submit(self, case_id: str) -> Dict[str, Any]:
        model = self._require_ready()
        self._validate_case_input(case_id)
        if self._unified_queue is not None and self._process_runner is not None:
            return self._submit_unified(case_id, model)
        with self._tasks_lock:
            existing = self._tasks.get(case_id)
            if existing and existing.thread and existing.thread.is_alive():
                raise InferenceBusyError(
                    "CASE_INFERENCE_ALREADY_ACTIVE",
                    "该病例已经存在正在运行或排队的推理任务",
                )

            run_id = f"run_{utc_now().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
            handle = InferenceTaskHandle(case_id=case_id, run_id=run_id)
            self.case_service.transition_status(
                case_id,
                CaseStatus.QUEUED,
                "脑胶质瘤分割任务已进入队列",
                0,
            )
            thread = Thread(
                target=self._run_task,
                args=(handle, model),
                name=f"nnunet-{run_id}",
                daemon=True,
            )
            handle.thread = thread
            self._tasks[case_id] = handle
            thread.start()
        return {"case_id": case_id, "run_id": run_id, "status": "queued"}

    def cancel(self, case_id: str) -> Dict[str, Any]:
        if self._unified_queue is not None:
            task_id = self._queue_task_ids.get(case_id)
            if not task_id:
                latest = self._unified_queue.latest_for_case(case_id)
                if latest and latest.get("task_type") == TaskType.BRAIN_NNUNET.value:
                    task_id = str(latest["task_id"])
            if not task_id:
                raise InferenceTaskNotFoundError(
                    "ACTIVE_INFERENCE_NOT_FOUND",
                    "该病例没有可以取消的活动推理任务",
                )
            try:
                record = self._unified_queue.cancel(task_id)
            except KeyError as exc:
                raise InferenceTaskNotFoundError(
                    "ACTIVE_INFERENCE_NOT_FOUND",
                    "该病例没有可以取消的活动推理任务",
                ) from exc
            self._observe_unified(case_id, record)
            return {"case_id": case_id, "run_id": task_id, "status": record["status"]}
        with self._tasks_lock:
            handle = self._tasks.get(case_id)
            if handle is None or handle.thread is None or not handle.thread.is_alive():
                raise InferenceTaskNotFoundError(
                    "ACTIVE_INFERENCE_NOT_FOUND",
                    "该病例没有可以取消的活动推理任务",
                )
            handle.cancel_event.set()
            process = handle.process

        if process is not None and process.poll() is None:
            self._terminate_process_tree(process)
        else:
            current = self.case_service.read_status(case_id)
            if current.status == CaseStatus.QUEUED:
                self.case_service.transition_status(
                    case_id,
                    CaseStatus.CANCELLED,
                    "脑胶质瘤分割任务已取消",
                    0,
                )
        return {"case_id": case_id, "run_id": handle.run_id, "status": "cancelled"}

    def task_state(self, case_id: str) -> Dict[str, Any]:
        if self._unified_queue is not None:
            task_id = self._queue_task_ids.get(case_id)
            task = None
            if task_id:
                try:
                    task = self._unified_queue.get(task_id)
                except KeyError:
                    task = None
            if task is None:
                latest = self._unified_queue.latest_for_case(case_id)
                if latest and latest.get("task_type") == TaskType.BRAIN_NNUNET.value:
                    task = latest
            status = self.case_service.read_status(case_id)
            paths = self.case_service.paths_for(case_id, require_exists=True)
            record_path = paths.output / "inference.json"
            record = None
            if record_path.is_file():
                try:
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    record = {"status": "unreadable"}
            return {
                "case_id": case_id,
                "status": status.status.value,
                "message": status.message,
                "progress": status.progress,
                "active": bool(task and task.get("status") in {"queued", "running"}),
                "run_id": (task or {}).get("task_id") or (record or {}).get("run_id"),
                "task": task,
                "inference": record,
            }
        status = self.case_service.read_status(case_id)
        with self._tasks_lock:
            handle = self._tasks.get(case_id)
            active = bool(handle and handle.thread and handle.thread.is_alive())
            run_id = handle.run_id if handle else None
        paths = self.case_service.paths_for(case_id, require_exists=True)
        record_path = paths.output / "inference.json"
        record = None
        if record_path.is_file():
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                record = {"status": "unreadable"}
        return {
            "case_id": case_id,
            "status": status.status.value,
            "message": status.message,
            "progress": status.progress,
            "active": active,
            "run_id": run_id or (record or {}).get("run_id"),
            "inference": record,
        }

    def _submit_unified(self, case_id: str, model: Dict[str, Any]) -> Dict[str, Any]:
        assert self._unified_queue is not None
        with self._tasks_lock:
            existing_id = self._queue_task_ids.get(case_id)
            if existing_id:
                try:
                    existing = self._unified_queue.get(existing_id)
                except KeyError:
                    existing = None
                if existing and existing.get("status") in {"queued", "running"}:
                    raise InferenceBusyError(
                        "CASE_INFERENCE_ALREADY_ACTIVE",
                        "该病例已经存在正在运行或排队的推理任务",
                    )

            try:
                record = self._unified_queue.submit(
                    task_type=TaskType.BRAIN_NNUNET,
                    case_id=case_id,
                    handler=lambda context: self._run_unified_worker(case_id, model, context),
                    observer=lambda task: self._observe_unified(case_id, task),
                )
            except WorkerExecutionError as exc:
                raise InferenceBusyError(exc.code, exc.message, exc.details) from exc
            task_id = str(record["task_id"])
            self._queue_task_ids[case_id] = task_id
        return {"case_id": case_id, "run_id": task_id, "status": record["status"]}

    def _observe_unified(self, case_id: str, task: Dict[str, Any]) -> None:
        mapping = {
            TaskStatus.QUEUED: (CaseStatus.QUEUED, "脑胶质瘤分割任务已进入队列", 0),
            TaskStatus.RUNNING: (CaseStatus.RUNNING, "正在进行脑胶质瘤分割", 10),
            TaskStatus.COMPLETED: (CaseStatus.COMPLETED, "脑胶质瘤分割完成", 100),
            TaskStatus.FAILED: (CaseStatus.FAILED, str(task.get("message") or "脑胶质瘤分割失败"), 100),
            TaskStatus.CANCELLED: (CaseStatus.CANCELLED, "脑胶质瘤分割任务已取消", 0),
            TaskStatus.TIMED_OUT: (CaseStatus.TIMED_OUT, "脑胶质瘤分割任务超时", 100),
        }
        try:
            task_status = TaskStatus(str(task["status"]))
            target, message, progress = mapping[task_status]
            self.case_service.transition_status(case_id, target, message, progress)
            paths = self.case_service.paths_for(case_id, require_exists=True)
            inference_path = paths.output / "inference.json"
            if inference_path.is_file():
                try:
                    inference = json.loads(inference_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    inference = {}
            else:
                inference = {}
            inference.update(
                {
                    "schema_version": inference.get("schema_version", "2.0"),
                    "case_id": case_id,
                    "run_id": task.get("task_id"),
                    "status": task_status.value,
                    "message": message,
                    "error_code": task.get("error_code"),
                    "retryable": bool(task.get("retryable", False)),
                    "updated_at": utc_now().isoformat(),
                }
            )
            atomic_write_json(inference_path, inference)
        except Exception:
            return

    def _run_unified_worker(
        self,
        case_id: str,
        model: Dict[str, Any],
        context: TaskExecutionContext,
    ) -> Dict[str, Any]:
        assert self._process_runner is not None
        paths = self.case_service.paths_for(case_id, require_exists=True)
        run_id = context.task_id
        run_dir = paths.output / "inference_runs" / run_id
        prediction_dir = run_dir / "prediction"
        prediction_dir.mkdir(parents=True, exist_ok=False)
        record_path = run_dir / "run.json"
        current_record_path = paths.output / "inference.json"
        started_at = utc_now()
        record: Dict[str, Any] = {
            "schema_version": "2.0",
            "case_id": case_id,
            "run_id": run_id,
            "status": "running",
            "model": model,
            "parameters": {
                "device": self.config.device,
                "step_size": self.config.step_size,
                "disable_tta": self.config.disable_tta,
                "preprocess_processes": self.config.preprocess_processes,
                "export_processes": self.config.export_processes,
                "timeout_seconds": self.config.timeout_seconds,
            },
            "started_at": started_at.isoformat(),
            "updated_at": started_at.isoformat(),
        }
        atomic_write_json(record_path, record)
        atomic_write_json(current_record_path, record)
        lifecycle_log = paths.output / "model_lifecycle.log"
        lifecycle_started = False
        try:
            with self._execution_lock:
                if self.before_inference is not None:
                    self.before_inference(lifecycle_log)
                    lifecycle_started = True
                job = BrainNnunetJob(
                    case_id=case_id,
                    input_dir=paths.nnunet_input,
                    flair_path=paths.nnunet_input / f"{case_id}_0000.nii.gz",
                    output_dir=prediction_dir,
                    model_dir=self.config.model_dir,
                    data_root=self._data_root,
                    timeout_seconds=self.config.timeout_seconds,
                    fold=self.config.fold,
                    checkpoint=self.config.checkpoint,
                    device=self.config.device,
                    step_size=self.config.step_size,
                    preprocess_processes=self.config.preprocess_processes,
                    export_processes=self.config.export_processes,
                    disable_tta=self.config.disable_tta,
                    predict_command=self.predict_command,
                )
                result = BrainNnunetWorker(self._process_runner).run(job, context)
                predicted = prediction_dir / f"{case_id}.nii.gz"
                final_segmentation = paths.output / "segmentation.nii.gz"
                temporary = final_segmentation.with_name(f".{final_segmentation.name}.{uuid.uuid4().hex}.tmp")
                shutil.copy2(predicted, temporary)
                os.replace(temporary, final_segmentation)
                metrics = self.metrics_service.generate(case_id) if self.metrics_service else None
                self.case_service.transition_status(
                    case_id,
                    CaseStatus.RUNNING,
                    "脑胶质瘤分割完成，正在生成可视化",
                    95,
                )
                derived: Dict[str, Any] = {}
                warnings: list[dict[str, Any]] = []
                if self.visualization_service is not None:
                    try:
                        visualizations = self.visualization_service.generate(case_id)
                        derived["visualizations"] = "output/visualizations.json"
                        derived["mesh"] = visualizations.get("mesh", {}).get("filename")
                    except Exception as exc:
                        warnings.append(
                            {
                                "component": "visualizations",
                                "code": getattr(exc, "code", type(exc).__name__),
                                "message": redact_text(getattr(exc, "message", str(exc))),
                            }
                        )
                ended_at = utc_now()
                record.update(
                    {
                        "status": "completed",
                        "segmentation_file": "output/segmentation.nii.gz",
                        "metrics_file": "output/metrics.json" if metrics else None,
                        "derived_outputs": derived,
                        "derived_output_warnings": warnings,
                        "result_manifest": result.get("manifest"),
                        "resource_usage": result.get("resource_usage", {}),
                        "ended_at": ended_at.isoformat(),
                        "updated_at": ended_at.isoformat(),
                        "duration_seconds": round((ended_at - started_at).total_seconds(), 3),
                    }
                )
                atomic_write_json(record_path, record)
                atomic_write_json(current_record_path, record)
                return result
        except WorkerExecutionError:
            raise
        except Exception as exc:
            raise WorkerExecutionError(
                TaskErrorCode.INTERNAL_ERROR,
                "脑胶质瘤推理后处理失败",
                details={"component": type(exc).__name__},
            ) from exc
        finally:
            if lifecycle_started and self.after_inference is not None:
                self.after_inference(lifecycle_log)

    def _build_command(self, input_dir: Path, output_dir: Path) -> list[str]:
        prefix = self.predict_command or resolve_predict_command()
        command = [
            *prefix,
            "-i",
            str(input_dir),
            "-o",
            str(output_dir),
            "-m",
            str(self.config.model_dir),
            "-f",
            str(self.config.fold),
            "-chk",
            self.config.checkpoint,
            "-device",
            self.config.device,
            "-step_size",
            str(self.config.step_size),
            "-npp",
            str(self.config.preprocess_processes),
            "-nps",
            str(self.config.export_processes),
            "--disable_progress_bar",
        ]
        if self.config.disable_tta:
            command.append("--disable_tta")
        return command

    def _run_task(self, handle: InferenceTaskHandle, model: Dict[str, Any]) -> None:
        case_id = handle.case_id
        paths = self.case_service.paths_for(case_id, require_exists=True)
        run_dir = paths.output / "inference_runs" / handle.run_id
        prediction_dir = run_dir / "prediction"
        run_dir.mkdir(parents=True, exist_ok=False)
        prediction_dir.mkdir(parents=True, exist_ok=False)
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        run_record_path = run_dir / "run.json"
        current_record_path = paths.output / "inference.json"
        started_at = utc_now()
        record = {
            "schema_version": "1.0",
            "case_id": case_id,
            "run_id": handle.run_id,
            "status": "queued",
            "model": model,
            "parameters": {
                "device": self.config.device,
                "step_size": self.config.step_size,
                "disable_tta": self.config.disable_tta,
                "preprocess_processes": self.config.preprocess_processes,
                "export_processes": self.config.export_processes,
                "timeout_seconds": self.config.timeout_seconds,
            },
            "started_at": started_at.isoformat(),
            "updated_at": started_at.isoformat(),
        }
        atomic_write_json(run_record_path, record)
        atomic_write_json(current_record_path, record)

        gpu_lock: GpuFileLock | None = None
        lifecycle_started = False
        lifecycle_finished = False
        lifecycle_log = paths.output / "model_lifecycle.log"
        try:
            with self._execution_lock:
                if handle.cancel_event.is_set():
                    self._finish_cancelled(handle, record, run_record_path, current_record_path)
                    return

                lock_file = self.config.gpu_lock_file or (self.config.model_dir / ".gpu.lock")
                gpu_lock = GpuFileLock(lock_file, f"inference:{case_id}:{handle.run_id}")
                gpu_lock.acquire()

                if self.before_inference is not None:
                    self.before_inference(lifecycle_log)
                    lifecycle_started = True

                self.case_service.transition_status(
                    case_id,
                    CaseStatus.RUNNING,
                    "正在进行脑胶质瘤分割",
                    10,
                )
                command = self._build_command(paths.nnunet_input, prediction_dir)
                record.update(
                    {
                        "status": "running",
                        "runner": "nnUNetv2_predict_from_modelfolder",
                        "updated_at": utc_now().isoformat(),
                    }
                )
                atomic_write_json(run_record_path, record)
                atomic_write_json(current_record_path, record)

                environment = os.environ.copy()
                environment.setdefault("PYTHONUTF8", "0" if os.name == "nt" else "1")
                environment.setdefault("PYTHONIOENCODING", "utf-8")
                environment.setdefault("nnUNet_compile", "F")
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                process = subprocess.Popen(
                    command,
                    cwd=str(run_dir),
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creationflags,
                    start_new_session=os.name != "nt",
                )
                assert process.stdout is not None
                assert process.stderr is not None
                stdout_thread = Thread(
                    target=_write_redacted_process_stream,
                    args=(process.stdout, stdout_path),
                    name=f"nnunet-stdout-{handle.run_id}",
                    daemon=True,
                )
                stderr_thread = Thread(
                    target=_write_redacted_process_stream,
                    args=(process.stderr, stderr_path),
                    name=f"nnunet-stderr-{handle.run_id}",
                    daemon=True,
                )
                stdout_thread.start()
                stderr_thread.start()
                with self._tasks_lock:
                    handle.process = process
                deadline = time.monotonic() + self.config.timeout_seconds
                timed_out = False
                while process.poll() is None:
                    if handle.cancel_event.wait(0.2):
                        self._terminate_process_tree(process)
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        self._terminate_process_tree(process)
                        break
                return_code = process.wait(timeout=10)
                stdout_thread.join(timeout=10)
                stderr_thread.join(timeout=10)
                record["return_code"] = return_code

                if handle.cancel_event.is_set():
                    self._finish_cancelled(handle, record, run_record_path, current_record_path)
                    return
                if timed_out:
                    raise NnunetServiceError(
                        "INFERENCE_TIMEOUT",
                        "nnU-Net 推理超过配置的最长运行时间",
                    )
                if return_code != 0:
                    raise NnunetServiceError(
                        "NNUNET_PROCESS_FAILED",
                        "nnU-Net 推理进程执行失败",
                        {"return_code": return_code},
                    )

                predicted = prediction_dir / f"{case_id}.nii.gz"
                if not predicted.is_file():
                    raise NnunetServiceError(
                        "SEGMENTATION_OUTPUT_MISSING",
                        "nnU-Net 没有生成预期的分割文件",
                    )
                inspect_nifti(predicted)
                final_segmentation = paths.output / "segmentation.nii.gz"
                temporary_segmentation = final_segmentation.with_name(
                    f".{final_segmentation.name}.{uuid.uuid4().hex}.tmp"
                )
                shutil.copy2(predicted, temporary_segmentation)
                os.replace(temporary_segmentation, final_segmentation)
                metrics = self.metrics_service.generate(case_id) if self.metrics_service else None

                ended_at = utc_now()
                record.update(
                    {
                        "status": "completed",
                        "return_code": return_code,
                        "segmentation_file": "output/segmentation.nii.gz",
                        "metrics_file": "output/metrics.json" if metrics else None,
                        "metrics_algorithm": metrics.get("algorithm") if metrics else None,
                        "ended_at": ended_at.isoformat(),
                        "updated_at": ended_at.isoformat(),
                        "duration_seconds": round((ended_at - started_at).total_seconds(), 3),
                    }
                )
                atomic_write_json(run_record_path, record)
                atomic_write_json(current_record_path, record)
                self.case_service.transition_status(
                    case_id,
                    CaseStatus.COMPLETED,
                    "脑胶质瘤分割完成",
                    100,
                )
                derived_outputs: Dict[str, Any] = {}
                derived_warnings = []
                if self.visualization_service is not None:
                    try:
                        visualizations = self.visualization_service.generate(case_id)
                        derived_outputs["visualizations"] = "output/visualizations.json"
                        derived_outputs["mesh"] = visualizations.get("mesh", {}).get("filename")
                    except Exception as exc:
                        derived_warnings.append(
                            {
                                "component": "visualizations",
                                "code": getattr(exc, "code", type(exc).__name__),
                                "message": redact_text(getattr(exc, "message", str(exc))),
                            }
                        )
                record["derived_outputs"] = derived_outputs
                record["derived_output_warnings"] = derived_warnings
                atomic_write_json(run_record_path, record)
                atomic_write_json(current_record_path, record)
                if lifecycle_started and self.after_inference is not None:
                    self.after_inference(lifecycle_log)
                    lifecycle_finished = True
        except Exception as exc:
            if handle.cancel_event.is_set():
                self._finish_cancelled(handle, record, run_record_path, current_record_path)
            else:
                ended_at = utc_now()
                error_code = getattr(exc, "code", "INFERENCE_INTERNAL_ERROR")
                record.update(
                    {
                        "status": "failed",
                        "error_code": error_code,
                        "error": redact_text(getattr(exc, "message", str(exc))),
                        "ended_at": ended_at.isoformat(),
                        "updated_at": ended_at.isoformat(),
                        "duration_seconds": round((ended_at - started_at).total_seconds(), 3),
                    }
                )
                atomic_write_json(run_record_path, record)
                atomic_write_json(current_record_path, record)
                current = self.case_service.read_status(case_id)
                if current.status in {CaseStatus.QUEUED, CaseStatus.RUNNING}:
                    self.case_service.transition_status(
                        case_id,
                        CaseStatus.TIMED_OUT
                        if error_code == "INFERENCE_TIMEOUT"
                        else CaseStatus.FAILED,
                        record["error"],
                        100,
                    )
        finally:
            if lifecycle_started and not lifecycle_finished and self.after_inference is not None:
                with self._execution_lock:
                    self.after_inference(lifecycle_log)
            if gpu_lock is not None:
                gpu_lock.release()
            with self._tasks_lock:
                handle.process = None

    def _finish_cancelled(
        self,
        handle: InferenceTaskHandle,
        record: Dict[str, Any],
        run_record_path: Path,
        current_record_path: Path,
    ) -> None:
        ended_at = utc_now()
        record.update(
            {
                "status": "cancelled",
                "ended_at": ended_at.isoformat(),
                "updated_at": ended_at.isoformat(),
            }
        )
        atomic_write_json(run_record_path, record)
        atomic_write_json(current_record_path, record)
        current = self.case_service.read_status(handle.case_id)
        if current.status in {CaseStatus.QUEUED, CaseStatus.RUNNING}:
            self.case_service.transition_status(
                handle.case_id,
                CaseStatus.CANCELLED,
                "脑胶质瘤分割任务已取消",
                0,
            )

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen) -> None:
        terminate_process_tree(process)
