from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable

import nibabel as nib
import numpy as np
from scipy import ndimage
from scipy.spatial import ConvexHull, QhullError
from skimage import measure

from .models import CaseStatus
from .case_service import CaseService, atomic_write_json
from .nifti_validator import NiftiValidationError, inspect_nifti, load_nifti_image


METRICS_SCHEMA_VERSION = "1.0"
METRICS_ALGORITHM_NAME = "ppgl-brats-quantitative-metrics"
METRICS_ALGORITHM_VERSION = "1.0.0"
ALLOWED_LABELS = frozenset({0, 1, 2, 3})
REGION_LABELS = {
    "edema": (1,),
    "net": (2,),
    "et": (3,),
    "tc": (2, 3),
    "wt": (1, 2, 3),
}
REGION_NAMES = {
    "edema": "水肿 ED",
    "net": "非增强肿瘤 NET",
    "et": "增强肿瘤 ET",
    "tc": "肿瘤核心 TC",
    "wt": "全肿瘤 WT",
}


class MetricsServiceError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class MetricsNotGeneratedError(MetricsServiceError):
    pass


class SegmentationNotFoundError(MetricsServiceError):
    pass


class MetricsValidationError(MetricsServiceError):
    pass


def _round(value: float | np.floating, digits: int = 6) -> float:
    return round(float(value), digits)


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return _round(numerator / denominator)


def _apply_affine(points_ijk: np.ndarray, affine: np.ndarray) -> np.ndarray:
    if points_ijk.size == 0:
        return np.empty((0, 3), dtype=np.float64)
    return nib.affines.apply_affine(affine, points_ijk).astype(np.float64, copy=False)


def _farthest_pair(points: np.ndarray, chunk_size: int = 512) -> tuple[float, np.ndarray | None]:
    """Return the exact farthest distance without allocating a full NxN matrix."""

    count = len(points)
    if count < 2:
        return 0.0, None
    maximum_squared = -1.0
    pair = None
    for start in range(0, count, chunk_size):
        block = points[start : start + chunk_size]
        squared = np.sum((block[:, None, :] - points[None, :, :]) ** 2, axis=2)
        flat_index = int(np.argmax(squared))
        block_index, point_index = np.unravel_index(flat_index, squared.shape)
        candidate = float(squared[block_index, point_index])
        if candidate > maximum_squared:
            maximum_squared = candidate
            pair = np.vstack((block[block_index], points[point_index]))
    return math.sqrt(max(maximum_squared, 0.0)), pair


def _surface_mesh(mask: np.ndarray, affine: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if not np.any(mask):
        return np.empty((0, 3), dtype=np.float64), np.empty((0, 3), dtype=np.int32)
    padded = np.pad(mask.astype(np.uint8), 1, mode="constant")
    vertices, faces, _, _ = measure.marching_cubes(padded, level=0.5)
    vertices -= 1.0
    return _apply_affine(vertices, affine), np.asarray(faces, dtype=np.int32)


def _mesh_surface_area(vertices: np.ndarray, faces: np.ndarray) -> float:
    if len(faces) == 0:
        return 0.0
    triangles = vertices[faces]
    cross_products = np.cross(
        triangles[:, 1] - triangles[:, 0],
        triangles[:, 2] - triangles[:, 0],
    )
    return float(np.linalg.norm(cross_products, axis=1).sum() * 0.5)


def _mesh_volume(vertices: np.ndarray, faces: np.ndarray) -> float:
    if len(faces) == 0:
        return 0.0
    triangles = vertices[faces] - vertices[0]
    signed = np.einsum(
        "ij,ij->i",
        triangles[:, 0],
        np.cross(triangles[:, 1], triangles[:, 2]),
    )
    return abs(float(signed.sum() / 6.0))


def _convex_vertices(points: np.ndarray) -> np.ndarray:
    if len(points) < 4:
        return points
    try:
        hull = ConvexHull(points)
    except QhullError:
        return points
    return points[hull.vertices]


def _component_count(mask: np.ndarray) -> int:
    structure = ndimage.generate_binary_structure(rank=3, connectivity=3)
    _, count = ndimage.label(mask, structure=structure)
    return int(count)


def _axial_contour_points(mask_slice: np.ndarray, slice_index: int, affine: np.ndarray) -> np.ndarray:
    padded = np.pad(mask_slice.astype(np.uint8), 1, mode="constant")
    contours = measure.find_contours(padded, level=0.5)
    if not contours:
        return np.empty((0, 3), dtype=np.float64)
    points = []
    for contour in contours:
        ijk = np.column_stack(
            (
                contour[:, 0] - 1.0,
                contour[:, 1] - 1.0,
                np.full(len(contour), float(slice_index)),
            )
        )
        points.append(_apply_affine(ijk, affine))
    return np.vstack(points)


def _axial_measurements(mask: np.ndarray, affine: np.ndarray) -> Dict[str, Any]:
    in_plane_area = float(np.linalg.norm(np.cross(affine[:3, 0], affine[:3, 1])))
    areas = np.count_nonzero(mask, axis=(0, 1)).astype(np.float64) * in_plane_area
    if not np.any(mask):
        return {
            "maximum_area_mm2": 0.0,
            "maximum_area_slice_index": None,
            "long_diameter_mm": 0.0,
            "short_diameter_mm": 0.0,
            "diameter_slice_index": None,
        }

    maximum_area_index = int(np.argmax(areas))
    maximum_long = -1.0
    associated_short = 0.0
    diameter_slice_index = None
    plane_normal = np.cross(affine[:3, 0], affine[:3, 1])
    plane_normal /= np.linalg.norm(plane_normal)

    for slice_index in np.flatnonzero(areas > 0):
        points = _axial_contour_points(mask[:, :, slice_index], int(slice_index), affine)
        if len(points) < 2:
            continue
        points = _convex_vertices(points)
        long_diameter, pair = _farthest_pair(points)
        if pair is None or long_diameter <= maximum_long:
            continue
        long_direction = pair[1] - pair[0]
        long_direction /= np.linalg.norm(long_direction)
        short_direction = np.cross(plane_normal, long_direction)
        short_norm = np.linalg.norm(short_direction)
        if short_norm <= 1e-12:
            short_diameter = 0.0
        else:
            short_direction /= short_norm
            projections = points @ short_direction
            short_diameter = float(projections.max() - projections.min())
        maximum_long = long_diameter
        associated_short = short_diameter
        diameter_slice_index = int(slice_index)

    return {
        "maximum_area_mm2": _round(areas[maximum_area_index]),
        "maximum_area_slice_index": maximum_area_index,
        "long_diameter_mm": _round(max(maximum_long, 0.0)),
        "short_diameter_mm": _round(associated_short),
        "diameter_slice_index": diameter_slice_index,
    }


def _shape_measurements(mask: np.ndarray, affine: np.ndarray) -> Dict[str, Any]:
    vertices, faces = _surface_mesh(mask, affine)
    if len(vertices) == 0:
        return {
            "maximum_3d_diameter_mm": 0.0,
            "surface_area_mm2": 0.0,
            "sphericity": None,
            "boundary_regularity_index": None,
        }

    hull_points = _convex_vertices(vertices)
    maximum_diameter, _ = _farthest_pair(hull_points)
    surface_area = _mesh_surface_area(vertices, faces)
    mesh_volume = _mesh_volume(vertices, faces)
    sphericity = None
    if surface_area > 0 and mesh_volume > 0:
        value = math.pi ** (1.0 / 3.0) * (6.0 * mesh_volume) ** (2.0 / 3.0) / surface_area
        sphericity = _round(min(max(value, 0.0), 1.0))

    regularity = None
    if len(vertices) >= 4 and mesh_volume > 0:
        try:
            convex_volume = float(ConvexHull(vertices).volume)
        except QhullError:
            convex_volume = 0.0
        if convex_volume > 0:
            regularity = _round(min(max(mesh_volume / convex_volume, 0.0), 1.0))

    return {
        "maximum_3d_diameter_mm": _round(maximum_diameter),
        "surface_area_mm2": _round(surface_area),
        "sphericity": sphericity,
        "boundary_regularity_index": regularity,
    }


def _region_metrics(labels: np.ndarray, region_labels: Iterable[int], voxel_volume_mm3: float) -> dict:
    selected = tuple(int(value) for value in region_labels)
    mask = np.isin(labels, selected)
    voxel_count = int(np.count_nonzero(mask))
    return {
        "labels": list(selected),
        "voxel_count": voxel_count,
        "volume_mm3": _round(voxel_count * voxel_volume_mm3),
        "volume_ml": _round(voxel_count * voxel_volume_mm3 / 1000.0),
        "component_count_26_connected": _component_count(mask) if voxel_count else 0,
    }


class MetricsService:
    """Generate deterministic quantitative measurements from a BraTS segmentation."""

    def __init__(self, case_service: CaseService):
        self.case_service = case_service
        self._lock = RLock()

    def output_path(self, case_id: str) -> Path:
        paths = self.case_service.paths_for(case_id, require_exists=True)
        return paths.output / "metrics.json"

    def require_existing(self, case_id: str) -> Dict[str, Any]:
        status = self.case_service.read_status(case_id)
        manifest = self.case_service.read_upload_manifest(case_id)
        if status.status != CaseStatus.COMPLETED or not manifest.complete:
            raise MetricsNotGeneratedError(
                "METRICS_NOT_CURRENT",
                "当前病例状态没有可用的最新脑肿瘤定量指标",
                {"status": status.status.value},
            )
        path = self.output_path(case_id)
        if not path.is_file():
            raise MetricsNotGeneratedError(
                "METRICS_NOT_GENERATED",
                "该病例尚未生成脑肿瘤定量影像指标",
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MetricsServiceError(
                "METRICS_FILE_UNREADABLE",
                "脑肿瘤指标文件无法读取",
            ) from exc
        if not isinstance(payload, dict):
            raise MetricsServiceError("METRICS_FILE_INVALID", "脑肿瘤指标文件格式无效")
        return payload

    def generate(self, case_id: str) -> Dict[str, Any]:
        with self._lock:
            paths = self.case_service.paths_for(case_id, require_exists=True)
            status = self.case_service.read_status(case_id)
            manifest = self.case_service.read_upload_manifest(case_id)
            if not manifest.complete or status.status not in {
                CaseStatus.RUNNING,
                CaseStatus.COMPLETED,
                CaseStatus.FAILED,
            }:
                raise MetricsValidationError(
                    "CASE_NOT_READY_FOR_METRICS",
                    "病例必须完成四序列校验和分割后才能计算定量指标",
                    {"status": status.status.value},
                )
            segmentation_path = paths.output / "segmentation.nii.gz"
            reference_path = paths.original_input / "flair.nii.gz"
            if not segmentation_path.is_file():
                raise SegmentationNotFoundError(
                    "SEGMENTATION_NOT_FOUND",
                    "该病例尚未生成 segmentation.nii.gz",
                )
            if not reference_path.is_file():
                raise MetricsValidationError(
                    "REFERENCE_IMAGE_NOT_FOUND",
                    "病例缺少用于空间校验的 FLAIR 影像",
                )

            try:
                segmentation_metadata = inspect_nifti(segmentation_path)
                reference_metadata = inspect_nifti(reference_path)
            except NiftiValidationError as exc:
                raise MetricsValidationError(exc.code, exc.message, exc.details) from exc

            if segmentation_metadata.shape != reference_metadata.shape or not np.allclose(
                np.asarray(segmentation_metadata.affine, dtype=np.float64),
                np.asarray(reference_metadata.affine, dtype=np.float64),
                rtol=1e-5,
                atol=1e-3,
            ):
                raise MetricsValidationError(
                    "SEGMENTATION_SPATIAL_MISMATCH",
                    "分割结果与病例 FLAIR 影像的 shape 或 affine 不一致",
                    {
                        "segmentation_shape": list(segmentation_metadata.shape),
                        "reference_shape": list(reference_metadata.shape),
                    },
                )

            try:
                image = load_nifti_image(segmentation_path)
                raw_labels = np.asanyarray(image.dataobj)
            except Exception as exc:
                raise MetricsValidationError(
                    "SEGMENTATION_UNREADABLE",
                    "分割结果数据无法读取",
                ) from exc
            if not np.isfinite(raw_labels).all():
                raise MetricsValidationError(
                    "SEGMENTATION_NONFINITE",
                    "分割结果包含 NaN 或无穷值",
                )
            rounded_labels = np.rint(raw_labels)
            if not np.allclose(raw_labels, rounded_labels, rtol=0.0, atol=1e-6):
                raise MetricsValidationError(
                    "SEGMENTATION_LABELS_NOT_INTEGER",
                    "分割结果必须只包含整数标签 0、1、2、3",
                )
            unique_labels = {int(value) for value in np.unique(rounded_labels)}
            invalid_labels = sorted(unique_labels - ALLOWED_LABELS)
            if invalid_labels:
                raise MetricsValidationError(
                    "UNSUPPORTED_SEGMENTATION_LABELS",
                    "分割结果包含当前 BraTS 流程不支持的标签",
                    {"invalid_labels": invalid_labels, "allowed_labels": sorted(ALLOWED_LABELS)},
                )
            labels = rounded_labels.astype(np.uint8, copy=False)

            affine = np.asarray(image.affine, dtype=np.float64)
            voxel_volume_mm3 = abs(float(np.linalg.det(affine[:3, :3])))
            regions = {}
            for key, region_labels in REGION_LABELS.items():
                regions[key] = {
                    "display_name": REGION_NAMES[key],
                    **_region_metrics(labels, region_labels, voxel_volume_mm3),
                }

            wt_mask = np.isin(labels, REGION_LABELS["wt"])
            indices = np.argwhere(wt_mask)
            if len(indices):
                voxel_centroid = indices.mean(axis=0)
                world_centroid = _apply_affine(voxel_centroid.reshape(1, 3), affine)[0]
                centroid = {
                    "voxel_ijk": [_round(value) for value in voxel_centroid],
                    "world_xyz_mm": [_round(value) for value in world_centroid],
                    "world_coordinate_convention": "NIfTI affine world coordinates",
                }
            else:
                centroid = {
                    "voxel_ijk": None,
                    "world_xyz_mm": None,
                    "world_coordinate_convention": "NIfTI affine world coordinates",
                }

            axial = _axial_measurements(wt_mask, affine)
            shape = _shape_measurements(wt_mask, affine)
            inference = None
            inference_path = paths.output / "inference.json"
            if inference_path.is_file():
                try:
                    inference_record = json.loads(inference_path.read_text(encoding="utf-8"))
                    inference = {
                        "run_id": inference_record.get("run_id"),
                        "model": inference_record.get("model"),
                    }
                except (OSError, json.JSONDecodeError):
                    inference = {"record": "unreadable"}

            generated_at = datetime.now(timezone.utc).isoformat()
            for region in regions.values():
                region["units"] = {
                    "voxel_count": "voxel",
                    "volume_mm3": "mm3",
                    "volume_ml": "mL",
                    "component_count_26_connected": "count",
                }
                region["algorithm_version"] = METRICS_ALGORITHM_VERSION
                region["generated_at"] = generated_at
            centroid["unit"] = {"voxel_ijk": "voxel index", "world_xyz_mm": "mm"}
            measurements = {
                "lesion_component_count": {
                    "value": regions["wt"]["component_count_26_connected"],
                    "unit": "count",
                    "region": "WT",
                },
                "maximum_3d_diameter": {
                    "value": shape["maximum_3d_diameter_mm"],
                    "unit": "mm",
                    "region": "WT",
                },
                "maximum_axial_long_diameter": {
                    "value": axial["long_diameter_mm"],
                    "unit": "mm",
                    "region": "WT",
                    "slice_index": axial["diameter_slice_index"],
                },
                "maximum_axial_short_diameter": {
                    "value": axial["short_diameter_mm"],
                    "unit": "mm",
                    "region": "WT",
                    "slice_index": axial["diameter_slice_index"],
                },
                "maximum_axial_area": {
                    "value": axial["maximum_area_mm2"],
                    "unit": "mm2",
                    "region": "WT",
                    "slice_index": axial["maximum_area_slice_index"],
                },
                "centroid": centroid,
                "surface_area": {
                    "value": shape["surface_area_mm2"],
                    "unit": "mm2",
                    "region": "WT",
                },
                "sphericity": {
                    "value": shape["sphericity"],
                    "unit": "ratio",
                    "region": "WT",
                },
                "boundary_regularity": {
                    "value": shape["boundary_regularity_index"],
                    "unit": "ratio",
                    "region": "WT",
                },
                "et_to_tc_ratio": {
                    "value": _ratio(regions["et"]["volume_mm3"], regions["tc"]["volume_mm3"]),
                    "unit": "ratio",
                },
                "edema_to_wt_ratio": {
                    "value": _ratio(regions["edema"]["volume_mm3"], regions["wt"]["volume_mm3"]),
                    "unit": "ratio",
                },
            }
            for measurement in measurements.values():
                measurement["algorithm_version"] = METRICS_ALGORITHM_VERSION
                measurement["generated_at"] = generated_at

            payload = {
                "schema_version": METRICS_SCHEMA_VERSION,
                "case_id": case_id,
                "workflow": "brats_brain_tumour",
                "result_type": "AI 定量影像辅助结果",
                "generated_at": generated_at,
                "algorithm": {
                    "name": METRICS_ALGORITHM_NAME,
                    "version": METRICS_ALGORITHM_VERSION,
                    "label_definition": {"0": "background", "1": "ED", "2": "NET", "3": "ET"},
                    "component_connectivity": 26,
                    "diameter_method": "physical-space farthest points on the WT surface convex hull",
                    "axial_method": "WT contour long axis and perpendicular short axis",
                    "surface_method": "marching cubes transformed by the NIfTI affine",
                    "boundary_regularity_method": "WT surface-mesh solidity (mesh volume / convex-hull volume)",
                },
                "source": {
                    "segmentation_file": "output/segmentation.nii.gz",
                    "reference_file": "input/original/flair.nii.gz",
                    "shape": list(segmentation_metadata.shape),
                    "spacing_mm": [_round(value) for value in segmentation_metadata.spacing],
                    "orientation": list(segmentation_metadata.orientation),
                    "voxel_volume_mm3": _round(voxel_volume_mm3),
                    "inference": inference,
                },
                "regions": regions,
                "measurements": measurements,
                "limitations": [
                    "结果仅用于 AI 定量影像辅助，必须由具备资质的医生结合原始影像复核。",
                    "本结果不判断良恶性、WHO 分级、胶质瘤类型、分子状态、生存期或治疗方案。",
                    "边界规则度是算法定义的三维 mesh solidity，不等同于临床诊断结论。",
                ],
            }
            atomic_write_json(paths.output / "metrics.json", payload)
            return payload

