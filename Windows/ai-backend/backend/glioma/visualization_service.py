from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import struct
from threading import RLock
from typing import Any

import nibabel as nib
import numpy as np
from PIL import Image
from skimage import measure

from .models import CaseStatus
from .case_service import CaseService, atomic_write_json
from .nifti_validator import NiftiValidationError, inspect_nifti, load_nifti_image


VISUALIZATION_SCHEMA_VERSION = "1.0"
VISUALIZATION_ALGORITHM_VERSION = "1.0.0"
PLANES = ("axial", "coronal", "sagittal")
PLANE_AXES = {"sagittal": 0, "coronal": 1, "axial": 2}
REGIONS = {
    "edema": {"label": 1, "display_name": "水肿 ED", "rgb": (255, 209, 102)},
    "net": {"label": 2, "display_name": "非增强肿瘤 NET", "rgb": (239, 68, 68)},
    "et": {"label": 3, "display_name": "增强肿瘤 ET", "rgb": (59, 130, 246)},
}


class VisualizationServiceError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class VisualizationNotGeneratedError(VisualizationServiceError):
    pass


class VisualizationValidationError(VisualizationServiceError):
    pass


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _png_bytes(array: np.ndarray) -> bytes:
    output = BytesIO()
    Image.fromarray(array).save(output, format="PNG", optimize=True)
    return output.getvalue()


def _normalize_mri(volume: np.ndarray) -> np.ndarray:
    finite = volume[np.isfinite(volume)]
    foreground = finite[finite != 0]
    sample = foreground if foreground.size else finite
    if not sample.size:
        return np.zeros(volume.shape, dtype=np.uint8)
    lower, upper = np.percentile(sample, (1.0, 99.0))
    if upper <= lower:
        lower = float(sample.min())
        upper = float(sample.max())
    if upper <= lower:
        return np.zeros(volume.shape, dtype=np.uint8)
    scaled = np.clip((volume - lower) / (upper - lower), 0.0, 1.0)
    scaled[~np.isfinite(scaled)] = 0.0
    return np.rint(scaled * 255.0).astype(np.uint8)


def _representative_indices(labels: np.ndarray) -> dict[str, int]:
    mask = labels > 0
    indices: dict[str, int] = {}
    for plane, axis in PLANE_AXES.items():
        other_axes = tuple(value for value in range(3) if value != axis)
        counts = np.count_nonzero(mask, axis=other_axes)
        indices[plane] = int(np.argmax(counts)) if np.any(counts) else labels.shape[axis] // 2
    return indices


def _slice(volume: np.ndarray, plane: str, index: int) -> np.ndarray:
    if plane == "axial":
        selected = volume[:, :, index]
    elif plane == "coronal":
        selected = volume[:, index, :]
    elif plane == "sagittal":
        selected = volume[index, :, :]
    else:
        raise ValueError(f"Unsupported plane: {plane}")
    return np.ascontiguousarray(np.rot90(selected))


def _combined_overlay(base: np.ndarray, labels: np.ndarray, alpha: float = 0.48) -> np.ndarray:
    rgb = np.repeat(base[:, :, None], 3, axis=2).astype(np.float32)
    for definition in REGIONS.values():
        mask = labels == definition["label"]
        color = np.asarray(definition["rgb"], dtype=np.float32)
        rgb[mask] = rgb[mask] * (1.0 - alpha) + color * alpha
    return np.clip(rgb, 0, 255).astype(np.uint8)


def _region_layer(labels: np.ndarray, label: int, rgb: tuple[int, int, int]) -> np.ndarray:
    rgba = np.zeros((*labels.shape, 4), dtype=np.uint8)
    mask = labels == label
    rgba[mask, :3] = rgb
    rgba[mask, 3] = 180
    return rgba


def _pad4(payload: bytearray, value: int = 0) -> None:
    while len(payload) % 4:
        payload.append(value)


def _build_glb(labels: np.ndarray, affine: np.ndarray) -> tuple[bytes, list[dict[str, Any]]]:
    binary = bytearray()
    buffer_views: list[dict[str, Any]] = []
    accessors: list[dict[str, Any]] = []
    meshes: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    materials: list[dict[str, Any]] = []
    region_manifest: list[dict[str, Any]] = []

    def append_view(array: np.ndarray, target: int) -> int:
        _pad4(binary)
        offset = len(binary)
        payload = np.ascontiguousarray(array).tobytes()
        binary.extend(payload)
        index = len(buffer_views)
        buffer_views.append(
            {"buffer": 0, "byteOffset": offset, "byteLength": len(payload), "target": target}
        )
        return index

    for key, definition in REGIONS.items():
        mask = labels == definition["label"]
        if not np.any(mask):
            region_manifest.append(
                {
                    "key": key,
                    "display_name": definition["display_name"],
                    "label": definition["label"],
                    "color": "#%02x%02x%02x" % definition["rgb"],
                    "node_name": key.upper(),
                    "empty": True,
                }
            )
            continue

        padded = np.pad(mask.astype(np.uint8), 1, mode="constant")
        vertices, faces, _, _ = measure.marching_cubes(
            padded,
            level=0.5,
            allow_degenerate=False,
        )
        vertices -= 1.0
        world_vertices = nib.affines.apply_affine(affine, vertices).astype("<f4", copy=False)
        indices = np.asarray(faces, dtype="<u4").reshape(-1)

        position_view = append_view(world_vertices, 34962)
        position_accessor = len(accessors)
        accessors.append(
            {
                "bufferView": position_view,
                "componentType": 5126,
                "count": int(len(world_vertices)),
                "type": "VEC3",
                "min": world_vertices.min(axis=0).astype(float).tolist(),
                "max": world_vertices.max(axis=0).astype(float).tolist(),
            }
        )
        index_view = append_view(indices, 34963)
        index_accessor = len(accessors)
        accessors.append(
            {
                "bufferView": index_view,
                "componentType": 5125,
                "count": int(len(indices)),
                "type": "SCALAR",
                "min": [int(indices.min())],
                "max": [int(indices.max())],
            }
        )

        color = [value / 255.0 for value in definition["rgb"]]
        material_index = len(materials)
        materials.append(
            {
                "name": f"{key}_material",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [*color, 0.72],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.75,
                },
                "alphaMode": "BLEND",
                "doubleSided": True,
                "extensions": {"KHR_materials_unlit": {}},
            }
        )
        mesh_index = len(meshes)
        meshes.append(
            {
                "name": f"{key}_surface",
                "primitives": [
                    {
                        "attributes": {"POSITION": position_accessor},
                        "indices": index_accessor,
                        "material": material_index,
                        "mode": 4,
                    }
                ],
            }
        )
        node_name = key.upper()
        node_index = len(nodes)
        nodes.append({"name": node_name, "mesh": mesh_index})
        region_manifest.append(
            {
                "key": key,
                "display_name": definition["display_name"],
                "label": definition["label"],
                "color": "#%02x%02x%02x" % definition["rgb"],
                "node_name": node_name,
                "empty": False,
                "vertex_count": int(len(world_vertices)),
                "triangle_count": int(len(faces)),
            }
        )

    _pad4(binary)
    document: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": "PPGL-Assist BraTS visualization 1.0.0"},
        "extensionsUsed": ["KHR_materials_unlit"],
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": materials,
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
    }
    json_payload = bytearray(json.dumps(document, separators=(",", ":")).encode("utf-8"))
    _pad4(json_payload, 0x20)
    total_length = 12 + 8 + len(json_payload) + 8 + len(binary)
    glb = bytearray(struct.pack("<4sII", b"glTF", 2, total_length))
    glb.extend(struct.pack("<II", len(json_payload), 0x4E4F534A))
    glb.extend(json_payload)
    glb.extend(struct.pack("<II", len(binary), 0x004E4942))
    glb.extend(binary)
    return bytes(glb), region_manifest


class VisualizationService:
    """Generate deterministic 2D and 3D assets from a BraTS segmentation."""

    def __init__(self, case_service: CaseService):
        self.case_service = case_service
        self._lock = RLock()

    def manifest_path(self, case_id: str) -> Path:
        paths = self.case_service.paths_for(case_id, require_exists=True)
        return paths.output / "visualizations.json"

    def require_existing(self, case_id: str) -> dict[str, Any]:
        path = self.manifest_path(case_id)
        if not path.is_file():
            raise VisualizationNotGeneratedError(
                "VISUALIZATIONS_NOT_GENERATED",
                "该病例尚未生成二维和三维可视化结果",
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VisualizationServiceError(
                "VISUALIZATION_MANIFEST_UNREADABLE",
                "可视化清单无法读取",
            ) from exc
        if not isinstance(payload, dict):
            raise VisualizationServiceError(
                "VISUALIZATION_MANIFEST_INVALID",
                "可视化清单格式无效",
            )
        return payload

    def generate(self, case_id: str) -> dict[str, Any]:
        with self._lock:
            paths = self.case_service.paths_for(case_id, require_exists=True)
            status = self.case_service.read_status(case_id)
            manifest = self.case_service.read_upload_manifest(case_id)
            # The unified worker publishes segmentation.nii.gz atomically and
            # keeps the case in RUNNING/95% while derived assets are built.
            # Accept that internal post-processing state so the public task is
            # not marked completed before the visualization manifest exists.
            if status.status not in {CaseStatus.RUNNING, CaseStatus.COMPLETED} or not manifest.complete:
                raise VisualizationValidationError(
                    "CASE_NOT_READY_FOR_VISUALIZATION",
                    "病例必须完成四序列校验和脑肿瘤分割后才能生成可视化结果",
                    {"status": status.status.value},
                )

            reference_path = paths.original_input / "flair.nii.gz"
            segmentation_path = paths.output / "segmentation.nii.gz"
            if not reference_path.is_file() or not segmentation_path.is_file():
                raise VisualizationValidationError(
                    "VISUALIZATION_SOURCE_MISSING",
                    "生成可视化所需的 FLAIR 或 segmentation.nii.gz 不存在",
                )
            try:
                reference_metadata = inspect_nifti(reference_path)
                segmentation_metadata = inspect_nifti(segmentation_path)
            except NiftiValidationError as exc:
                raise VisualizationValidationError(exc.code, exc.message, exc.details) from exc
            if reference_metadata.shape != segmentation_metadata.shape or not np.allclose(
                np.asarray(reference_metadata.affine),
                np.asarray(segmentation_metadata.affine),
                rtol=1e-5,
                atol=1e-3,
            ):
                raise VisualizationValidationError(
                    "VISUALIZATION_SPATIAL_MISMATCH",
                    "FLAIR 与分割结果的 shape 或 affine 不一致",
                )

            try:
                reference_image = nib.as_closest_canonical(load_nifti_image(reference_path))
                segmentation_image = nib.as_closest_canonical(load_nifti_image(segmentation_path))
                reference = np.asarray(reference_image.dataobj, dtype=np.float32)
                raw_labels = np.asanyarray(segmentation_image.dataobj)
            except Exception as exc:
                raise VisualizationValidationError(
                    "VISUALIZATION_SOURCE_UNREADABLE",
                    "可视化源影像无法读取",
                ) from exc
            rounded = np.rint(raw_labels)
            if not np.allclose(raw_labels, rounded, rtol=0.0, atol=1e-6):
                raise VisualizationValidationError(
                    "SEGMENTATION_LABELS_NOT_INTEGER",
                    "分割结果包含非整数标签",
                )
            labels = rounded.astype(np.uint8, copy=False)
            invalid = sorted(set(np.unique(labels).astype(int)) - {0, 1, 2, 3})
            if invalid:
                raise VisualizationValidationError(
                    "UNSUPPORTED_SEGMENTATION_LABELS",
                    "分割结果包含不支持的标签",
                    {"invalid_labels": invalid},
                )

            normalized = _normalize_mri(reference)
            indices = _representative_indices(labels)
            original_items = []
            overlay_items = []
            plane_items = []
            for plane in PLANES:
                index = indices[plane]
                base_slice = _slice(normalized, plane, index)
                label_slice = _slice(labels, plane, index)
                original_filename = f"{plane}.png"
                overlay_filename = f"{plane}_overlay.png"
                _atomic_write_bytes(paths.slices / original_filename, _png_bytes(base_slice))
                _atomic_write_bytes(
                    paths.overlay / overlay_filename,
                    _png_bytes(_combined_overlay(base_slice, label_slice)),
                )
                layers = {}
                for key, definition in REGIONS.items():
                    filename = f"{plane}_{key}.png"
                    _atomic_write_bytes(
                        paths.overlay / filename,
                        _png_bytes(
                            _region_layer(label_slice, definition["label"], definition["rgb"])
                        ),
                    )
                    layers[key] = {"filename": filename, "label": definition["label"]}
                title = {"axial": "轴位", "coronal": "冠状位", "sagittal": "矢状位"}[plane]
                original_items.append(
                    {"plane": plane, "title": title, "filename": original_filename, "index": index}
                )
                overlay_items.append(
                    {"plane": plane, "title": title, "filename": overlay_filename, "index": index}
                )
                plane_items.append(
                    {
                        "plane": plane,
                        "title": title,
                        "index": index,
                        "slice_count": int(labels.shape[PLANE_AXES[plane]]),
                        "original": {"filename": original_filename},
                        "combined_overlay": {"filename": overlay_filename},
                        "layers": layers,
                    }
                )

            glb, mesh_regions = _build_glb(labels, np.asarray(segmentation_image.affine))
            _atomic_write_bytes(paths.meshes / "scene.glb", glb)
            generated_at = datetime.now(timezone.utc).isoformat()
            payload = {
                "schema_version": VISUALIZATION_SCHEMA_VERSION,
                "case_id": case_id,
                "workflow": "brats_brain_tumour",
                "generated_at": generated_at,
                "algorithm_version": VISUALIZATION_ALGORITHM_VERSION,
                "source": {
                    "reference": "input/original/flair.nii.gz",
                    "segmentation": "output/segmentation.nii.gz",
                    "orientation": "closest canonical RAS",
                },
                "colors": {
                    key: {
                        "label": value["label"],
                        "display_name": value["display_name"],
                        "hex": "#%02x%02x%02x" % value["rgb"],
                    }
                    for key, value in REGIONS.items()
                },
                "original": original_items,
                "overlay": overlay_items,
                "planes": plane_items,
                "mesh": {
                    "filename": "scene.glb",
                    "format": "glTF Binary 2.0",
                    "regions": mesh_regions,
                },
                "limitations": [
                    "切片位置按 WT 最大截面积自动选择，不能替代完整序列阅片。",
                    "三维表面来自自动分割标签，仅用于辅助显示，不代表手术边界。",
                    "所有可视化结果必须由专业医生结合原始四序列 MRI 复核。",
                ],
            }
            atomic_write_json(paths.output / "visualizations.json", payload)
            return payload

    def slice_asset_path(
        self,
        case_id: str,
        plane: str,
        index: int,
        layer: str,
    ) -> Path:
        """Return a cached, independently navigable MRI or label-layer PNG."""

        normalized_plane = str(plane or "").strip().lower()
        normalized_layer = str(layer or "").strip().lower()
        if normalized_plane not in PLANES:
            raise VisualizationValidationError(
                "INVALID_SLICE_PLANE",
                "切片方向必须是 axial、coronal 或 sagittal",
                {"plane": plane},
            )
        allowed_layers = {"original", "combined", *REGIONS.keys()}
        if normalized_layer not in allowed_layers:
            raise VisualizationValidationError(
                "INVALID_SLICE_LAYER",
                "切片图层必须是 original、combined、edema、net 或 et",
                {"layer": layer},
            )

        manifest = self.require_existing(case_id)
        paths = self.case_service.paths_for(case_id, require_exists=True)
        reference_path = paths.original_input / "flair.nii.gz"
        segmentation_path = paths.output / "segmentation.nii.gz"
        if not reference_path.is_file() or not segmentation_path.is_file():
            raise VisualizationValidationError(
                "VISUALIZATION_SOURCE_MISSING",
                "读取切片所需的 FLAIR 或 segmentation.nii.gz 不存在",
            )

        plane_manifest = next(
            (
                item
                for item in manifest.get("planes", [])
                if item.get("plane") == normalized_plane
            ),
            None,
        )
        slice_count = int((plane_manifest or {}).get("slice_count") or 0)
        if slice_count <= 0:
            try:
                reference_header = nib.as_closest_canonical(load_nifti_image(reference_path))
                slice_count = int(reference_header.shape[PLANE_AXES[normalized_plane]])
            except Exception as exc:
                raise VisualizationValidationError(
                    "VISUALIZATION_SOURCE_UNREADABLE",
                    "FLAIR 影像无法读取切片数量",
                ) from exc
        if index < 0 or index >= slice_count:
            raise VisualizationValidationError(
                "SLICE_INDEX_OUT_OF_RANGE",
                f"切片编号必须位于 0 到 {slice_count - 1} 之间",
                {"plane": normalized_plane, "index": index, "slice_count": slice_count},
            )

        # Reuse the representative assets already produced by generate().
        if plane_manifest and int(plane_manifest.get("index", -1)) == index:
            if normalized_layer == "original":
                filename = plane_manifest.get("original", {}).get("filename")
                candidate = paths.slices / str(filename or "")
            elif normalized_layer == "combined":
                filename = plane_manifest.get("combined_overlay", {}).get("filename")
                candidate = paths.overlay / str(filename or "")
            else:
                filename = plane_manifest.get("layers", {}).get(normalized_layer, {}).get("filename")
                candidate = paths.overlay / str(filename or "")
            if filename and candidate.is_file():
                return candidate

        filename = f"{normalized_plane}_{index:04d}_{normalized_layer}.png"
        target_directory = paths.slices / "pages" if normalized_layer == "original" else paths.overlay / "pages"
        target = target_directory / filename
        source_mtime = max(reference_path.stat().st_mtime_ns, segmentation_path.stat().st_mtime_ns)
        if target.is_file() and target.stat().st_mtime_ns >= source_mtime:
            return target

        with self._lock:
            if target.is_file() and target.stat().st_mtime_ns >= source_mtime:
                return target
            try:
                reference_image = nib.as_closest_canonical(load_nifti_image(reference_path))
                segmentation_image = nib.as_closest_canonical(load_nifti_image(segmentation_path))
                reference = np.asarray(reference_image.dataobj, dtype=np.float32)
                raw_labels = np.asanyarray(segmentation_image.dataobj)
            except Exception as exc:
                raise VisualizationValidationError(
                    "VISUALIZATION_SOURCE_UNREADABLE",
                    "可视化源影像无法读取",
                ) from exc
            if reference.shape != raw_labels.shape:
                raise VisualizationValidationError(
                    "VISUALIZATION_SPATIAL_MISMATCH",
                    "FLAIR 与分割结果的 shape 不一致",
                )
            rounded = np.rint(raw_labels)
            if not np.allclose(raw_labels, rounded, rtol=0.0, atol=1e-6):
                raise VisualizationValidationError(
                    "SEGMENTATION_LABELS_NOT_INTEGER",
                    "分割结果包含非整数标签",
                )
            labels = rounded.astype(np.uint8, copy=False)
            invalid = sorted(set(np.unique(labels).astype(int)) - {0, 1, 2, 3})
            if invalid:
                raise VisualizationValidationError(
                    "UNSUPPORTED_SEGMENTATION_LABELS",
                    "分割结果包含不支持的标签",
                    {"invalid_labels": invalid},
                )

            base_slice = _slice(_normalize_mri(reference), normalized_plane, index)
            label_slice = _slice(labels, normalized_plane, index)
            page_assets: dict[str, tuple[Path, np.ndarray]] = {
                "original": (
                    paths.slices / "pages" / f"{normalized_plane}_{index:04d}_original.png",
                    base_slice,
                ),
                "combined": (
                    paths.overlay / "pages" / f"{normalized_plane}_{index:04d}_combined.png",
                    _combined_overlay(base_slice, label_slice),
                ),
            }
            for key, definition in REGIONS.items():
                page_assets[key] = (
                    paths.overlay / "pages" / f"{normalized_plane}_{index:04d}_{key}.png",
                    _region_layer(label_slice, definition["label"], definition["rgb"]),
                )
            for asset_path, array in page_assets.values():
                _atomic_write_bytes(asset_path, _png_bytes(array))
            return page_assets[normalized_layer][0]

    def asset_path(self, case_id: str, filename: str) -> Path:
        safe_name = Path(str(filename or "")).name
        if safe_name != filename or not safe_name:
            raise VisualizationValidationError(
                "INVALID_VISUALIZATION_FILENAME",
                "可视化文件名无效",
            )
        manifest = self.require_existing(case_id)
        allowed = set()
        for plane in manifest.get("planes", []):
            allowed.add(plane.get("original", {}).get("filename"))
            allowed.add(plane.get("combined_overlay", {}).get("filename"))
            allowed.update(item.get("filename") for item in plane.get("layers", {}).values())
        if safe_name not in allowed:
            raise VisualizationNotGeneratedError(
                "VISUALIZATION_FILE_NOT_FOUND",
                "请求的可视化文件不在结果清单中",
            )
        paths = self.case_service.paths_for(case_id, require_exists=True)
        candidates = (paths.slices / safe_name, paths.overlay / safe_name)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise VisualizationNotGeneratedError(
            "VISUALIZATION_FILE_NOT_FOUND",
            "请求的可视化文件不存在",
        )

    def mesh_path(self, case_id: str) -> Path:
        manifest = self.require_existing(case_id)
        filename = manifest.get("mesh", {}).get("filename")
        if filename != "scene.glb":
            raise VisualizationNotGeneratedError(
                "MESH_NOT_GENERATED",
                "脑肿瘤三维模型尚未生成",
            )
        paths = self.case_service.paths_for(case_id, require_exists=True)
        path = paths.meshes / filename
        if not path.is_file():
            raise VisualizationNotGeneratedError(
                "MESH_NOT_GENERATED",
                "脑肿瘤三维模型尚未生成",
            )
        return path
