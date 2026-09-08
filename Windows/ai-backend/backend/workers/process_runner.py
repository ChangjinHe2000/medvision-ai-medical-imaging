from __future__ import annotations

from dataclasses import dataclass, field
import ctypes
import json
from pathlib import Path
import os
import re
import signal
import subprocess
from threading import Event, Lock, Thread
import time
from typing import Any, Iterable, Sequence

from backend.domain.task_contract import TaskErrorCode
from backend.enterprise.privacy import redact_text as redact_private_text


class WorkerExecutionError(RuntimeError):
    def __init__(
        self,
        code: TaskErrorCode | str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code.value if isinstance(code, TaskErrorCode) else str(code)
        self.message = message
        self.retryable = retryable
        self.details = details or {}


def terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    """Terminate only the subprocess tree owned by the current task."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


@dataclass
class TaskExecutionContext:
    task_id: str
    cancel_event: Event = field(default_factory=Event)
    _process: subprocess.Popen[Any] | None = None
    _lock: Lock = field(default_factory=Lock)

    def attach_process(self, process: subprocess.Popen[Any] | None) -> None:
        with self._lock:
            self._process = process

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._lock:
            process = self._process
        if process is not None:
            terminate_process_tree(process)


def _secret_values(env: dict[str, str]) -> list[str]:
    markers = ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "PRIVATE_KEY")
    return [
        value
        for key, value in env.items()
        if value and any(marker in key.upper() for marker in markers)
    ]


def redact_text(
    value: Any,
    *,
    roots: Iterable[Path] = (),
    secrets: Iterable[str] = (),
) -> str:
    text = str(value or "")
    replacements = [str(path.resolve()) for path in roots if str(path)]
    replacements.extend(str(secret) for secret in secrets if str(secret))
    for item in sorted(set(replacements), key=len, reverse=True):
        text = text.replace(item, "<redacted>")
        text = text.replace(item.replace("\\", "/"), "<redacted>")
    text = re.sub(r"(?i)bearer\s+[a-z0-9._~+/=-]+", "Bearer <redacted>", text)
    return redact_private_text(text)


def _working_set_mib(process: subprocess.Popen[Any]) -> float | None:
    if os.name != "nt" or not hasattr(process, "_handle"):
        return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    try:
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            int(process._handle), ctypes.byref(counters), counters.cb
        )
    except (AttributeError, OSError, ValueError):
        return None
    if not ok:
        return None
    return round(float(counters.PeakWorkingSetSize) / 1048576.0, 3)


def _gpu_memory_mib(pid: int) -> float | None:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            shell=False,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",", 1)]
        if len(parts) != 2 or not parts[0].isdigit() or int(parts[0]) != pid:
            continue
        try:
            return float(parts[1])
        except ValueError:
            return None
    return None


def classify_failure(text: str) -> tuple[TaskErrorCode, str, bool]:
    lowered = text.lower()
    if "cuda out of memory" in lowered or "cuda error: out of memory" in lowered:
        return TaskErrorCode.CUDA_OUT_OF_MEMORY, "GPU 显存不足", True
    if "no such file" in lowered or "not recognized as an internal or external command" in lowered:
        return TaskErrorCode.COMMAND_NOT_FOUND, "推理命令或依赖不存在", False
    if "affine" in lowered and ("mismatch" in lowered or "inconsistent" in lowered):
        return TaskErrorCode.OUTPUT_SPATIAL_MISMATCH, "推理输出空间信息不一致", False
    return TaskErrorCode.PROCESS_FAILED, "模型推理进程执行失败", True


@dataclass(frozen=True)
class ProcessResult:
    return_code: int
    duration_seconds: float
    peak_working_set_mib: float | None
    peak_gpu_memory_mib: float | None


class ProcessRunner:
    def __init__(self, *, redaction_roots: Iterable[Path] = ()):
        self.redaction_roots = tuple(Path(path).resolve() for path in redaction_roots)

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdout_path: Path,
        stderr_path: Path,
        timeout_seconds: int,
        context: TaskExecutionContext,
        pid_path: Path | None = None,
    ) -> ProcessResult:
        if not command or not str(command[0]).strip():
            raise WorkerExecutionError(
                TaskErrorCode.COMMAND_NOT_FOUND,
                "推理命令为空",
            )
        if context.cancel_event.is_set():
            raise WorkerExecutionError(
                TaskErrorCode.PROCESS_CANCELLED,
                "任务已取消",
            )
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        secrets = _secret_values(env)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                [str(item) for item in command],
                cwd=str(cwd),
                env=dict(env),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
        except FileNotFoundError as exc:
            raise WorkerExecutionError(
                TaskErrorCode.COMMAND_NOT_FOUND,
                "推理命令不存在",
            ) from exc

        context.attach_process(process)
        if pid_path is not None:
            pid_path.parent.mkdir(parents=True, exist_ok=True)
            pid_path.write_text(
                json.dumps(
                    {
                        "task_id": context.task_id,
                        "pid": process.pid,
                        "status": "running",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        assert process.stdout is not None
        assert process.stderr is not None

        def copy_stream(source: Any, destination: Path) -> None:
            with destination.open("w", encoding="utf-8", newline="\n") as output:
                for line in iter(source.readline, ""):
                    output.write(
                        redact_text(
                            line.rstrip("\r\n"),
                            roots=self.redaction_roots,
                            secrets=secrets,
                        )
                        + "\n"
                    )
            source.close()

        stdout_thread = Thread(target=copy_stream, args=(process.stdout, stdout_path), daemon=True)
        stderr_thread = Thread(target=copy_stream, args=(process.stderr, stderr_path), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        started = time.monotonic()
        deadline = started + timeout_seconds
        peak_working_set = 0.0
        peak_gpu = 0.0
        last_gpu_sample = 0.0
        timed_out = False
        try:
            while process.poll() is None:
                if context.cancel_event.wait(0.2):
                    terminate_process_tree(process)
                    break
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    terminate_process_tree(process)
                    break
                memory = _working_set_mib(process)
                if memory is not None:
                    peak_working_set = max(peak_working_set, memory)
                if now - last_gpu_sample >= 2.0:
                    gpu_memory = _gpu_memory_mib(process.pid)
                    if gpu_memory is not None:
                        peak_gpu = max(peak_gpu, gpu_memory)
                    last_gpu_sample = now
            return_code = process.wait(timeout=15)
        finally:
            context.attach_process(None)
            stdout_thread.join(timeout=15)
            stderr_thread.join(timeout=15)
            if pid_path is not None:
                pid_path.write_text(
                    json.dumps(
                        {
                            "task_id": context.task_id,
                            "pid": process.pid,
                            "status": "stopped",
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )

        duration = round(time.monotonic() - started, 3)
        if context.cancel_event.is_set():
            raise WorkerExecutionError(
                TaskErrorCode.PROCESS_CANCELLED,
                "任务已取消",
            )
        if timed_out:
            raise WorkerExecutionError(
                TaskErrorCode.PROCESS_TIMEOUT,
                "推理任务超过最长运行时间",
                retryable=True,
                details={"timeout_seconds": timeout_seconds},
            )
        if return_code != 0:
            stdout_tail = stdout_path.read_text(encoding="utf-8", errors="ignore")[-8000:]
            stderr_tail = stderr_path.read_text(encoding="utf-8", errors="ignore")[-8000:]
            code, message, retryable = classify_failure(stdout_tail + "\n" + stderr_tail)
            raise WorkerExecutionError(
                code,
                message,
                retryable=retryable,
                details={"return_code": return_code},
            )
        return ProcessResult(
            return_code=return_code,
            duration_seconds=duration,
            peak_working_set_mib=peak_working_set or None,
            peak_gpu_memory_mib=peak_gpu or None,
        )
