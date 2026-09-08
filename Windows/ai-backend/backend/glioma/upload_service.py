from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
from threading import RLock
from typing import Any, BinaryIO, Dict
import uuid

from .models import MRI_MODALITIES, CaseStatus, ModalityUpload
from .case_service import CaseService, utc_now
from .nifti_validator import (
    NiftiValidationError,
    inspect_nifti,
    sanitize_nifti_header,
    validate_spatial_consistency,
)


class UploadServiceError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class InvalidModalityError(UploadServiceError):
    pass


class UploadStateError(UploadServiceError):
    pass


class MissingModalitiesError(UploadServiceError):
    pass


class UploadTooLargeError(UploadServiceError):
    pass


class InsufficientStorageError(UploadServiceError):
    pass


def sanitize_client_filename(filename: str | None) -> str:
    value = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    value = "".join(character for character in value if character >= " " and character != "\x7f")
    return value[:255]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class BratsUploadService:
    MIN_FREE_SPACE_BYTES = 256 * 1024 * 1024

    def __init__(self, case_service: CaseService, max_upload_bytes: int):
        self.case_service = case_service
        self.max_upload_bytes = max_upload_bytes
        self._lock = RLock()

    def _require_modality(self, modality: str):
        key = str(modality or "").strip().lower()
        definition = MRI_MODALITIES.get(key)
        if definition is None:
            raise InvalidModalityError(
                "INVALID_MODALITY",
                "modality 必须是 flair、t1、t1ce 或 t2",
                {"allowed_modalities": list(MRI_MODALITIES)},
            )
        return key, definition

    def _transition_to_uploading(self, case_id: str) -> None:
        current = self.case_service.read_status(case_id)
        allowed = {
            CaseStatus.CREATED,
            CaseStatus.UPLOADING,
            CaseStatus.INVALID,
            CaseStatus.UPLOADED,
            CaseStatus.FAILED,
            CaseStatus.CANCELLED,
        }
        if current.status not in allowed:
            raise UploadStateError(
                "CASE_NOT_UPLOADABLE",
                f"当前病例状态 {current.status.value} 不允许上传 MRI 序列",
                {"status": current.status.value},
            )
        if current.status != CaseStatus.UPLOADING:
            self.case_service.transition_status(
                case_id,
                CaseStatus.UPLOADING,
                "正在上传四序列 MRI",
                0,
            )

    def _write_stream(self, stream: BinaryIO, staging_path: Path) -> tuple[int, str]:
        free_bytes = shutil.disk_usage(staging_path.parent).free
        if free_bytes < self.MIN_FREE_SPACE_BYTES:
            raise InsufficientStorageError(
                "INSUFFICIENT_STORAGE",
                "病例目录所在磁盘的可用空间不足",
                {
                    "free_bytes": free_bytes,
                    "minimum_free_bytes": self.MIN_FREE_SPACE_BYTES,
                },
            )

        size_bytes = 0
        digest = hashlib.sha256()
        try:
            with staging_path.open("xb") as destination:
                while True:
                    chunk = stream.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    if size_bytes + len(chunk) > self.max_upload_bytes:
                        raise UploadTooLargeError(
                            "UPLOAD_TOO_LARGE",
                            "单个 MRI 文件超过后端允许的最大上传大小",
                            {"max_upload_bytes": self.max_upload_bytes},
                        )
                    free_bytes = shutil.disk_usage(staging_path.parent).free
                    if free_bytes < len(chunk) + self.MIN_FREE_SPACE_BYTES:
                        raise InsufficientStorageError(
                            "INSUFFICIENT_STORAGE",
                            "写入 MRI 文件时磁盘剩余空间不足",
                            {
                                "free_bytes": free_bytes,
                                "minimum_free_bytes": self.MIN_FREE_SPACE_BYTES,
                            },
                        )
                    destination.write(chunk)
                    digest.update(chunk)
                    size_bytes += len(chunk)
                destination.flush()
                os.fsync(destination.fileno())
        except OSError as exc:
            if getattr(exc, "winerror", None) == 112 or getattr(exc, "errno", None) == 28:
                raise InsufficientStorageError(
                    "INSUFFICIENT_STORAGE",
                    "写入 MRI 文件时磁盘空间不足",
                ) from exc
            raise

        if size_bytes == 0:
            raise UploadServiceError("EMPTY_FILE", "上传文件为空")
        return size_bytes, digest.hexdigest()

    def preflight(self, case_id: str, modality_sizes: Dict[str, int]) -> Dict[str, Any]:
        """Validate file sizes and conservatively estimate storage before upload."""

        paths = self.case_service.paths_for(case_id, require_exists=True)
        status = self.case_service.read_status(case_id)
        allowed = {
            CaseStatus.CREATED,
            CaseStatus.UPLOADING,
            CaseStatus.INVALID,
            CaseStatus.UPLOADED,
            CaseStatus.FAILED,
            CaseStatus.CANCELLED,
        }
        if status.status not in allowed:
            raise UploadStateError(
                "CASE_NOT_UPLOADABLE",
                f"当前病例状态 {status.status.value} 不允许上传 MRI 序列",
                {"status": status.status.value},
            )

        normalized: Dict[str, int] = {}
        for modality, raw_size in modality_sizes.items():
            key, _ = self._require_modality(modality)
            if key in normalized:
                raise UploadServiceError("DUPLICATE_MODALITY", "MRI 序列不能重复")
            if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size <= 0:
                raise UploadServiceError(
                    "INVALID_FILE_SIZE",
                    f"{key.upper()} 文件大小无效",
                    {"modality": key},
                )
            if raw_size > self.max_upload_bytes:
                raise UploadTooLargeError(
                    "UPLOAD_TOO_LARGE",
                    f"{key.upper()} 超过后端允许的单文件最大上传大小",
                    {
                        "modality": key,
                        "size_bytes": raw_size,
                        "max_upload_bytes": self.max_upload_bytes,
                    },
                )
            normalized[key] = raw_size

        manifest = self.case_service.read_upload_manifest(case_id)
        existing_bytes = sum(
            int(item.size_bytes or 0)
            for item in manifest.modalities.values()
            if item.uploaded
        )
        incoming_bytes = sum(normalized.values())
        estimated_case_bytes = existing_bytes + incoming_bytes
        largest_file_bytes = max(normalized.values(), default=0)
        safety_reserve_bytes = max(
            self.MIN_FREE_SPACE_BYTES,
            int(estimated_case_bytes * 0.10),
        )
        # Original files, the later nnU-Net copies and one sanitization temporary
        # may coexist. The estimate is intentionally conservative.
        required_bytes = (
            incoming_bytes
            + estimated_case_bytes
            + largest_file_bytes
            + safety_reserve_bytes
        )
        free_bytes = shutil.disk_usage(paths.root).free
        if free_bytes < required_bytes:
            raise InsufficientStorageError(
                "INSUFFICIENT_STORAGE",
                "磁盘空间不足，无法安全接收本次 MRI 上传",
                {
                    "free_bytes": free_bytes,
                    "required_bytes": required_bytes,
                    "safety_reserve_bytes": safety_reserve_bytes,
                },
            )

        return {
            "case_id": case_id,
            "accepted": True,
            "modalities": normalized,
            "incoming_bytes": incoming_bytes,
            "free_bytes": free_bytes,
            "required_bytes": required_bytes,
            "safety_reserve_bytes": safety_reserve_bytes,
            "max_file_bytes": self.max_upload_bytes,
        }

    def _prepare_nnunet_files(self, case_id: str, paths) -> list[Path]:
        operations = []
        try:
            for key, definition in MRI_MODALITIES.items():
                source = paths.original_input / f"{key}.nii.gz"
                target = paths.nnunet_input / f"{case_id}_{definition.nnunet_suffix}.nii.gz"
                temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                backup = target.with_name(f".{target.name}.{uuid.uuid4().hex}.backup")
                shutil.copy2(source, temporary)
                operations.append(
                    {
                        "temporary": temporary,
                        "target": target,
                        "backup": backup,
                        "had_previous": target.exists(),
                        "replaced": False,
                    }
                )

            for operation in operations:
                if operation["had_previous"]:
                    os.replace(operation["target"], operation["backup"])
                os.replace(operation["temporary"], operation["target"])
                operation["replaced"] = True

            for operation in operations:
                if operation["backup"].exists():
                    operation["backup"].unlink()
            return [operation["target"] for operation in operations]
        except Exception:
            for operation in reversed(operations):
                if operation["replaced"] and operation["target"].exists():
                    operation["target"].unlink()
                if operation["backup"].exists():
                    os.replace(operation["backup"], operation["target"])
            raise
        finally:
            for operation in operations:
                for temporary in (operation["temporary"], operation["backup"]):
                    if temporary.exists():
                        temporary.unlink()

    def save_modality(
        self,
        case_id: str,
        modality: str,
        stream: BinaryIO,
        original_filename: str | None,
    ) -> Dict[str, Any]:
        key, definition = self._require_modality(modality)
        safe_filename = sanitize_client_filename(original_filename)
        if not safe_filename.lower().endswith(".nii.gz"):
            raise UploadServiceError("INVALID_EXTENSION", "MRI 文件必须使用 .nii.gz 格式")

        with self._lock:
            paths = self.case_service.paths_for(case_id, require_exists=True)
            self._transition_to_uploading(case_id)
            staging_path = paths.staging / f".{key}.{uuid.uuid4().hex}.uploading.nii.gz"
            sanitized_path = paths.staging / f".{key}.{uuid.uuid4().hex}.sanitized.nii.gz"
            destination_path = paths.original_input / f"{key}.nii.gz"
            backup_path = paths.staging / f".{key}.{uuid.uuid4().hex}.backup"
            destination_replaced = False
            previous_exists = False

            try:
                self._write_stream(stream, staging_path)
                inspect_nifti(staging_path)
                sanitize_nifti_header(staging_path, sanitized_path)
                metadata = inspect_nifti(sanitized_path)
                size_bytes = sanitized_path.stat().st_size
                digest = sha256_file(sanitized_path)

                previous_exists = destination_path.exists()
                if previous_exists:
                    os.replace(destination_path, backup_path)
                os.replace(sanitized_path, destination_path)
                destination_replaced = True

                manifest = self.case_service.read_upload_manifest(case_id)
                now = utc_now()
                modalities = dict(manifest.modalities)
                modalities[key] = ModalityUpload(
                    modality=definition.key,
                    channel_index=definition.channel_index,
                    nnunet_suffix=definition.nnunet_suffix,
                    uploaded=True,
                    # Client filenames may contain patient identifiers. Validate the
                    # extension, but never persist the supplied name.
                    original_filename=None,
                    stored_filename=f"input/original/{key}.nii.gz",
                    size_bytes=size_bytes,
                    sha256=digest,
                    uploaded_at=now,
                    nifti=metadata,
                )
                updated_manifest = manifest.model_copy(
                    update={"complete": False, "modalities": modalities, "updated_at": now}
                )
                updated_manifest = self.case_service.save_upload_manifest(updated_manifest)

                case_info = self.case_service.read_case_info(case_id)
                self.case_service.save_case_info(case_info.model_copy(update={"updated_at": now}))

                uploaded = [name for name, item in updated_manifest.modalities.items() if item.uploaded]
                missing = [name for name in MRI_MODALITIES if name not in uploaded]
                progress = int(len(uploaded) / len(MRI_MODALITIES) * 70)
                status = self.case_service.transition_status(
                    case_id,
                    CaseStatus.UPLOADING,
                    f"已上传 {len(uploaded)}/4 个 MRI 序列",
                    progress,
                )

                if backup_path.exists():
                    backup_path.unlink()
                return {
                    "case_id": case_id,
                    "modality": key,
                    "status": status.status.value,
                    "uploaded_modalities": uploaded,
                    "missing_modalities": missing,
                    "file": {
                        "size_bytes": size_bytes,
                        "nifti": metadata.model_dump(mode="json"),
                    },
                }
            except (UploadServiceError, NiftiValidationError) as exc:
                if destination_replaced and destination_path.exists():
                    destination_path.unlink()
                if previous_exists and backup_path.exists():
                    os.replace(backup_path, destination_path)
                try:
                    self.case_service.transition_status(
                        case_id,
                        CaseStatus.INVALID,
                        getattr(exc, "message", str(exc)),
                        0,
                    )
                except Exception:
                    pass
                raise
            except Exception:
                if destination_replaced and destination_path.exists():
                    destination_path.unlink()
                if previous_exists and backup_path.exists():
                    os.replace(backup_path, destination_path)
                try:
                    self.case_service.transition_status(
                        case_id,
                        CaseStatus.FAILED,
                        "MRI 文件保存失败",
                        0,
                    )
                except Exception:
                    pass
                raise
            finally:
                for temporary_path in (staging_path, sanitized_path, backup_path):
                    if temporary_path.exists():
                        temporary_path.unlink()

    def complete_upload(self, case_id: str) -> Dict[str, Any]:
        with self._lock:
            paths = self.case_service.paths_for(case_id, require_exists=True)
            manifest = self.case_service.read_upload_manifest(case_id)
            if manifest.complete:
                status = self.case_service.read_status(case_id)
                return {
                    "case_id": case_id,
                    "status": status.status.value,
                    "ready_for_segmentation": status.status == CaseStatus.UPLOADED,
                    "uploaded_modalities": list(MRI_MODALITIES),
                    "nnunet_files": [
                        f"{case_id}_{definition.nnunet_suffix}.nii.gz"
                        for definition in MRI_MODALITIES.values()
                    ],
                }
            missing = [
                key
                for key in MRI_MODALITIES
                if not manifest.modalities.get(key) or not manifest.modalities[key].uploaded
            ]
            if missing:
                raise MissingModalitiesError(
                    "MISSING_MODALITIES",
                    "缺少必要的 MRI 序列",
                    {"missing_modalities": missing},
                )

            self.case_service.transition_status(
                case_id,
                CaseStatus.VALIDATING,
                "正在校验四个 MRI 序列的空间一致性",
                80,
            )

            try:
                metadata_by_modality = {}
                digests = {}
                for key in MRI_MODALITIES:
                    path = paths.original_input / f"{key}.nii.gz"
                    if not path.is_file():
                        raise MissingModalitiesError(
                            "MISSING_MODALITIES",
                            "上传清单与病例文件不一致",
                            {"missing_modalities": [key]},
                        )
                    metadata_by_modality[key] = inspect_nifti(path)
                    digests[key] = sha256_file(path)
                    expected_digest = manifest.modalities[key].sha256
                    if not expected_digest or digests[key] != expected_digest:
                        raise NiftiValidationError(
                            "FILE_CHANGED_AFTER_UPLOAD",
                            "MRI 文件在上传后发生变化，请重新上传该序列",
                            {"modality": key},
                        )

                validate_spatial_consistency(metadata_by_modality, digests)
                nnunet_files = self._prepare_nnunet_files(case_id, paths)

                now = utc_now()
                completed_manifest = manifest.model_copy(update={"complete": True, "updated_at": now})
                completed_manifest = self.case_service.save_upload_manifest(completed_manifest)
                case_info = self.case_service.read_case_info(case_id)
                self.case_service.save_case_info(case_info.model_copy(update={"updated_at": now}))
                status = self.case_service.transition_status(
                    case_id,
                    CaseStatus.UPLOADED,
                    "四个 MRI 序列已通过校验，可以提交脑肿瘤分割",
                    100,
                )
                return {
                    "case_id": case_id,
                    "status": status.status.value,
                    "ready_for_segmentation": True,
                    "uploaded_modalities": list(MRI_MODALITIES),
                    "nnunet_files": [path.name for path in nnunet_files],
                }
            except (MissingModalitiesError, NiftiValidationError) as exc:
                self.case_service.transition_status(
                    case_id,
                    CaseStatus.INVALID,
                    getattr(exc, "message", str(exc)),
                    0,
                )
                raise
            except Exception:
                self.case_service.transition_status(
                    case_id,
                    CaseStatus.FAILED,
                    "整理 nnU-Net 输入文件失败",
                    0,
                )
                raise
            finally:
                for temporary in paths.nnunet_input.glob(".*.tmp"):
                    temporary.unlink()

