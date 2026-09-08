from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np

from backend.domain.task_contract import TaskErrorCode
from backend.workers.process_runner import WorkerExecutionError


def require_file(path: Path, code: TaskErrorCode, message: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise WorkerExecutionError(code, message)
    return resolved


def require_directory(path: Path, code: TaskErrorCode, message: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise WorkerExecutionError(code, message)
    return resolved


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerExecutionError(
            TaskErrorCode.OUTPUT_MISSING,
            "推理结果清单不存在或无法读取",
        ) from exc
    if not isinstance(payload, dict):
        raise WorkerExecutionError(TaskErrorCode.OUTPUT_MISSING, "推理结果清单格式无效")
    return payload


def validate_segmentation(
    segmentation_path: Path,
    reference_path: Path,
    *,
    allowed_labels: set[int] | None = None,
    require_foreground: bool = True,
) -> dict[str, Any]:
    require_file(segmentation_path, TaskErrorCode.OUTPUT_MISSING, "分割结果文件不存在")
    require_file(reference_path, TaskErrorCode.INPUT_MISSING, "参考影像不存在")
    try:
        segmentation = nib.load(str(segmentation_path))
        reference = nib.load(str(reference_path))
        raw = np.asanyarray(segmentation.dataobj)
    except Exception as exc:
        raise WorkerExecutionError(TaskErrorCode.OUTPUT_MISSING, "分割结果无法读取") from exc
    if segmentation.shape[:3] != reference.shape[:3] or not np.allclose(
        segmentation.affine, reference.affine, rtol=0.0, atol=1e-5
    ):
        raise WorkerExecutionError(
            TaskErrorCode.OUTPUT_SPATIAL_MISMATCH,
            "分割结果与输入影像空间不一致",
        )
    if not np.isfinite(raw).all():
        raise WorkerExecutionError(TaskErrorCode.OUTPUT_EMPTY, "分割结果包含无效数值")
    rounded = np.rint(raw)
    if not np.allclose(raw, rounded, rtol=0.0, atol=1e-6):
        raise WorkerExecutionError(TaskErrorCode.OUTPUT_EMPTY, "分割结果包含非整数标签")
    labels = {int(value) for value in np.unique(rounded)}
    if allowed_labels is not None and not labels.issubset(allowed_labels):
        raise WorkerExecutionError(TaskErrorCode.OUTPUT_EMPTY, "分割结果包含未定义标签")
    foreground = int(np.count_nonzero(rounded))
    if require_foreground and foreground == 0:
        raise WorkerExecutionError(TaskErrorCode.OUTPUT_EMPTY, "分割结果为空")
    return {
        "shape": [int(value) for value in segmentation.shape[:3]],
        "spacing_mm": [float(value) for value in segmentation.header.get_zooms()[:3]],
        "labels": sorted(labels),
        "foreground_voxels": foreground,
    }


def _comparison_path(path: Path) -> Path:
    """Normalize ordinary and Windows extended-length path spellings."""

    raw = str(path.expanduser().resolve())
    if os.name == "nt":
        if raw.startswith("\\\\?\\UNC\\"):
            raw = "\\\\" + raw[8:]
        elif raw.startswith("\\\\?\\"):
            raw = raw[4:]
        raw = os.path.normcase(os.path.normpath(raw))
    return Path(raw)


def runtime_relative(path: Path, data_root: Path) -> str:
    try:
        return _comparison_path(path).relative_to(_comparison_path(data_root)).as_posix()
    except ValueError as exc:
        raise WorkerExecutionError(
            TaskErrorCode.INTERNAL_ERROR,
            "Worker 输出不在 PPGL_DATA_ROOT 中",
        ) from exc
