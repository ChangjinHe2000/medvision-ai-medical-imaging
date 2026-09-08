from __future__ import annotations

import gzip
import os
from pathlib import Path
import shutil
import tempfile
from typing import Dict, Mapping

import nibabel as nib
import numpy as np

from .models import NiftiMetadata


class NiftiValidationError(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


NIFTI_TEXT_FIELDS = ("descrip", "aux_file", "intent_name", "db_name")


def load_nifti_image(path: Path) -> nib.spatialimages.SpatialImage:
    """Load NIfTI, working around nibabel's Windows extended-path limitation."""

    try:
        return nib.load(str(path))
    except FileNotFoundError:
        if not str(path).startswith("\\\\?\\"):
            raise
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".nii.gz", delete=False) as temporary:
                temporary_path = Path(temporary.name)
                with path.open("rb") as source:
                    shutil.copyfileobj(source, temporary, length=8 * 1024 * 1024)
            temporary_image = nib.load(str(temporary_path))
            data = np.asanyarray(temporary_image.dataobj)
            return nib.Nifti1Image(
                data,
                np.asarray(temporary_image.affine),
                header=temporary_image.header.copy(),
            )
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def _verify_gzip_integrity(path: Path) -> None:
    try:
        with gzip.open(path, "rb") as stream:
            while stream.read(8 * 1024 * 1024):
                pass
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise NiftiValidationError(
            "INVALID_GZIP",
            "文件不是完整有效的 .nii.gz 压缩文件",
        ) from exc


def inspect_nifti(path: Path) -> NiftiMetadata:
    if not path.is_file() or path.stat().st_size <= 0:
        raise NiftiValidationError("EMPTY_FILE", "上传文件为空")

    _verify_gzip_integrity(path)
    try:
        image = load_nifti_image(path)
        shape = tuple(int(value) for value in image.shape)
        affine = np.asarray(image.affine, dtype=np.float64)
        spacing = tuple(float(value) for value in image.header.get_zooms()[:3])
        orientation = tuple(str(value) for value in nib.aff2axcodes(affine))
        dtype = str(image.header.get_data_dtype())
    except NiftiValidationError:
        raise
    except Exception as exc:
        raise NiftiValidationError(
            "INVALID_NIFTI",
            "文件不能作为有效的 NIfTI 影像读取",
        ) from exc

    if len(shape) != 3 or any(value <= 0 for value in shape):
        raise NiftiValidationError(
            "INVALID_DIMENSIONS",
            "MRI 序列必须是尺寸有效的三维 NIfTI 影像",
            {"shape": list(shape)},
        )
    if affine.shape != (4, 4) or not np.isfinite(affine).all():
        raise NiftiValidationError("INVALID_AFFINE", "NIfTI affine 矩阵无效")
    if abs(float(np.linalg.det(affine[:3, :3]))) < 1e-8:
        raise NiftiValidationError("INVALID_AFFINE", "NIfTI affine 矩阵不可逆")
    if len(spacing) != 3 or not np.isfinite(spacing).all() or any(value <= 0 for value in spacing):
        raise NiftiValidationError(
            "INVALID_SPACING",
            "NIfTI 体素间距必须是三个正数",
        )
    if len(orientation) != 3 or any(value == "None" for value in orientation):
        raise NiftiValidationError("INVALID_ORIENTATION", "NIfTI 方向信息无效")

    return NiftiMetadata(
        shape=shape,
        spacing=spacing,
        orientation=orientation,
        affine=affine.tolist(),
        dtype=dtype,
    )


def sanitize_nifti_header(source_path: Path, destination_path: Path) -> None:
    """Write a functional NIfTI copy without free-text fields or extensions."""

    try:
        image = load_nifti_image(source_path)
        header = image.header.copy()
        for field in NIFTI_TEXT_FIELDS:
            if field in header:
                header[field] = b""
        if hasattr(header, "extensions"):
            header.extensions.clear()
        sanitized = image.__class__(image.dataobj, image.affine, header=header)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".nii.gz", delete=False) as temporary:
                temporary_path = Path(temporary.name)
            nib.save(sanitized, str(temporary_path))
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            with temporary_path.open("rb") as source, destination_path.open("xb") as destination:
                shutil.copyfileobj(source, destination, length=8 * 1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            if not destination_path.is_file() or destination_path.stat().st_size <= 0:
                raise OSError("sanitized NIfTI output is empty")
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
    except Exception as exc:
        if destination_path.exists():
            destination_path.unlink()
        raise NiftiValidationError(
            "NIFTI_PRIVACY_SANITIZATION_FAILED",
            "MRI 文件隐私字段清洗失败",
        ) from exc


def validate_spatial_consistency(
    metadata_by_modality: Mapping[str, NiftiMetadata],
    sha256_by_modality: Mapping[str, str],
) -> None:
    if not metadata_by_modality:
        raise NiftiValidationError("MISSING_MODALITIES", "没有可校验的 MRI 序列")

    duplicate_groups: Dict[str, list[str]] = {}
    for modality, digest in sha256_by_modality.items():
        duplicate_groups.setdefault(digest, []).append(modality)
    duplicates = [items for items in duplicate_groups.values() if len(items) > 1]
    if duplicates:
        raise NiftiValidationError(
            "DUPLICATE_MODALITY_CONTENT",
            "不同 MRI 序列不能上传完全相同的文件",
            {"duplicate_groups": duplicates},
        )

    reference_modality = "flair" if "flair" in metadata_by_modality else next(iter(metadata_by_modality))
    reference = metadata_by_modality[reference_modality]
    reference_affine = np.asarray(reference.affine, dtype=np.float64)
    mismatches = []

    for modality, metadata in metadata_by_modality.items():
        if modality == reference_modality:
            continue
        fields = []
        if metadata.shape != reference.shape:
            fields.append("shape")
        if not np.allclose(metadata.spacing, reference.spacing, rtol=1e-5, atol=1e-4):
            fields.append("spacing")
        if metadata.orientation != reference.orientation:
            fields.append("orientation")
        if not np.allclose(
            np.asarray(metadata.affine, dtype=np.float64),
            reference_affine,
            rtol=1e-5,
            atol=1e-3,
        ):
            fields.append("affine")
        if fields:
            mismatches.append({"modality": modality, "fields": fields})

    if mismatches:
        raise NiftiValidationError(
            "SPATIAL_MISMATCH",
            "四个 MRI 序列的空间信息不一致，请先完成配准",
            {"reference_modality": reference_modality, "mismatches": mismatches},
        )

