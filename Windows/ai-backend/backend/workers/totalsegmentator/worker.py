from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Any

from backend.core.runtime import subprocess_runtime_env
from backend.domain.task_contract import TaskErrorCode, result_manifest
from backend.workers.process_runner import ProcessRunner, TaskExecutionContext
from backend.workers.validation import (
    read_json,
    require_directory,
    require_file,
    runtime_relative,
    validate_segmentation,
)


@dataclass(frozen=True)
class TotalSegmentatorJob:
    case_id: str
    image_path: Path
    output_dir: Path
    ai_backend_dir: Path
    data_root: Path
    weights_path: Path
    home_dir: Path
    timeout_seconds: int
    mode: str = "full_total"
    device: str = "cuda"
    fastest: bool = False
    fast: bool = False
    node_bin: Path | None = None
    gltfpack_bin: Path | None = None
    existing_masks_dir: Path | None = None
    force: bool = False


class TotalSegmentatorWorker:
    def __init__(self, runner: ProcessRunner):
        self.runner = runner

    def run(self, job: TotalSegmentatorJob, context: TaskExecutionContext) -> dict[str, Any]:
        image = require_file(job.image_path, TaskErrorCode.INPUT_MISSING, "CT 输入不存在")
        if job.existing_masks_dir is None:
            require_directory(
                job.weights_path,
                TaskErrorCode.MODEL_WEIGHT_MISSING,
                "TotalSegmentator 离线权重目录不存在",
            )
        else:
            require_directory(
                job.existing_masks_dir,
                TaskErrorCode.INPUT_MISSING,
                "已有 TotalSegmentator 掩膜目录不存在",
            )
        job.output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "backend.ct.run_totalseg",
            "--input",
            str(image),
            "--output",
            str(job.output_dir),
            "--case-id",
            job.case_id,
            "--mode",
            job.mode,
            "--device",
            job.device,
        ]
        if job.fastest:
            command.append("--totalseg-fastest")
        elif job.fast:
            command.append("--totalseg-fast")
        if job.existing_masks_dir is not None:
            command.extend(["--totalseg-existing-dir", str(job.existing_masks_dir.resolve())])
        if job.force:
            command.append("--force")
        env = subprocess_runtime_env(python_paths=(job.ai_backend_dir,))
        env.update(
            {
                "PPGL_DATA_ROOT": str(job.data_root),
                "TOTALSEG_WEIGHTS_PATH": str(job.weights_path),
                "TOTALSEG_HOME_DIR": str(job.home_dir),
                "PYTHONUTF8": "0" if os.name == "nt" else "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        if job.node_bin is not None:
            env["PPGL_NODE_BIN"] = str(job.node_bin)
        if job.gltfpack_bin is not None:
            env["PPGL_GLTFPACK_BIN"] = str(job.gltfpack_bin)
        process = self.runner.run(
            command,
            cwd=job.ai_backend_dir,
            env=env,
            stdout_path=job.output_dir / "logs" / "stdout.log",
            stderr_path=job.output_dir / "logs" / "stderr.log",
            timeout_seconds=job.timeout_seconds,
            context=context,
            pid_path=job.output_dir / "logs" / "process.json",
        )
        result = read_json(job.output_dir / "result.json")
        mask = job.output_dir / "mask.nii.gz"
        validation = validate_segmentation(mask, image, require_foreground=True)
        outputs = result.get("outputs", {})
        return {
            "manifest": result_manifest(
                case_id=job.case_id,
                workflow="ppgl_ct",
                segmentation=runtime_relative(mask, job.data_root),
                metrics=runtime_relative(
                    Path(outputs.get("clinical_metrics_path", job.output_dir / "metrics.json")),
                    job.data_root,
                ),
                visualization_manifest=runtime_relative(
                    Path(outputs.get("slice_gallery_path", job.output_dir / "slices" / "slice_gallery.json")),
                    job.data_root,
                ),
                mesh=runtime_relative(
                    Path(outputs.get("mesh_glb_path", job.output_dir / "meshes" / "scene.glb")),
                    job.data_root,
                ),
            ),
            "validation": validation,
            "resource_usage": {
                "duration_seconds": process.duration_seconds,
                "peak_working_set_mib": process.peak_working_set_mib,
                "peak_gpu_memory_mib": process.peak_gpu_memory_mib,
            },
        }
