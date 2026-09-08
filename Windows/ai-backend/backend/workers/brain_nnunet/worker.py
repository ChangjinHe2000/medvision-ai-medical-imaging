from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Sequence

from backend.core.runtime import find_console_script, subprocess_runtime_env
from backend.domain.task_contract import TaskErrorCode, result_manifest
from backend.workers.process_runner import ProcessRunner, TaskExecutionContext, WorkerExecutionError
from backend.workers.validation import require_directory, runtime_relative, validate_segmentation


@dataclass(frozen=True)
class BrainNnunetJob:
    case_id: str
    input_dir: Path
    flair_path: Path
    output_dir: Path
    model_dir: Path
    data_root: Path
    timeout_seconds: int
    fold: int = 0
    checkpoint: str = "checkpoint_best.pth"
    device: str = "cuda"
    step_size: float = 0.5
    preprocess_processes: int = 1
    export_processes: int = 1
    disable_tta: bool = False
    predict_command: Sequence[str] | None = None


class BrainNnunetWorker:
    def __init__(self, runner: ProcessRunner):
        self.runner = runner

    def run(self, job: BrainNnunetJob, context: TaskExecutionContext) -> dict[str, Any]:
        require_directory(job.input_dir, TaskErrorCode.INPUT_MISSING, "四序列输入目录不存在")
        require_directory(job.model_dir, TaskErrorCode.MODEL_WEIGHT_MISSING, "nnU-Net 模型目录不存在")
        checkpoint = job.model_dir / f"fold_{job.fold}" / job.checkpoint
        if not checkpoint.is_file():
            raise WorkerExecutionError(TaskErrorCode.MODEL_WEIGHT_MISSING, "nnU-Net 权重不存在")
        missing = [
            suffix
            for suffix in ("0000", "0001", "0002", "0003")
            if not (job.input_dir / f"{job.case_id}_{suffix}.nii.gz").is_file()
        ]
        if missing:
            raise WorkerExecutionError(
                TaskErrorCode.INPUT_MISSING,
                "脑 MRI 四序列输入不完整",
                details={"missing_channels": missing},
            )
        prefix = list(job.predict_command) if job.predict_command else [
            find_console_script("nnUNetv2_predict_from_modelfolder")
        ]
        job.output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            *prefix,
            "-i",
            str(job.input_dir),
            "-o",
            str(job.output_dir),
            "-m",
            str(job.model_dir),
            "-f",
            str(job.fold),
            "-chk",
            job.checkpoint,
            "-device",
            job.device,
            "-step_size",
            str(job.step_size),
            "-npp",
            str(job.preprocess_processes),
            "-nps",
            str(job.export_processes),
            "--disable_progress_bar",
        ]
        if job.disable_tta:
            command.append("--disable_tta")
        env = subprocess_runtime_env()
        env.update(
            {
                "PYTHONUTF8": "0" if os.name == "nt" else "1",
                "PYTHONIOENCODING": "utf-8",
                "nnUNet_compile": "F",
            }
        )
        process = self.runner.run(
            command,
            cwd=job.output_dir,
            env=env,
            stdout_path=job.output_dir / "logs" / "stdout.log",
            stderr_path=job.output_dir / "logs" / "stderr.log",
            timeout_seconds=job.timeout_seconds,
            context=context,
            pid_path=job.output_dir / "logs" / "process.json",
        )
        segmentation = job.output_dir / f"{job.case_id}.nii.gz"
        validation = validate_segmentation(
            segmentation,
            job.flair_path,
            allowed_labels={0, 1, 2, 3},
            require_foreground=True,
        )
        return {
            "manifest": result_manifest(
                case_id=job.case_id,
                workflow="brats_brain_tumour",
                segmentation=runtime_relative(segmentation, job.data_root),
                metrics="",
                visualization_manifest="",
                mesh="",
            ),
            "validation": validation,
            "resource_usage": {
                "duration_seconds": process.duration_seconds,
                "peak_working_set_mib": process.peak_working_set_mib,
                "peak_gpu_memory_mib": process.peak_gpu_memory_mib,
            },
        }
