from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_text() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class TaskType(str, Enum):
    PPGL_V5 = "ppgl_v5"
    TOTALSEGMENTATOR = "totalsegmentator"
    BRAIN_NNUNET = "brain_nnunet"


class TaskErrorCode(str, Enum):
    COMMAND_NOT_FOUND = "COMMAND_NOT_FOUND"
    MODEL_WEIGHT_MISSING = "MODEL_WEIGHT_MISSING"
    MODEL_CONFIG_MISSING = "MODEL_CONFIG_MISSING"
    INPUT_MISSING = "INPUT_MISSING"
    INPUT_SPATIAL_MISMATCH = "INPUT_SPATIAL_MISMATCH"
    CUDA_OUT_OF_MEMORY = "CUDA_OUT_OF_MEMORY"
    GPU_QUEUE_BUSY = "GPU_QUEUE_BUSY"
    PROCESS_FAILED = "PROCESS_FAILED"
    PROCESS_TIMEOUT = "PROCESS_TIMEOUT"
    PROCESS_CANCELLED = "PROCESS_CANCELLED"
    OUTPUT_MISSING = "OUTPUT_MISSING"
    OUTPUT_EMPTY = "OUTPUT_EMPTY"
    OUTPUT_SPATIAL_MISMATCH = "OUTPUT_SPATIAL_MISMATCH"
    WORKER_RESTARTED = "WORKER_RESTARTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
    }
)

ALLOWED_TRANSITIONS = {
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.TIMED_OUT}
    ),
    TaskStatus.RUNNING: TERMINAL_STATUSES,
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.TIMED_OUT: frozenset(),
}


@dataclass
class TaskRecord:
    task_id: str
    task_type: TaskType
    case_id: str
    status: TaskStatus = TaskStatus.QUEUED
    progress: int = 0
    message: str = "任务已进入队列"
    error_code: str | None = None
    retryable: bool = False
    created_at: str = field(default_factory=utc_text)
    started_at: str | None = None
    ended_at: str | None = None
    updated_at: str = field(default_factory=utc_text)
    result: dict[str, Any] = field(default_factory=dict)
    resource_usage: dict[str, Any] = field(default_factory=dict)

    def transition(
        self,
        status: TaskStatus,
        *,
        message: str,
        progress: int | None = None,
        error_code: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        if status != self.status and status not in ALLOWED_TRANSITIONS[self.status]:
            raise ValueError(f"Invalid task transition: {self.status.value} -> {status.value}")
        now = utc_text()
        if status == TaskStatus.RUNNING and self.started_at is None:
            self.started_at = now
        if status in TERMINAL_STATUSES:
            self.ended_at = now
        self.status = status
        self.message = message
        self.progress = max(0, min(100, int(progress if progress is not None else self.progress)))
        self.error_code = error_code
        if retryable is not None:
            self.retryable = retryable
        self.updated_at = now

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["task_type"] = self.task_type.value
        payload["status"] = self.status.value
        return payload


RESULT_MANIFEST_SCHEMA = "medvision_result_v1"


def result_manifest(
    *,
    case_id: str,
    workflow: str,
    segmentation: str,
    metrics: str,
    visualization_manifest: str,
    mesh: str,
    report: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": RESULT_MANIFEST_SCHEMA,
        "case_id": case_id,
        "workflow": workflow,
        "outputs": {
            "segmentation": segmentation,
            "metrics": metrics,
            "visualization_manifest": visualization_manifest,
            "mesh": mesh,
            "report": report,
        },
    }

