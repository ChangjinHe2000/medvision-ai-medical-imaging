from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from threading import RLock
from typing import Any, Dict
import uuid

from .models import (
    ALLOWED_CASE_STATUS_TRANSITIONS,
    MRI_MODALITIES,
    CaseInfo,
    CaseStatus,
    CaseStatusRecord,
    ModalityUpload,
    StatusHistoryEntry,
    UploadManifest,
)


CASE_ID_PATTERN = re.compile(r"^case_\d{8}T\d{6}Z_[0-9a-f]{8}$")


def filesystem_root(path: Path) -> Path:
    """Resolve a case root and enable Windows extended-length paths when needed."""

    resolved = path.expanduser().resolve()
    raw = str(resolved)
    if os.name != "nt" or raw.startswith("\\\\?\\"):
        return resolved
    if raw.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + raw[2:])
    return Path("\\\\?\\" + raw)


class CaseServiceError(RuntimeError):
    """Base exception for case storage operations."""


class InvalidCaseIdError(CaseServiceError):
    """Raised when a case identifier is malformed or unsafe."""


class CaseAlreadyExistsError(CaseServiceError):
    """Raised when a generated case identifier collides with an existing case."""


class CaseNotFoundError(CaseServiceError):
    """Raised when a requested case directory does not exist."""


class InvalidStatusTransitionError(CaseServiceError):
    """Raised when a case status transition violates the state machine."""


class InvalidCaseDisplayNameError(CaseServiceError):
    """Raised when a case display name is empty or unsafe."""


class CaseDeletionConflictError(CaseServiceError):
    """Raised when a case cannot be deleted in its current state."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_case_id(case_id: str) -> str:
    value = str(case_id or "").strip()
    if not CASE_ID_PATTERN.fullmatch(value):
        raise InvalidCaseIdError("Invalid server-generated case_id")
    return value


def normalize_case_display_name(display_name: str) -> str:
    value = " ".join(str(display_name or "").split())
    if not value:
        raise InvalidCaseDisplayNameError("病例显示名称不能为空")
    if len(value) > 80:
        raise InvalidCaseDisplayNameError("病例显示名称不能超过 80 个字符")
    if any(character in value for character in ("/", "\\")):
        raise InvalidCaseDisplayNameError("病例显示名称不能包含路径分隔符")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise InvalidCaseDisplayNameError("病例显示名称不能包含控制字符")
    return value


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise CaseNotFoundError(f"Case metadata not found: {path.name}") from exc
    if not isinstance(payload, dict):
        raise CaseServiceError(f"Expected a JSON object in {path.name}")
    return payload


@dataclass(frozen=True)
class CasePaths:
    root: Path
    case_dir: Path
    case_info: Path
    upload_manifest: Path
    status: Path
    staging: Path
    input_dir: Path
    original_input: Path
    nnunet_input: Path
    output: Path
    overlay: Path
    slices: Path
    meshes: Path

    @classmethod
    def build(cls, cases_root: Path, case_id: str) -> "CasePaths":
        safe_case_id = validate_case_id(case_id)
        root = filesystem_root(cases_root)
        case_dir = (root / safe_case_id).resolve()
        if case_dir.parent != root:
            raise InvalidCaseIdError("case_id escapes the configured cases directory")
        output = case_dir / "output"
        return cls(
            root=root,
            case_dir=case_dir,
            case_info=case_dir / "case_info.json",
            upload_manifest=case_dir / "upload_manifest.json",
            status=case_dir / "status.json",
            staging=case_dir / "staging",
            input_dir=case_dir / "input",
            original_input=case_dir / "input" / "original",
            nnunet_input=case_dir / "input" / "nnunet",
            output=output,
            overlay=output / "overlay",
            slices=output / "slices",
            meshes=output / "meshes",
        )

    def directories(self) -> tuple[Path, ...]:
        return (
            self.staging,
            self.original_input,
            self.nnunet_input,
            self.output,
            self.overlay,
            self.slices,
            self.meshes,
        )


class CaseService:
    def __init__(self, cases_root: Path, trash_root: Path | None = None):
        self.cases_root = filesystem_root(cases_root)
        self.trash_root = filesystem_root(trash_root or (self.cases_root / ".trash"))
        if self.trash_root == self.cases_root:
            raise OSError("病例回收站不能与活动病例目录相同")
        if os.name == "nt":
            cases_drive = os.path.splitdrive(str(self.cases_root))[0].casefold()
            trash_drive = os.path.splitdrive(str(self.trash_root))[0].casefold()
            if cases_drive != trash_drive:
                raise OSError("病例目录和回收站必须位于同一 Windows 磁盘分区")
        self.cases_root.mkdir(parents=True, exist_ok=True)
        self.trash_root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def generate_case_id(self) -> str:
        timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        return f"case_{timestamp}_{uuid.uuid4().hex[:8]}"

    def paths_for(self, case_id: str, require_exists: bool = False) -> CasePaths:
        paths = CasePaths.build(self.cases_root, case_id)
        if require_exists and not paths.case_dir.is_dir():
            raise CaseNotFoundError(f"Case not found: {case_id}")
        return paths

    def trash_paths_for(self, case_id: str, require_exists: bool = False) -> CasePaths:
        paths = CasePaths.build(self.trash_root, case_id)
        if require_exists and not paths.case_dir.is_dir():
            raise CaseNotFoundError(f"Trashed case not found: {case_id}")
        return paths

    def create_case(self, owner_user_id: int | None = None) -> Dict[str, Any]:
        with self._lock:
            for _ in range(5):
                case_id = self.generate_case_id()
                paths = self.paths_for(case_id)
                trash_paths = self.trash_paths_for(case_id)
                if trash_paths.case_dir.exists():
                    continue
                try:
                    paths.case_dir.mkdir(parents=False, exist_ok=False)
                    break
                except FileExistsError:
                    continue
            else:
                raise CaseAlreadyExistsError("Unable to allocate a unique case_id")

            try:
                for directory in paths.directories():
                    directory.mkdir(parents=True, exist_ok=False)

                created_at = utc_now()
                modality_keys = list(MRI_MODALITIES.keys())
                case_info = CaseInfo(
                    case_id=case_id,
                    owner_user_id=owner_user_id,
                    required_modalities=modality_keys,
                    created_at=created_at,
                    updated_at=created_at,
                )
                manifest = UploadManifest(
                    case_id=case_id,
                    modalities={
                        key: ModalityUpload(
                            modality=definition.key,
                            channel_index=definition.channel_index,
                            nnunet_suffix=definition.nnunet_suffix,
                        )
                        for key, definition in MRI_MODALITIES.items()
                    },
                    created_at=created_at,
                    updated_at=created_at,
                )
                status = CaseStatusRecord(
                    case_id=case_id,
                    status=CaseStatus.CREATED,
                    message="脑胶质瘤病例已创建，等待上传四个 MRI 序列",
                    progress=0,
                    created_at=created_at,
                    updated_at=created_at,
                    history=[
                        StatusHistoryEntry(
                            status=CaseStatus.CREATED,
                            message="脑胶质瘤病例已创建，等待上传四个 MRI 序列",
                            progress=0,
                            timestamp=created_at,
                        )
                    ],
                )

                atomic_write_json(paths.case_info, case_info.model_dump(mode="json"))
                atomic_write_json(paths.upload_manifest, manifest.model_dump(mode="json"))
                atomic_write_json(paths.status, status.model_dump(mode="json"))
            except Exception:
                if paths.case_dir.is_dir() and paths.case_dir.parent == self.cases_root:
                    shutil.rmtree(paths.case_dir)
                raise

            return {
                "case_info": case_info.model_dump(mode="json"),
                "upload_manifest": manifest.model_dump(mode="json"),
                "status": status.model_dump(mode="json"),
                "paths": paths,
            }

    def read_case_info(self, case_id: str) -> CaseInfo:
        paths = self.paths_for(case_id, require_exists=True)
        return CaseInfo.model_validate(read_json(paths.case_info))

    def read_upload_manifest(self, case_id: str) -> UploadManifest:
        paths = self.paths_for(case_id, require_exists=True)
        return UploadManifest.model_validate(read_json(paths.upload_manifest))

    def read_status(self, case_id: str) -> CaseStatusRecord:
        paths = self.paths_for(case_id, require_exists=True)
        return CaseStatusRecord.model_validate(read_json(paths.status))

    def case_summary(self, case_id: str, include_history: bool = False) -> Dict[str, Any]:
        paths = self.paths_for(case_id, require_exists=True)
        case_info = self.read_case_info(case_id)
        manifest = self.read_upload_manifest(case_id)
        status = self.read_status(case_id)
        uploaded_modalities = [
            key for key, item in manifest.modalities.items() if item.uploaded
        ]
        payload = {
            "case_id": case_id,
            "display_name": case_info.display_name,
            "owner_user_id": case_info.owner_user_id,
            "workflow": case_info.workflow,
            "created_at": case_info.created_at.isoformat(),
            "updated_at": status.updated_at.isoformat(),
            "status": status.status.value,
            "message": status.message,
            "progress": status.progress,
            "upload_complete": manifest.complete,
            "uploaded_modalities": uploaded_modalities,
            "required_modalities": list(case_info.required_modalities),
            "outputs": {
                "segmentation": (paths.output / "segmentation.nii.gz").is_file(),
                "metrics": (paths.output / "metrics.json").is_file(),
                "report": (paths.output / "report.md").is_file(),
                "overlay": any(paths.overlay.iterdir()),
                "slices": any(paths.slices.iterdir()),
                "meshes": any(paths.meshes.iterdir()),
            },
        }
        if include_history:
            payload["history"] = [item.model_dump(mode="json") for item in status.history]
            payload["modalities"] = {
                key: {
                    "modality": item.modality,
                    "channel_index": item.channel_index,
                    "nnunet_suffix": item.nnunet_suffix,
                    "uploaded": item.uploaded,
                    "size_bytes": item.size_bytes,
                    "uploaded_at": item.uploaded_at.isoformat() if item.uploaded_at else None,
                    "nifti": item.nifti.model_dump(mode="json") if item.nifti else None,
                }
                for key, item in manifest.modalities.items()
            }
        return payload

    def list_cases(self) -> list[Dict[str, Any]]:
        cases = []
        for path in self.cases_root.iterdir():
            if not path.is_dir() or not CASE_ID_PATTERN.fullmatch(path.name):
                continue
            try:
                cases.append(self.case_summary(path.name))
            except (CaseServiceError, ValueError, OSError):
                continue
        cases.sort(key=lambda item: item["created_at"], reverse=True)
        return cases

    def save_case_info(self, case_info: CaseInfo) -> CaseInfo:
        with self._lock:
            paths = self.paths_for(case_info.case_id, require_exists=True)
            validated = CaseInfo.model_validate(case_info.model_dump())
            atomic_write_json(paths.case_info, validated.model_dump(mode="json"))
            return validated

    def rename_case(self, case_id: str, display_name: str) -> Dict[str, Any]:
        normalized_name = normalize_case_display_name(display_name)
        with self._lock:
            current = self.read_case_info(case_id)
            updated = current.model_copy(
                update={"display_name": normalized_name, "updated_at": utc_now()}
            )
            saved = self.save_case_info(updated)
            summary = self.case_summary(case_id)
            summary["display_name"] = saved.display_name
            return summary

    @staticmethod
    def _protected_delete_statuses() -> set[CaseStatus]:
        return {
            CaseStatus.UPLOADING,
            CaseStatus.VALIDATING,
            CaseStatus.QUEUED,
            CaseStatus.RUNNING,
        }

    def move_case_to_trash(self, case_id: str) -> Dict[str, Any]:
        """Atomically move an inactive case out of the active namespace."""

        protected_statuses = self._protected_delete_statuses()
        with self._lock:
            paths = self.paths_for(case_id, require_exists=True)
            trash_paths = self.trash_paths_for(case_id)
            status = self.read_status(case_id)
            if status.status in protected_statuses:
                raise CaseDeletionConflictError(
                    f"病例处于 {status.status.value} 状态，不能移入回收站"
                )
            if trash_paths.case_dir.exists():
                raise CaseDeletionConflictError("回收站中已存在同编号病例，不能重复删除")
            if paths.case_dir.parent != paths.root:
                raise InvalidCaseIdError("case directory escapes the configured cases directory")
            os.replace(paths.case_dir, trash_paths.case_dir)
            return {
                "case_id": case_id,
                "deleted": True,
                "trashed": True,
                "recoverable": True,
            }

    def delete_case(self, case_id: str) -> Dict[str, Any]:
        """Compatibility alias: application deletion now means recoverable trash."""

        return self.move_case_to_trash(case_id)

    def restore_case(self, case_id: str) -> Dict[str, Any]:
        with self._lock:
            paths = self.paths_for(case_id)
            trash_paths = self.trash_paths_for(case_id, require_exists=True)
            if paths.case_dir.exists():
                raise CaseDeletionConflictError("活动病例目录中已存在同编号病例，不能恢复")
            os.replace(trash_paths.case_dir, paths.case_dir)
            return {"case_id": case_id, "restored": True}

    def purge_case(self, case_id: str) -> Dict[str, Any]:
        """Permanently remove an exact active or trashed case directory."""

        with self._lock:
            active_paths = self.paths_for(case_id)
            trash_paths = self.trash_paths_for(case_id)
            if active_paths.case_dir.is_dir() and trash_paths.case_dir.is_dir():
                raise CaseDeletionConflictError("活动区和回收站同时存在病例目录，拒绝清除")
            target = trash_paths if trash_paths.case_dir.is_dir() else active_paths
            if not target.case_dir.is_dir():
                raise CaseNotFoundError(f"Case not found: {case_id}")
            if target is active_paths:
                status = self.read_status(case_id)
                if status.status in self._protected_delete_statuses():
                    raise CaseDeletionConflictError(
                        f"病例处于 {status.status.value} 状态，不能永久清除"
                    )
            if target.case_dir.parent != target.root:
                raise InvalidCaseIdError("case directory escapes the configured storage root")
            shutil.rmtree(target.case_dir)
            return {"case_id": case_id, "purged": True, "recoverable": False}

    def destroy_unregistered_case(self, case_id: str) -> None:
        """Rollback a just-created directory before it has been registered."""

        protected_statuses = {
            CaseStatus.UPLOADING,
            CaseStatus.VALIDATING,
            CaseStatus.QUEUED,
            CaseStatus.RUNNING,
        }
        with self._lock:
            paths = self.paths_for(case_id, require_exists=True)
            status = self.read_status(case_id)
            if status.status in protected_statuses:
                raise CaseDeletionConflictError(
                    f"病例处于 {status.status.value} 状态，不能删除"
                )
            if paths.case_dir.parent != paths.root:
                raise InvalidCaseIdError("case directory escapes the configured cases directory")
            shutil.rmtree(paths.case_dir)

    def trash_case_summary(self, case_id: str) -> Dict[str, Any]:
        paths = self.trash_paths_for(case_id, require_exists=True)
        case_info = CaseInfo.model_validate(read_json(paths.case_info))
        status = CaseStatusRecord.model_validate(read_json(paths.status))
        return {
            "case_id": case_id,
            "display_name": case_info.display_name,
            "created_at": case_info.created_at.isoformat(),
            "updated_at": status.updated_at.isoformat(),
            "status": status.status.value,
            "message": status.message,
        }

    def list_trashed_cases(self) -> list[Dict[str, Any]]:
        cases = []
        for path in self.trash_root.iterdir():
            if not path.is_dir() or not CASE_ID_PATTERN.fullmatch(path.name):
                continue
            try:
                cases.append(self.trash_case_summary(path.name))
            except (CaseServiceError, ValueError, OSError):
                continue
        cases.sort(key=lambda item: item["created_at"], reverse=True)
        return cases

    def save_upload_manifest(self, manifest: UploadManifest) -> UploadManifest:
        with self._lock:
            paths = self.paths_for(manifest.case_id, require_exists=True)
            validated = UploadManifest.model_validate(manifest.model_dump())
            atomic_write_json(paths.upload_manifest, validated.model_dump(mode="json"))
            return validated

    def transition_status(
        self,
        case_id: str,
        target: CaseStatus,
        message: str,
        progress: int,
    ) -> CaseStatusRecord:
        with self._lock:
            paths = self.paths_for(case_id, require_exists=True)
            current = self.read_status(case_id)
            target_status = CaseStatus(target)
            if (
                target_status != current.status
                and target_status not in ALLOWED_CASE_STATUS_TRANSITIONS[current.status]
            ):
                raise InvalidStatusTransitionError(
                    f"Invalid case status transition: {current.status.value} -> {target_status.value}"
                )

            changed_at = utc_now()
            history = list(current.history)
            history.append(
                StatusHistoryEntry(
                    status=target_status,
                    message=message,
                    progress=progress,
                    timestamp=changed_at,
                )
            )
            updated = current.model_copy(
                update={
                    "status": target_status,
                    "message": message,
                    "progress": progress,
                    "updated_at": changed_at,
                    "history": history,
                }
            )
            updated = CaseStatusRecord.model_validate(updated.model_dump())
            atomic_write_json(paths.status, updated.model_dump(mode="json"))
            return updated
