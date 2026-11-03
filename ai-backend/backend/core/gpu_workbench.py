from __future__ import annotations

import csv
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
from threading import Event, RLock, Thread
import time
from typing import Any, Callable
import urllib.error
import urllib.request
import uuid


TASK_LABELS = {
    "organ": "全器官分割",
    "ppgl": "PPGL 肿瘤分割",
    "glioma": "脑胶质瘤分割",
}
RUNTIME_ACTIVITY_LABELS = {
    "rag": "知识库问答",
    "llm": "AI 问答",
    "report": "AI 报告问答",
}
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def seconds_between(started_at: str, ended_at: str | None = None) -> float | None:
    started = parse_iso(started_at)
    ended = parse_iso(ended_at or now_iso())
    if started is None or ended is None:
        return None
    return round(max(0.0, (ended - started).total_seconds()), 3)


class GpuWorkbench:
    """Persisted GPU telemetry and admission state for local inference jobs."""

    def __init__(self, data_root: Path):
        self.root = data_root / "gpu-workbench"
        self.task_store = self.root / "tasks.json"
        self.telemetry_store = self.root / "telemetry.json"
        self._lock = RLock()
        self._runtime_activities: dict[str, dict[str, Any]] = {}
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._dispatch_wakeup = Event()
        self._dispatch_stop = Event()
        self._dispatch_thread: Thread | None = None

    @staticmethod
    def _int_env(name: str, default: int, minimum: int = 0) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except ValueError:
            return default
        return max(minimum, value)

    @property
    def reserve_mb(self) -> int:
        return self._int_env("PPGL_GPU_SYSTEM_RESERVE_MB", 2048)

    @property
    def poll_seconds(self) -> int:
        return self._int_env("PPGL_GPU_QUEUE_POLL_SECONDS", 5, minimum=1)

    @property
    def telemetry_sample_seconds(self) -> int:
        return self._int_env("PPGL_GPU_TELEMETRY_SAMPLE_SECONDS", 5, minimum=2)

    @property
    def telemetry_retention_minutes(self) -> int:
        return self._int_env("PPGL_GPU_TELEMETRY_RETENTION_MINUTES", 720, minimum=30)

    def estimated_vram_mb(self, task_type: str) -> int:
        defaults = {"organ": 16384, "ppgl": 12288, "glioma": 16384}
        env_names = {
            "organ": "PPGL_GPU_ORGAN_REQUIRED_MB",
            "ppgl": "PPGL_GPU_PPGL_REQUIRED_MB",
            "glioma": "PPGL_GPU_GLIOMA_REQUIRED_MB",
        }
        return self._int_env(env_names.get(task_type, "PPGL_GPU_GENERIC_REQUIRED_MB"), defaults.get(task_type, 12288), minimum=1)

    def required_free_mb(self, task_type: str) -> int:
        return self.estimated_vram_mb(task_type) + self.reserve_mb

    def _read_store(self) -> dict[str, Any]:
        if not self.task_store.is_file():
            return {"schema_version": "gpu_workbench_tasks_v1", "tasks": []}
        try:
            payload = json.loads(self.task_store.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": "gpu_workbench_tasks_v1", "tasks": []}
        tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
        return {
            "schema_version": "gpu_workbench_tasks_v1",
            "tasks": tasks if isinstance(tasks, list) else [],
        }

    def _write_store(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tasks = payload.get("tasks", [])[-120:]
        output = {"schema_version": "gpu_workbench_tasks_v1", "tasks": tasks}
        temporary = self.task_store.with_name(f"{self.task_store.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.task_store)

    def _read_telemetry(self) -> list[dict[str, Any]]:
        if not self.telemetry_store.is_file():
            return []
        try:
            payload = json.loads(self.telemetry_store.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        samples = payload.get("samples", []) if isinstance(payload, dict) else []
        return [dict(item) for item in samples if isinstance(item, dict)]

    def _write_telemetry(self, samples: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        output = {"schema_version": "gpu_workbench_telemetry_v1", "samples": samples}
        temporary = self.telemetry_store.with_name(f"{self.telemetry_store.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.telemetry_store)

    def _append_telemetry(
        self,
        captured_at: str,
        gpu: dict[str, Any],
        models: list[dict[str, Any]],
        active_tasks: list[dict[str, Any]],
        runtime_activities: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        primary = gpu.get("primary") if isinstance(gpu, dict) else None
        primary = primary if isinstance(primary, dict) else {}
        active_task = next((item for item in active_tasks if item.get("status") == "running"), None)
        if active_task is None and active_tasks:
            active_task = active_tasks[0]
        active_activity = runtime_activities[0] if runtime_activities else None
        sample = {
            "captured_at": captured_at,
            "memory_total_mb": primary.get("memory_total_mb"),
            "memory_used_mb": primary.get("memory_used_mb"),
            "memory_free_mb": primary.get("memory_free_mb"),
            "utilization_percent": primary.get("utilization_percent"),
            "loaded_model_count": len(models),
            "active_task_type": active_task.get("task_type") if isinstance(active_task, dict) else None,
            "runtime_activity_type": active_activity.get("activity_type") if isinstance(active_activity, dict) else None,
        }
        with self._lock:
            samples = self._read_telemetry()
            last_time = parse_iso(str(samples[-1].get("captured_at", ""))) if samples else None
            current_time = parse_iso(captured_at)
            changed = False
            if last_time is None or current_time is None or (current_time - last_time).total_seconds() >= self.telemetry_sample_seconds:
                samples.append(sample)
                changed = True

            cutoff = datetime.now().astimezone().timestamp() - self.telemetry_retention_minutes * 60
            retained_samples = [
                item
                for item in samples
                if (parsed := parse_iso(str(item.get("captured_at", "")))) is not None and parsed.timestamp() >= cutoff
            ]
            if len(retained_samples) != len(samples):
                changed = True
            if changed:
                self._write_telemetry(retained_samples)
            return retained_samples

    def start_runtime_activity(
        self,
        activity_type: str,
        label: str | None = None,
        model: str | None = None,
    ) -> str:
        activity_id = f"runtime_{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._runtime_activities[activity_id] = {
                "activity_id": activity_id,
                "activity_type": activity_type,
                "label": label or RUNTIME_ACTIVITY_LABELS.get(activity_type, activity_type),
                "model": (model or "").strip(),
                "started_at": now_iso(),
            }
        return activity_id

    def finish_runtime_activity(self, activity_id: str) -> None:
        with self._lock:
            self._runtime_activities.pop(activity_id, None)

    def runtime_activities(self) -> list[dict[str, Any]]:
        with self._lock:
            activities = [dict(item) for item in self._runtime_activities.values()]
        activities.sort(key=lambda item: str(item.get("started_at", "")))
        for item in activities:
            item["elapsed_seconds"] = seconds_between(str(item.get("started_at", "")))
        return activities

    @staticmethod
    def _matching_active_task(tasks: list[dict[str, Any]], task_type: str, case_id: str) -> dict[str, Any] | None:
        for task in reversed(tasks):
            if (
                task.get("task_type") == task_type
                and task.get("case_id") == case_id
                and task.get("status") not in TERMINAL_STATUSES
            ):
                return task
        return None

    def submit(
        self,
        task_type: str,
        case_id: str,
        payload: dict[str, Any] | None = None,
        trace_id: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            store = self._read_store()
            existing = self._matching_active_task(store["tasks"], task_type, case_id)
            if existing is not None:
                return dict(existing)
            submitted_at = now_iso()
            task = {
                "task_id": f"gpu_{uuid.uuid4().hex[:12]}",
                "task_type": task_type,
                "task_label": TASK_LABELS.get(task_type, task_type),
                "case_id": case_id,
                "status": "queued",
                "submitted_at": submitted_at,
                "estimated_vram_mb": self.estimated_vram_mb(task_type),
                "required_free_mb": self.required_free_mb(task_type),
                "payload": dict(payload or {}),
                "trace_id": str(trace_id or ""),
                "attempt": 0,
            }
            store["tasks"].append(task)
            self._write_store(store)
        self._dispatch_wakeup.set()
        return dict(task)

    def register_handler(self, task_type: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        """Register the single-process handler used by the durable segmentation queue."""

        with self._lock:
            self._handlers[task_type] = handler

    def start_dispatcher(self) -> None:
        """Resume interrupted work and start the one-at-a-time local GPU dispatcher."""

        with self._lock:
            self._recover_interrupted_tasks_locked()
            if self._dispatch_thread is not None and self._dispatch_thread.is_alive():
                self._dispatch_wakeup.set()
                return
            self._dispatch_stop.clear()
            self._dispatch_thread = Thread(
                target=self._dispatch_loop,
                name="ppgl-segmentation-dispatcher",
                daemon=True,
            )
            self._dispatch_thread.start()

    def stop_dispatcher(self) -> None:
        self._dispatch_stop.set()
        self._dispatch_wakeup.set()
        thread = self._dispatch_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def recover_interrupted_tasks(self) -> int:
        with self._lock:
            recovered = self._recover_interrupted_tasks_locked()
        if recovered:
            self._dispatch_wakeup.set()
        return recovered

    def _recover_interrupted_tasks_locked(self) -> int:
        store = self._read_store()
        recovered = 0
        for task in store["tasks"]:
            if task.get("status") not in {"waiting_for_gpu", "running"}:
                continue
            task.update(
                {
                    "status": "queued",
                    "message": "检测到服务重启，任务已重新排队",
                    "recovered_at": now_iso(),
                    "recovery_count": int(task.get("recovery_count", 0) or 0) + 1,
                }
            )
            recovered += 1
        if recovered:
            self._write_store(store)
        return recovered

    def find_active_task(self, task_type: str, case_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._matching_active_task(self._read_store()["tasks"], task_type, case_id)
            return dict(task) if task is not None else None

    def cancel(self, task_type: str, case_id: str) -> dict[str, Any] | None:
        """Cancel a task that has not started inference yet.

        A running inference is deliberately not killed here: its owner must terminate the
        subprocess safely before the task can become cancelled.
        """

        with self._lock:
            store = self._read_store()
            task = self._matching_active_task(store["tasks"], task_type, case_id)
            if task is None:
                return None
            if task.get("status") in {"queued", "waiting_for_gpu"}:
                task.update(
                    {
                        "status": "cancelled",
                        "ended_at": now_iso(),
                        "message": "任务已取消",
                        "run_seconds": 0,
                    }
                )
                self._write_store(store)
                return dict(task)
            return dict(task)

    def _claim_next_task(self) -> dict[str, Any] | None:
        with self._lock:
            store = self._read_store()
            queued = [task for task in store["tasks"] if task.get("status") == "queued"]
            queued.sort(key=lambda item: (str(item.get("submitted_at", "")), str(item.get("task_id", ""))))
            for task in queued:
                if task.get("task_type") not in self._handlers:
                    continue
                task.update(
                    {
                        "status": "running",
                        "started_at": now_iso(),
                        "attempt": int(task.get("attempt", 0) or 0) + 1,
                        "message": "任务已由本地调度器领取",
                    }
                )
                self._write_store(store)
                return dict(task)
        return None

    def _task_by_id(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            for task in self._read_store()["tasks"]:
                if task.get("task_id") == task_id:
                    return dict(task)
        return None

    def _dispatch_loop(self) -> None:
        while not self._dispatch_stop.is_set():
            task = self._claim_next_task()
            if task is None:
                self._dispatch_wakeup.wait(timeout=self.poll_seconds)
                self._dispatch_wakeup.clear()
                continue
            handler = self._handlers.get(str(task.get("task_type", "")))
            if handler is None:
                continue
            try:
                outcome = handler(task)
                if outcome in TERMINAL_STATUSES:
                    current = self._task_by_id(str(task.get("task_id", "")))
                    if current is not None and current.get("status") not in TERMINAL_STATUSES:
                        self.finish(
                            str(task.get("task_type", "")),
                            str(task.get("case_id", "")),
                            str(outcome),
                        )
                current = self._task_by_id(str(task.get("task_id", "")))
                if current is not None and current.get("status") not in TERMINAL_STATUSES:
                    self.finish(str(task.get("task_type", "")), str(task.get("case_id", "")), "completed")
            except Exception as exc:
                current = self._task_by_id(str(task.get("task_id", "")))
                if current is not None and current.get("status") not in TERMINAL_STATUSES:
                    self.finish(str(task.get("task_type", "")), str(task.get("case_id", "")), "failed")
                with self._lock:
                    store = self._read_store()
                    for item in store["tasks"]:
                        if item.get("task_id") == task.get("task_id"):
                            item["error"] = str(exc)[:500]
                            item["message"] = "任务执行失败"
                            break
                    self._write_store(store)

    def _ensure_task(self, task_type: str, case_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        store = self._read_store()
        task = self._matching_active_task(store["tasks"], task_type, case_id)
        if task is None:
            task = {
                "task_id": f"gpu_{uuid.uuid4().hex[:12]}",
                "task_type": task_type,
                "task_label": TASK_LABELS.get(task_type, task_type),
                "case_id": case_id,
                "status": "queued",
                "submitted_at": now_iso(),
                "estimated_vram_mb": self.estimated_vram_mb(task_type),
                "required_free_mb": self.required_free_mb(task_type),
            }
            store["tasks"].append(task)
        return store, task

    def gpu_snapshot(self) -> dict[str, Any]:
        command = [
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu",
            "--format=csv,noheader,nounits",
        ]
        try:
            completed = subprocess.run(command, text=True, capture_output=True, timeout=3, check=False)
        except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
            return {"available": False, "reason": str(exc), "gpus": [], "primary": None}
        if completed.returncode != 0:
            return {
                "available": False,
                "reason": (completed.stderr or "nvidia-smi failed").strip(),
                "gpus": [],
                "primary": None,
            }

        rows = []
        for row in csv.reader(line for line in completed.stdout.splitlines() if line.strip()):
            if len(row) != 6:
                continue
            try:
                rows.append(
                    {
                        "index": int(row[0].strip()),
                        "name": row[1].strip(),
                        "memory_total_mb": int(row[2].strip()),
                        "memory_used_mb": int(row[3].strip()),
                        "memory_free_mb": int(row[4].strip()),
                        "utilization_percent": int(row[5].strip()),
                    }
                )
            except ValueError:
                continue
        primary = next((item for item in rows if item["index"] == 0), rows[0] if rows else None)
        return {"available": bool(rows), "reason": "", "gpus": rows, "primary": primary}

    @staticmethod
    def _ollama_base_url() -> str:
        base_url = os.environ.get("CHAT_OPENAI_BASE_URL", "http://127.0.0.1:11434/v1").strip().rstrip("/")
        return base_url[:-3] if base_url.endswith("/v1") else base_url

    def loaded_models(self) -> list[dict[str, Any]]:
        request = urllib.request.Request(f"{self._ollama_base_url()}/api/ps", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError):
            return []
        models = payload.get("models", []) if isinstance(payload, dict) else []
        output = []
        for model in models if isinstance(models, list) else []:
            if not isinstance(model, dict):
                continue
            size_vram = model.get("size_vram", 0)
            try:
                size_vram_mb = round(int(size_vram) / 1024 / 1024, 1)
            except (TypeError, ValueError):
                size_vram_mb = 0.0
            output.append(
                {
                    "name": str(model.get("name", "")),
                    "size_vram_mb": size_vram_mb,
                    "expires_at": str(model.get("expires_at", "")),
                }
            )
        return output

    def unload_loaded_models(self) -> list[str]:
        unloaded: list[str] = []
        for model in self.loaded_models():
            name = model.get("name", "")
            if not name:
                continue
            request = urllib.request.Request(
                f"{self._ollama_base_url()}/api/generate",
                data=json.dumps({"model": name, "keep_alive": 0}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=30):
                    unloaded.append(name)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
                continue
        return unloaded

    def begin(
        self,
        task_type: str,
        case_id: str,
        on_wait: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Wait for the configured GPU reserve, unloading LLMs once if needed."""
        with self._lock:
            store, task = self._ensure_task(task_type, case_id)
            task_id = str(task.get("task_id", ""))
            required_free_mb = self.required_free_mb(task_type)
            task.update(
                {
                    "status": "waiting_for_gpu",
                    "estimated_vram_mb": self.estimated_vram_mb(task_type),
                    "required_free_mb": required_free_mb,
                    "admission_checked_at": now_iso(),
                }
            )
            self._write_store(store)

        unloaded_models: list[str] = []
        unload_attempted = False
        waited = False
        while True:
            snapshot = self.gpu_snapshot()
            primary = snapshot.get("primary") or {}
            free_mb = primary.get("memory_free_mb")
            if not snapshot.get("available") or not isinstance(free_mb, int) or free_mb >= required_free_mb:
                break
            waited = True
            if on_wait is not None:
                on_wait(free_mb, required_free_mb)
            if not unload_attempted:
                unload_attempted = True
                unloaded_models = self.unload_loaded_models()
                continue
            time.sleep(self.poll_seconds)

        with self._lock:
            store = self._read_store()
            task = next((item for item in store["tasks"] if item.get("task_id") == task_id), None)
            if task is None or task.get("status") == "cancelled":
                raise RuntimeError("任务已取消")
            started_at = now_iso()
            task.update(
                {
                    "status": "running",
                    "started_at": started_at,
                    "queue_wait_seconds": seconds_between(str(task.get("submitted_at", "")), started_at),
                    "gpu_before": snapshot,
                    "admission": "started_after_wait" if waited else "started",
                }
            )
            if unloaded_models:
                task["unloaded_models"] = unloaded_models
            self._write_store(store)
            return dict(task)

    def finish(self, task_type: str, case_id: str, status: str) -> None:
        if status not in TERMINAL_STATUSES:
            status = "failed"
        with self._lock:
            store = self._read_store()
            task = self._matching_active_task(store["tasks"], task_type, case_id)
            if task is None:
                return
            ended_at = now_iso()
            task.update(
                {
                    "status": status,
                    "ended_at": ended_at,
                    "run_seconds": seconds_between(str(task.get("started_at", "")), ended_at),
                    "gpu_after": self.gpu_snapshot(),
                }
            )
            self._write_store(store)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            tasks = [dict(item) for item in self._read_store()["tasks"] if isinstance(item, dict)]
        active = [item for item in tasks if item.get("status") not in TERMINAL_STATUSES]
        active.sort(key=lambda item: str(item.get("submitted_at", "")))
        for position, task in enumerate(active, start=1):
            task["queue_position"] = position
            task["queue_age_seconds"] = seconds_between(str(task.get("submitted_at", "")))

        completed = [item for item in tasks if item.get("status") in TERMINAL_STATUSES]
        completed.sort(key=lambda item: str(item.get("ended_at", "")), reverse=True)
        succeeded = sum(item.get("status") == "completed" for item in completed)
        runtimes = [item.get("run_seconds") for item in completed if isinstance(item.get("run_seconds"), (int, float))]
        generated_at = now_iso()
        gpu = self.gpu_snapshot()
        models = self.loaded_models()
        runtime_activities = self.runtime_activities()
        telemetry_samples = self._append_telemetry(
            generated_at,
            gpu,
            models,
            active,
            runtime_activities,
        )
        trend_cutoff = datetime.now().astimezone().timestamp() - 30 * 60
        recent_telemetry = [
            item
            for item in telemetry_samples
            if (parsed := parse_iso(str(item.get("captured_at", "")))) is not None and parsed.timestamp() >= trend_cutoff
        ]
        primary_task = next((item for item in active if item.get("status") == "running"), None)
        if primary_task is None and active:
            primary_task = active[0]
        primary_activity = runtime_activities[0] if runtime_activities else None
        if primary_task is not None:
            activity = {
                "state": str(primary_task.get("status", "queued")),
                "label": str(primary_task.get("task_label", "影像分割任务")),
                "detail": "影像分割任务正在占用或等待 GPU 资源。",
                "task_id": primary_task.get("task_id"),
                "case_id": primary_task.get("case_id"),
            }
        elif primary_activity is not None:
            activity = {
                "state": "running",
                "label": str(primary_activity.get("label", "AI 推理")),
                "detail": "本地语言模型正在处理 AI 请求。",
                "activity_id": primary_activity.get("activity_id"),
                "model": primary_activity.get("model", ""),
            }
        elif models:
            activity = {
                "state": "ready",
                "label": "本地模型待命",
                "detail": "当前没有运行中的 GPU 任务，本地模型已驻留。",
            }
        else:
            activity = {
                "state": "idle",
                "label": "GPU 空闲",
                "detail": "当前没有运行中的影像分割或本地 AI 推理任务。",
            }
        return {
            "generated_at": generated_at,
            "gpu": gpu,
            "models": models,
            "activity": activity,
            "runtime_activities": runtime_activities,
            "telemetry": {
                "window_minutes": 30,
                "sample_interval_seconds": self.telemetry_sample_seconds,
                "samples": recent_telemetry,
            },
            "capacity_policy": {
                "system_reserve_mb": self.reserve_mb,
                "poll_seconds": self.poll_seconds,
                "task_estimates_mb": {key: self.estimated_vram_mb(key) for key in TASK_LABELS},
                "admission_rule": "分割任务需保留估算显存与系统安全余量；不足时先卸载本地 LLM，再等待显存释放。",
            },
            "active_tasks": active,
            "recent_tasks": completed[:20],
            "summary": {
                "active": len(active),
                "completed": len(completed),
                "succeeded": succeeded,
                "failed": sum(item.get("status") == "failed" for item in completed),
                "success_rate": round(succeeded / len(completed), 4) if completed else None,
                "average_run_seconds": round(sum(runtimes) / len(runtimes), 3) if runtimes else None,
            },
        }
