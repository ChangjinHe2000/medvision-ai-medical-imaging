from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from threading import RLock
from typing import Any
import uuid

from backend.glioma.case_service import CaseService as BrainCaseService

from .models import CaseType


CASE_ID_PATTERN = re.compile(r"^case_\d{8}T\d{6}Z_[0-9a-f]{8}$")


class StorageError(RuntimeError):
    pass


class UnifiedCaseStorage:
    """Keep CT and brain MRI files separate while sharing one case identity."""

    def __init__(
        self,
        *,
        ct_root: Path,
        ct_trash_root: Path,
        brain_root: Path,
        brain_trash_root: Path,
    ) -> None:
        self.ct_root = ct_root.resolve()
        self.ct_trash_root = ct_trash_root.resolve()
        self.brain_service = BrainCaseService(brain_root, brain_trash_root)
        self._lock = RLock()
        self.ct_root.mkdir(parents=True, exist_ok=True)
        self.ct_trash_root.mkdir(parents=True, exist_ok=True)
        if os.name == "nt" and self.ct_root.drive.casefold() != self.ct_trash_root.drive.casefold():
            raise StorageError("CT 病例目录与回收站必须位于同一磁盘")

    @staticmethod
    def _case_id() -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"case_{timestamp}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _normalize_display_name(value: str) -> str:
        normalized = " ".join(str(value or "").split())
        if not normalized or len(normalized) > 120:
            raise StorageError("病例显示名不能为空且不能超过 120 个字符")
        if any(character in normalized for character in ("/", "\\")):
            raise StorageError("病例显示名不能包含路径分隔符")
        return normalized

    def _ct_path(self, case_id: str, *, trash: bool = False, require: bool = False) -> Path:
        if not CASE_ID_PATTERN.fullmatch(case_id):
            raise StorageError("病例编号不合法")
        root = self.ct_trash_root if trash else self.ct_root
        target = (root / case_id).resolve()
        if target.parent != root:
            raise StorageError("病例路径越界")
        if require and not target.is_dir():
            raise StorageError("CT 病例目录不存在")
        return target

    def create(self, case_type: str | CaseType, display_name: str) -> dict[str, Any]:
        case_type = CaseType(case_type)
        display_name = self._normalize_display_name(display_name)
        if case_type == CaseType.BRAIN_MRI:
            created = self.brain_service.create_case(None)
            case_id = str(created["case_info"]["case_id"])
            self.brain_service.rename_case(case_id, display_name)
            return {"case_id": case_id, "case_type": case_type.value, "display_name": display_name}

        with self._lock:
            for _ in range(8):
                case_id = self._case_id()
                case_dir = self._ct_path(case_id)
                trash_dir = self._ct_path(case_id, trash=True)
                if trash_dir.exists():
                    continue
                try:
                    case_dir.mkdir(parents=False, exist_ok=False)
                    break
                except FileExistsError:
                    continue
            else:
                raise StorageError("无法生成唯一病例编号")
            try:
                (case_dir / "input").mkdir()
                (case_dir / "output" / "organs").mkdir(parents=True)
                (case_dir / "output" / "ppgl").mkdir(parents=True)
                created_at = datetime.now(timezone.utc).isoformat()
                self._atomic_json(case_dir / "case_info.json", {
                    "case_id": case_id,
                    "case_type": CaseType.PPGL_CT.value,
                    "display_name": display_name,
                    "created_at": created_at,
                })
                self._atomic_json(case_dir / "status.json", {
                    "case_id": case_id,
                    "status": "created",
                    "message": "PPGL CT 病例已创建，等待上传 CT",
                    "progress": 0,
                    "updated_at": created_at,
                })
                self._atomic_json(case_dir / "ppgl_status.json", {
                    "case_id": case_id,
                    "status": "created",
                    "message": "等待全器官结果与 PPGL 分割",
                    "progress": 0,
                    "updated_at": created_at,
                })
            except Exception:
                if case_dir.is_dir() and case_dir.parent == self.ct_root:
                    shutil.rmtree(case_dir)
                raise
            return {"case_id": case_id, "case_type": case_type.value, "display_name": display_name}

    def rollback_unregistered(self, case_id: str, case_type: str | CaseType) -> None:
        if CaseType(case_type) == CaseType.BRAIN_MRI:
            self.brain_service.destroy_unregistered_case(case_id)
            return
        target = self._ct_path(case_id, require=True)
        if target.parent == self.ct_root:
            shutil.rmtree(target)

    def rename(self, case_id: str, case_type: str | CaseType, display_name: str) -> None:
        display_name = self._normalize_display_name(display_name)
        if CaseType(case_type) == CaseType.BRAIN_MRI:
            self.brain_service.rename_case(case_id, display_name)
            return
        case_dir = self._ct_path(case_id, require=True)
        path = case_dir / "case_info.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["display_name"] = display_name
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._atomic_json(path, payload)

    def move_to_trash(self, case_id: str, case_type: str | CaseType) -> None:
        if CaseType(case_type) == CaseType.BRAIN_MRI:
            self.brain_service.move_case_to_trash(case_id)
            return
        with self._lock:
            source = self._ct_path(case_id, require=True)
            target = self._ct_path(case_id, trash=True)
            status_path = source / "status.json"
            status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {}
            if status.get("status") in {"queued", "running"}:
                raise StorageError("推理任务运行期间不能删除病例")
            if target.exists():
                raise StorageError("回收站已存在同编号病例")
            os.replace(source, target)

    def restore(self, case_id: str, case_type: str | CaseType) -> None:
        if CaseType(case_type) == CaseType.BRAIN_MRI:
            self.brain_service.restore_case(case_id)
            return
        with self._lock:
            source = self._ct_path(case_id, trash=True, require=True)
            target = self._ct_path(case_id)
            if target.exists():
                raise StorageError("活动区已存在同编号病例")
            os.replace(source, target)

    def purge(self, case_id: str, case_type: str | CaseType) -> None:
        if CaseType(case_type) == CaseType.BRAIN_MRI:
            self.brain_service.purge_case(case_id)
            return
        target = self._ct_path(case_id, trash=True, require=True)
        if target.parent != self.ct_trash_root:
            raise StorageError("拒绝清除越界路径")
        shutil.rmtree(target)

    def case_root(self, case_id: str, case_type: str | CaseType, *, trash: bool = False) -> Path:
        if CaseType(case_type) == CaseType.BRAIN_MRI:
            paths = (
                self.brain_service.trash_paths_for(case_id, require_exists=True)
                if trash
                else self.brain_service.paths_for(case_id, require_exists=True)
            )
            return paths.case_dir
        return self._ct_path(case_id, trash=trash, require=True)
