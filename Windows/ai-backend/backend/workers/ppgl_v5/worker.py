from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any

from backend.core.runtime import subprocess_runtime_env
from backend.domain.task_contract import TaskErrorCode, result_manifest
from backend.workers.process_runner import ProcessRunner, TaskExecutionContext, WorkerExecutionError
from backend.workers.validation import read_json, require_file, runtime_relative, validate_segmentation


@dataclass(frozen=True)
class ProgressPatchV5Job:
    case_id: str
    image_path: Path
    output_dir: Path
    model_dir: Path
    checkpoint: Path
    model_config: Path
    data_root: Path
    timeout_seconds: int
    device: str = "cuda:0"


class ProgressPatchV5Worker:
    def __init__(self, runner: ProcessRunner):
        self.runner = runner

    def run(self, job: ProgressPatchV5Job, context: TaskExecutionContext) -> dict[str, Any]:
        image = require_file(job.image_path, TaskErrorCode.INPUT_MISSING, "PPGL CT 输入不存在")
        checkpoint = require_file(
            job.checkpoint, TaskErrorCode.MODEL_WEIGHT_MISSING, "ProgressPatchV5 权重不存在"
        )
        model_config = require_file(
            job.model_config, TaskErrorCode.MODEL_CONFIG_MISSING, "ProgressPatchV5 配置不存在"
        )
        script = require_file(
            job.model_dir / "infer_single_case.py",
            TaskErrorCode.COMMAND_NOT_FOUND,
            "ProgressPatchV5 推理入口不存在",
        )
        try:
            config_payload = json.loads(model_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkerExecutionError(TaskErrorCode.MODEL_CONFIG_MISSING, "模型配置无法读取") from exc
        inference = config_payload.get("inference", {})
        job.output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            str(script),
            "--image",
            str(image),
            "--output-dir",
            str(job.output_dir),
            "--case-id",
            job.case_id,
            "--checkpoint",
            str(checkpoint),
            "--model-config",
            str(model_config),
            "--device",
            job.device,
            "--roi-size",
            str(int(inference.get("roi_size", 256))),
            "--overlap",
            str(float(inference.get("overlap", 0.25))),
            "--threshold",
            str(float(inference.get("threshold", 0.5))),
        ]
        process = self.runner.run(
            command,
            cwd=job.model_dir,
            env=subprocess_runtime_env(python_paths=(job.model_dir.parent,)),
            stdout_path=job.output_dir / "logs" / "stdout.log",
            stderr_path=job.output_dir / "logs" / "stderr.log",
            timeout_seconds=job.timeout_seconds,
            context=context,
            pid_path=job.output_dir / "logs" / "process.json",
        )
        tumor_mask = job.output_dir / "tumor_mask.nii.gz"
        validation = validate_segmentation(
            tumor_mask, image, allowed_labels={0, 1}, require_foreground=True
        )
        result = read_json(job.output_dir / "result.json")
        return {
            "manifest": result_manifest(
                case_id=job.case_id,
                workflow="ppgl_ct",
                segmentation=runtime_relative(tumor_mask, job.data_root),
                metrics=runtime_relative(job.output_dir / "clinical_metrics.json", job.data_root),
                visualization_manifest="",
                mesh="",
            ),
            "validation": validation,
            "model_result_status": result.get("status"),
            "resource_usage": {
                "duration_seconds": process.duration_seconds,
                "peak_working_set_mib": process.peak_working_set_mib,
                "peak_gpu_memory_mib": process.peak_gpu_memory_mib,
            },
        }
