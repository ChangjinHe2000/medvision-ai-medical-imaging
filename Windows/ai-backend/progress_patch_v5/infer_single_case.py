#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
import torch
from monai.data import DataLoader, Dataset
from monai.inferers import sliding_window_inference
from monai.transforms import (
    Compose,
    CropForegroundd,
    EnsureChannelFirstd,
    EnsureTyped,
    LoadImaged,
    Orientationd,
    ScaleIntensityRanged,
    Spacingd,
    SpatialPadd,
    ToTensord,
)
from nibabel.processing import resample_from_to
from scipy import ndimage

try:
    from .model import build_model
except ImportError:
    from model import build_model


MODEL_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = MODEL_DIR.parents[1]
DEFAULT_CHECKPOINT = MODEL_DIR / "weights" / "model_best.pth"
DEFAULT_MODEL_CONFIG = MODEL_DIR / "model_config.json"
NUM_CLASSES = 11
ORGAN_NUM_CLASSES = 10
TUMOR_CHANNEL = 10
ORGAN_LABELS = {
    "aorta": 1,
    "kidney_left": 2,
    "kidney_right": 3,
    "inferior_vena_cava": 4,
    "adrenal_gland_left": 5,
    "adrenal_gland_right": 6,
    "iliopsoas_left": 7,
    "iliopsoas_right": 8,
    "vertebrae": 9,
}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def repository_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPOSITORY_ROOT))
    except ValueError:
        return str(path.resolve())


def validate_model_config(checkpoint: Path, config_path: Path) -> dict[str, Any]:
    if not checkpoint.is_file():
        raise FileNotFoundError(f"ProgressPatchV5 checkpoint not found: {checkpoint}")
    if not config_path.is_file():
        raise FileNotFoundError(f"ProgressPatchV5 model config not found: {config_path}")
    config = read_json(config_path)
    expected = {
        "num_classes": NUM_CLASSES,
        "organ_num_classes": ORGAN_NUM_CLASSES,
        "tumor_label": TUMOR_CHANNEL,
        "dual_label_mode": True,
        "center": "PUMCH_V5",
    }
    for key, expected_value in expected.items():
        if config.get(key) != expected_value:
            raise RuntimeError(
                f"ProgressPatchV5 config mismatch: {key}={config.get(key)!r}, "
                f"expected {expected_value!r}"
            )
    return config


def load_checkpoint(path: Path, device: str) -> Any:
    try:
        return torch.load(str(path), map_location=device, weights_only=True)
    except TypeError:
        return torch.load(str(path), map_location=device)


def checkpoint_state_dict(checkpoint: Any) -> dict[str, Any]:
    if not isinstance(checkpoint, dict):
        raise ValueError("Unsupported ProgressPatchV5 checkpoint format")
    for key in ("state_dict", "model_state_dict"):
        value = checkpoint.get(key)
        if isinstance(value, dict):
            return value
    return checkpoint


def load_model(checkpoint_path: Path, device: str) -> torch.nn.Module:
    model = build_model(out_channels=NUM_CLASSES).to(device)
    state = checkpoint_state_dict(load_checkpoint(checkpoint_path, device))
    model_keys = set(model.state_dict())
    missing = sorted(model_keys - set(state))
    unexpected = sorted(set(state) - model_keys)
    if missing or unexpected:
        raise RuntimeError(
            "Checkpoint is incompatible with ProgressPatchV5: "
            f"missing={missing[:10]}, unexpected={unexpected[:10]}"
        )
    model.load_state_dict(state, strict=True)
    model.eval()
    return model


def build_inference_transform(roi_size: int, foreground_margin: int) -> Compose:
    return Compose(
        [
            LoadImaged(keys=["image"]),
            EnsureChannelFirstd(keys=["image"]),
            Orientationd(keys=["image"], axcodes="RAS"),
            Spacingd(keys=["image"], pixdim=(1.0, 1.0, 1.0), mode="bilinear"),
            EnsureTyped(keys=["image"], track_meta=True),
            ScaleIntensityRanged(
                keys=["image"],
                a_min=0,
                a_max=255,
                b_min=0,
                b_max=1,
                clip=True,
            ),
            CropForegroundd(
                keys=["image"],
                source_key="image",
                margin=foreground_margin,
            ),
            SpatialPadd(
                keys=["image"],
                spatial_size=(roi_size, roi_size, roi_size),
                method="symmetric",
                mode="constant",
            ),
            ToTensord(keys=["image"]),
        ]
    )


def unwrap_meta_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and value:
        return unwrap_meta_value(value[0])
    if isinstance(value, np.ndarray):
        if value.shape == ():
            return value.item()
        if value.size:
            return unwrap_meta_value(value.reshape(-1)[0])
    return value


def first_affine(value: Any) -> np.ndarray | None:
    if value is None:
        return None
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    affine = np.asarray(value, dtype=np.float64)
    while affine.ndim > 2:
        affine = affine[0]
    return affine if affine.shape == (4, 4) else None


def original_image_path(batch: dict[str, Any]) -> Path:
    image = batch.get("image")
    if hasattr(image, "meta") and isinstance(image.meta, dict):
        value = unwrap_meta_value(image.meta.get("filename_or_obj"))
        if value:
            return Path(str(value))
    metadata = batch.get("image_meta_dict", {})
    if isinstance(metadata, dict):
        value = unwrap_meta_value(metadata.get("filename_or_obj"))
        if value:
            return Path(str(value))
    raise ValueError("Cannot recover original image path from MONAI metadata")


def current_image_affine(batch: dict[str, Any]) -> np.ndarray:
    image = batch.get("image")
    if hasattr(image, "meta") and isinstance(image.meta, dict):
        affine = first_affine(image.meta.get("affine"))
        if affine is not None:
            return affine
    metadata = batch.get("image_meta_dict", {})
    if isinstance(metadata, dict):
        affine = first_affine(metadata.get("affine"))
        if affine is not None:
            return affine
    raise ValueError("Cannot recover transformed image affine from MONAI metadata")


def tensor_to_3d(value: torch.Tensor | np.ndarray) -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach().cpu()
    array = value.numpy() if hasattr(value, "numpy") else np.asarray(value)
    while array.ndim > 3 and array.shape[0] == 1:
        array = array[0]
    if array.ndim != 3:
        raise ValueError(f"Expected a 3D array, got {tuple(array.shape)}")
    return np.asarray(array, dtype=np.float32)


def save_resampled(
    batch: dict[str, Any],
    value: torch.Tensor | np.ndarray,
    output_path: Path,
    interpolation_order: int,
    dtype: np.dtype,
) -> None:
    reference = nib.load(str(original_image_path(batch)))
    transformed = nib.Nifti1Image(tensor_to_3d(value), current_image_affine(batch))
    restored = resample_from_to(
        transformed,
        (reference.shape[:3], reference.affine),
        order=interpolation_order,
    )
    restored_array = np.asarray(restored.dataobj, dtype=np.float32)
    if np.issubdtype(dtype, np.integer):
        restored_array = np.rint(restored_array)
    else:
        restored_array = np.clip(restored_array, 0.0, 1.0)
    header = reference.header.copy()
    header.set_data_dtype(dtype)
    nib.save(
        nib.Nifti1Image(
            restored_array.astype(dtype, copy=False),
            reference.affine,
            header,
        ),
        str(output_path),
    )


def save_mask(path: Path, mask: np.ndarray, reference: nib.Nifti1Image) -> None:
    header = reference.header.copy()
    header.set_data_dtype(np.uint8)
    nib.save(
        nib.Nifti1Image(np.asarray(mask, dtype=np.uint8), reference.affine, header),
        str(path),
    )


def connected_component_metrics(
    tumor: np.ndarray,
    affine: np.ndarray,
    spacing: tuple[float, float, float],
) -> list[dict[str, Any]]:
    labels, count = ndimage.label(
        tumor,
        structure=np.ones((3, 3, 3), dtype=np.uint8),
    )
    voxel_volume_mm3 = float(np.prod(spacing))
    rows = []
    for component_id in range(1, int(count) + 1):
        coordinates = np.argwhere(labels == component_id)
        if coordinates.size == 0:
            continue
        minimum = coordinates.min(axis=0)
        maximum = coordinates.max(axis=0)
        bbox_size_mm = (maximum - minimum + 1) * np.asarray(spacing)
        centroid_world = nib.affines.apply_affine(affine, coordinates.mean(axis=0))
        voxel_count = int(coordinates.shape[0])
        rows.append(
            {
                "component_id": component_id,
                "voxel_count": voxel_count,
                "volume_ml": round(voxel_count * voxel_volume_mm3 / 1000.0, 3),
                "bbox_size_mm": [round(float(value), 2) for value in bbox_size_mm],
                "max_diameter_mm": round(float(np.max(bbox_size_mm)), 2),
                "centroid_mm": [round(float(value), 2) for value in centroid_world],
            }
        )
    rows.sort(key=lambda item: item["voxel_count"], reverse=True)
    return rows


def organ_relation_metrics(
    tumor: np.ndarray,
    anatomy: np.ndarray,
    spacing: tuple[float, float, float],
) -> dict[str, dict[str, Any]]:
    if not tumor.any():
        return {}
    relations = {}
    for name, label_id in ORGAN_LABELS.items():
        organ = anatomy == label_id
        if not organ.any():
            continue
        overlap_voxels = int(np.count_nonzero(tumor & organ))
        distance_map = ndimage.distance_transform_edt(~organ, sampling=spacing)
        minimum_distance = float(distance_map[tumor].min())
        relations[name] = {
            "name": name,
            "label_id": label_id,
            "min_surface_distance_mm": round(minimum_distance, 3),
            "overlap_voxels": overlap_voxels,
            "contact_or_overlap": overlap_voxels > 0 or minimum_distance <= 1.0,
        }
    return relations


def infer(args: argparse.Namespace) -> dict[str, Any]:
    image_path = Path(args.image).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    checkpoint_path = Path(args.checkpoint).expanduser().resolve()
    config_path = Path(args.model_config).expanduser().resolve()
    if not image_path.is_file():
        raise FileNotFoundError(f"Image not found: {image_path}")
    model_config = validate_model_config(checkpoint_path, config_path)
    if args.device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is not available")

    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    model = load_model(checkpoint_path, args.device)
    dataset = Dataset(
        data=[{"image": str(image_path)}],
        transform=build_inference_transform(args.roi_size, args.foreground_margin),
    )
    batch = next(iter(DataLoader(dataset, batch_size=1, num_workers=0)))
    images = batch["image"].to(args.device)
    with torch.inference_mode():
        logits = sliding_window_inference(
            inputs=images,
            roi_size=(args.roi_size, args.roi_size, args.roi_size),
            sw_batch_size=args.sw_batch_size,
            predictor=model,
            overlap=args.overlap,
            mode="gaussian",
            sw_device=args.device,
        )
    if logits.ndim != 5 or int(logits.shape[1]) != NUM_CLASSES:
        raise RuntimeError(
            f"Expected logits [B,{NUM_CLASSES},D,H,W], got {tuple(logits.shape)}"
        )

    anatomy_path = output_dir / "anatomy_mask.nii.gz"
    probability_path = output_dir / "tumor_probability.nii.gz"
    tumor_mask_path = output_dir / "tumor_mask.nii.gz"
    anatomy_prediction = torch.argmax(
        logits[:, :ORGAN_NUM_CLASSES],
        dim=1,
    ).to(torch.uint8)
    tumor_probability = torch.sigmoid(logits[:, TUMOR_CHANNEL])
    save_resampled(batch, anatomy_prediction[0], anatomy_path, 0, np.uint8)
    save_resampled(batch, tumor_probability[0], probability_path, 1, np.float32)

    probability_image = nib.load(str(probability_path))
    probability = np.asarray(probability_image.dataobj, dtype=np.float32)
    tumor = probability >= args.threshold
    save_mask(tumor_mask_path, tumor, probability_image)
    anatomy = np.rint(
        np.asarray(nib.load(str(anatomy_path)).dataobj)
    ).astype(np.uint8, copy=False)
    spacing = tuple(float(value) for value in probability_image.header.get_zooms()[:3])
    voxel_volume_mm3 = float(np.prod(spacing))
    components = connected_component_metrics(tumor, probability_image.affine, spacing)
    relations = organ_relation_metrics(tumor, anatomy, spacing)
    left_distance = (relations.get("kidney_left") or {}).get("min_surface_distance_mm")
    right_distance = (relations.get("kidney_right") or {}).get("min_surface_distance_mm")
    side = None
    if left_distance is not None or right_distance is not None:
        side = "left" if right_distance is None or (
            left_distance is not None and left_distance <= right_distance
        ) else "right"

    tumor_voxels = int(np.count_nonzero(tumor))
    metrics = {
        "tumor_voxels": tumor_voxels,
        "tumor_volume_mm3": round(tumor_voxels * voxel_volume_mm3, 3),
        "tumor_volume_ml": round(tumor_voxels * voxel_volume_mm3 / 1000.0, 3),
        "tumor_component_count": len(components),
        "tumor_side_by_nearest_kidney": side,
        "components": components,
        "max_diameter_mm": components[0]["max_diameter_mm"] if components else None,
        "anchor_distances_mm": {
            name: values["min_surface_distance_mm"]
            for name, values in relations.items()
        },
        "organ_relations": relations,
    }
    metrics_path = output_dir / "clinical_metrics.json"
    write_json(metrics_path, metrics)
    result = {
        "case_id": args.case_id or image_path.name.removesuffix(".nii.gz"),
        "status": "completed",
        "pipeline": "ProgressPatchV5 PPGL segmentation",
        "model": {
            "name": model_config["model_name"],
            "checkpoint": repository_relative(checkpoint_path),
            "model_config": repository_relative(config_path),
            "num_classes": model_config["num_classes"],
            "tumor_channel": model_config["tumor_label"],
        },
        "segmentation": {
            "threshold": args.threshold,
            "tumor_detected": tumor_voxels > 0,
            "tumor_voxels": tumor_voxels,
        },
        "metrics": metrics,
        "outputs": {
            "tumor_mask_path": str(tumor_mask_path),
            "tumor_probability_path": str(probability_path),
            "anatomy_mask_path": str(anatomy_path),
            "clinical_metrics_path": str(metrics_path),
        },
        "timing_seconds": round(time.perf_counter() - started, 3),
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }
    write_json(output_dir / "result.json", result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ProgressPatchV5 single-case PPGL inference"
    )
    parser.add_argument("--image", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--case-id", default="")
    parser.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT))
    parser.add_argument("--model-config", default=str(DEFAULT_MODEL_CONFIG))
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--roi-size", type=int, default=256)
    parser.add_argument("--sw-batch-size", type=int, default=1)
    parser.add_argument("--overlap", type=float, default=0.25)
    parser.add_argument("--foreground-margin", type=int, default=10)
    parser.add_argument("--threshold", type=float, default=0.5)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.roi_size < 1 or args.sw_batch_size < 1:
        raise ValueError("roi-size and sw-batch-size must be positive")
    if not 0.0 <= args.overlap < 1.0:
        raise ValueError("overlap must lie in [0, 1)")
    if not 0.0 < args.threshold < 1.0:
        raise ValueError("threshold must lie in (0, 1)")
    print(json.dumps(infer(args), ensure_ascii=False))


if __name__ == "__main__":
    main()
