from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from queue import Queue
from threading import Lock, Thread
import time
from typing import Any, Callable
import uuid

from backend.domain.task_contract import (
    TERMINAL_STATUSES,
    TaskErrorCode,
    TaskRecord,
    TaskStatus,
    TaskType,
    utc_text,
)
from backend.workers.process_runner import TaskExecutionContext, WorkerExecutionError


TaskHandler = Callable[[TaskExecutionContext], dict[str, Any]]
TaskObserver = Callable[[dict[str, Any]], None]


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@dataclass
class QueuedTask:
    record: TaskRecord
    handler: TaskHandler
    context: TaskExecutionContext
    record_path: Path
    observer: TaskObserver | None = None


class UnifiedGpuTaskQueue:
    """One-process FIFO queue shared by all GPU inference workers."""

    def __init__(self, tasks_dir: Path, *, autostart: bool = True):
        self.tasks_dir = tasks_dir.expanduser().resolve()
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._queue: Queue[QueuedTask | None] = Queue()
        self._tasks: dict[str, QueuedTask] = {}
        self._lock = Lock()
        self._thread: Thread | None = None
        self._closed = False
        self.recover_interrupted_tasks()
        if autostart:
            self.start()

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Task queue is closed")
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = Thread(target=self._run_loop, name="medvision-gpu-worker", daemon=True)
            self._thread.start()

    def submit(
        self,
        *,
        task_type: TaskType,
        case_id: str,
        handler: TaskHandler,
        task_id: str | None = None,
        observer: TaskObserver | None = None,
    ) -> dict[str, Any]:
        identifier = task_id or f"task_{uuid.uuid4().hex}"
        if not identifier.startswith("task_") or not identifier.replace("_", "").isalnum():
            raise ValueError("Invalid task identifier")
        record = TaskRecord(task_id=identifier, task_type=task_type, case_id=case_id)
        item = QueuedTask(
            record=record,
            handler=handler,
            context=TaskExecutionContext(identifier),
            record_path=self.tasks_dir / identifier / "task.json",
            observer=observer,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("Task queue is closed")
            if identifier in self._tasks or item.record_path.exists():
                raise ValueError("Task identifier already exists")
            if any(
                current.record.case_id == case_id
                and current.record.status not in TERMINAL_STATUSES
                for current in self._tasks.values()
            ):
                raise WorkerExecutionError(
                    TaskErrorCode.GPU_QUEUE_BUSY,
                    "该病例已有排队或运行中的推理任务",
                    retryable=True,
                )
            self._tasks[identifier] = item
            self._persist(item)
            self._queue.put(item)
        self._notify(item)
        return item.record.to_dict()

    def cancel(self, task_id: str) -> dict[str, Any]:
        item = self._require_task(task_id)
        with self._lock:
            if item.record.status in TERMINAL_STATUSES:
                return item.record.to_dict()
            item.context.cancel_event.set()
            if item.record.status == TaskStatus.QUEUED:
                item.record.transition(
                    TaskStatus.CANCELLED,
                    message="任务已在队列中取消",
                    progress=0,
                    error_code=TaskErrorCode.PROCESS_CANCELLED.value,
                )
                self._persist(item)
                self._notify(item)
        item.context.cancel()
        return item.record.to_dict()

    def get(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._tasks.get(task_id)
        if item is not None:
            return item.record.to_dict()
        path = self._record_path(task_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KeyError(task_id) from exc
        if not isinstance(payload, dict):
            raise KeyError(task_id)
        return payload

    def latest_for_case(self, case_id: str) -> dict[str, Any] | None:
        with self._lock:
            in_memory = [
                item.record.to_dict()
                for item in self._tasks.values()
                if item.record.case_id == case_id
            ]
        records = list(in_memory)
        known_ids = {record.get("task_id") for record in records}
        for path in self.tasks_dir.glob("task_*/task.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(payload, dict)
                and payload.get("case_id") == case_id
                and payload.get("task_id") not in known_ids
            ):
                records.append(payload)
        if not records:
            return None
        return max(records, key=lambda record: str(record.get("created_at", "")))

    def is_case_active(self, case_id: str) -> bool:
        with self._lock:
            return any(
                item.record.case_id == case_id and item.record.status not in TERMINAL_STATUSES
                for item in self._tasks.values()
            )

    def wait(self, task_id: str, timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.get(task_id)
            if TaskStatus(record["status"]) in TERMINAL_STATUSES:
                return record
            time.sleep(0.05)
        raise TimeoutError(f"Task did not finish: {task_id}")

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = [item for item in self._tasks.values() if item.record.status == TaskStatus.RUNNING]
        for item in active:
            item.context.cancel()
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def recover_interrupted_tasks(self) -> int:
        recovered = 0
        for path in self.tasks_dir.glob("task_*/task.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                status = TaskStatus(payload.get("status"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if status not in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                continue
            payload.update(
                {
                    "status": TaskStatus.FAILED.value,
                    "message": "服务重启中断了上一次推理任务，可安全重试",
                    "error_code": TaskErrorCode.WORKER_RESTARTED.value,
                    "retryable": True,
                    "progress": 100,
                    "ended_at": utc_text(),
                    "updated_at": utc_text(),
                }
            )
            atomic_write_json(path, payload)
            recovered += 1
        return recovered

    def _run_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            try:
                self._run_item(item)
            finally:
                self._queue.task_done()

    def _run_item(self, item: QueuedTask) -> None:
        with self._lock:
            if item.record.status == TaskStatus.CANCELLED or item.context.cancel_event.is_set():
                return
            item.record.transition(TaskStatus.RUNNING, message="推理任务正在运行", progress=5)
            self._persist(item)
            self._notify(item)
        try:
            result = item.handler(item.context)
            with self._lock:
                if item.context.cancel_event.is_set():
                    if item.record.status not in TERMINAL_STATUSES:
                        item.record.transition(
                            TaskStatus.CANCELLED,
                            message="推理任务已取消",
                            progress=0,
                            error_code=TaskErrorCode.PROCESS_CANCELLED.value,
                        )
                else:
                    item.record.result = dict(result or {})
                    usage = item.record.result.pop("resource_usage", None)
                    if isinstance(usage, dict):
                        item.record.resource_usage = usage
                    item.record.transition(
                        TaskStatus.COMPLETED,
                        message="推理任务已完成",
                        progress=100,
                    )
                self._persist(item)
                self._notify(item)
        except WorkerExecutionError as exc:
            with self._lock:
                status = (
                    TaskStatus.CANCELLED
                    if exc.code == TaskErrorCode.PROCESS_CANCELLED.value
                    else TaskStatus.TIMED_OUT
                    if exc.code == TaskErrorCode.PROCESS_TIMEOUT.value
                    else TaskStatus.FAILED
                )
                item.record.transition(
                    status,
                    message=exc.message,
                    progress=100 if status != TaskStatus.CANCELLED else 0,
                    error_code=exc.code,
                    retryable=exc.retryable,
                )
                self._persist(item)
                self._notify(item)
        except Exception:
            with self._lock:
                item.record.transition(
                    TaskStatus.FAILED,
                    message="推理 Worker 发生内部错误",
                    progress=100,
                    error_code=TaskErrorCode.INTERNAL_ERROR.value,
                    retryable=False,
                )
                self._persist(item)
                self._notify(item)

    def _persist(self, item: QueuedTask) -> None:
        atomic_write_json(item.record_path, item.record.to_dict())

    @staticmethod
    def _notify(item: QueuedTask) -> None:
        if item.observer is None:
            return
        try:
            item.observer(item.record.to_dict())
        except Exception:
            # 状态观察器不能破坏推理队列本身；调用方应自行记录观察器错误。
            return

    def _record_path(self, task_id: str) -> Path:
        if not task_id.startswith("task_") or not task_id.replace("_", "").isalnum():
            raise KeyError(task_id)
        return self.tasks_dir / task_id / "task.json"

    def _require_task(self, task_id: str) -> QueuedTask:
        with self._lock:
            item = self._tasks.get(task_id)
        if item is None:
            raise KeyError(task_id)
        return item
