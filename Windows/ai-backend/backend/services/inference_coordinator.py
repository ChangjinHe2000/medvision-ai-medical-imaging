from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Any, Callable

from backend.config import AppSettings
from backend.domain.task_contract import TaskStatus, TaskType
from backend.workers.ppgl_v5.worker import ProgressPatchV5Job, ProgressPatchV5Worker
from backend.workers.process_runner import ProcessRunner, TaskExecutionContext
from backend.workers.task_queue import UnifiedGpuTaskQueue
from backend.workers.totalsegmentator.worker import TotalSegmentatorJob, TotalSegmentatorWorker


TaskObserver = Callable[[dict[str, Any]], None]
LifecycleHook = Callable[[Path | None], None]
ResultHook = Callable[[Path], dict[str, Any] | None]


class InferenceCoordinator:
    """Compatibility facade routing CT workers through one Windows-safe GPU queue."""

    def __init__(
        self,
        settings: AppSettings,
        *,
        queue: UnifiedGpuTaskQueue | None = None,
        runner: ProcessRunner | None = None,
        execution_lock: Lock | None = None,
    ):
        self.settings = settings
        self.runner = runner or ProcessRunner(
            redaction_roots=(
                settings.project_root,
                settings.data_root,
                settings.ppgl_model_dir,
                settings.totalseg_weights_path,
                settings.brain_model_dir,
            )
        )
        self.queue = queue or UnifiedGpuTaskQueue(settings.tasks_dir)
        self.ppgl_worker = ProgressPatchV5Worker(self.runner)
        self.totalseg_worker = TotalSegmentatorWorker(self.runner)
        self.execution_lock = execution_lock or Lock()
        self._case_tasks: dict[tuple[str, TaskType], str] = {}
        self._lock = Lock()

    def submit_ppgl(
        self,
        *,
        case_id: str,
        image_path: Path,
        output_dir: Path,
        device: str,
        observer: TaskObserver | None = None,
        before_inference: LifecycleHook | None = None,
        after_inference: LifecycleHook | None = None,
        result_hook: ResultHook | None = None,
    ) -> dict[str, Any]:
        job = ProgressPatchV5Job(
            case_id=case_id,
            image_path=image_path,
            output_dir=output_dir,
            model_dir=self.settings.ppgl_model_dir,
            checkpoint=self.settings.ppgl_checkpoint,
            model_config=self.settings.ppgl_model_config,
            data_root=self.settings.data_root,
            timeout_seconds=self.settings.task_timeout_seconds,
            device=device,
        )

        def handler(context: TaskExecutionContext) -> dict[str, Any]:
            lifecycle_log = output_dir / "logs" / "model_lifecycle.log"
            with self.execution_lock:
                if before_inference is not None:
                    before_inference(lifecycle_log)
                try:
                    result = self.ppgl_worker.run(job, context)
                    if result_hook is not None:
                        derived = result_hook(output_dir) or {}
                        result["derived_outputs"] = derived
                    return result
                finally:
                    if after_inference is not None:
                        after_inference(lifecycle_log)

        return self._submit(TaskType.PPGL_V5, case_id, handler, observer)

    def submit_totalsegmentator(
        self,
        *,
        case_id: str,
        image_path: Path,
        output_dir: Path,
        mode: str,
        device: str,
        fast: bool = False,
        fastest: bool = False,
        force: bool = False,
        existing_masks_dir: Path | None = None,
        observer: TaskObserver | None = None,
        before_inference: LifecycleHook | None = None,
        after_inference: LifecycleHook | None = None,
    ) -> dict[str, Any]:
        job = TotalSegmentatorJob(
            case_id=case_id,
            image_path=image_path,
            output_dir=output_dir,
            ai_backend_dir=self.settings.project_root / "ai-backend",
            data_root=self.settings.data_root,
            weights_path=self.settings.totalseg_weights_path,
            home_dir=self.settings.totalseg_home_dir,
            timeout_seconds=self.settings.task_timeout_seconds,
            mode=mode,
            device=device,
            fast=fast,
            fastest=fastest,
            node_bin=self.settings.node_bin,
            gltfpack_bin=self.settings.gltfpack_bin,
            existing_masks_dir=existing_masks_dir,
            force=force,
        )

        def handler(context: TaskExecutionContext) -> dict[str, Any]:
            lifecycle_log = output_dir / "logs" / "model_lifecycle.log"
            with self.execution_lock:
                if before_inference is not None:
                    before_inference(lifecycle_log)
                try:
                    return self.totalseg_worker.run(job, context)
                finally:
                    if after_inference is not None:
                        after_inference(lifecycle_log)

        return self._submit(TaskType.TOTALSEGMENTATOR, case_id, handler, observer)

    def cancel(self, case_id: str, task_type: TaskType) -> dict[str, Any]:
        with self._lock:
            task_id = self._case_tasks.get((case_id, task_type))
        if not task_id:
            latest = self.queue.latest_for_case(case_id)
            if latest and latest.get("task_type") == task_type.value:
                task_id = str(latest["task_id"])
        if not task_id:
            raise KeyError(case_id)
        return self.queue.cancel(task_id)

    def task_state(self, case_id: str, task_type: TaskType) -> dict[str, Any] | None:
        with self._lock:
            task_id = self._case_tasks.get((case_id, task_type))
        if task_id:
            return self.queue.get(task_id)
        latest = self.queue.latest_for_case(case_id)
        if latest and latest.get("task_type") == task_type.value:
            return latest
        return None

    def _submit(
        self,
        task_type: TaskType,
        case_id: str,
        handler: Callable[[TaskExecutionContext], dict[str, Any]],
        observer: TaskObserver | None,
    ) -> dict[str, Any]:
        record = self.queue.submit(
            task_type=task_type,
            case_id=case_id,
            handler=handler,
            observer=observer,
        )
        with self._lock:
            self._case_tasks[(case_id, task_type)] = str(record["task_id"])
        return record

    @staticmethod
    def legacy_progress(record: dict[str, Any]) -> tuple[str, str, int, dict[str, Any]]:
        status = TaskStatus(str(record["status"]))
        extra: dict[str, Any] = {
            "task_id": record.get("task_id"),
            "error_code": record.get("error_code"),
            "retryable": bool(record.get("retryable", False)),
        }
        if record.get("result"):
            extra["worker_result"] = record["result"]
        return status.value, str(record.get("message", "")), int(record.get("progress", 0)), extra
