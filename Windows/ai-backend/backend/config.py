from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def _path_env(name: str, default: Path, base: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    path = Path(raw).expanduser() if raw else default
    return (path if path.is_absolute() else base / path).resolve()


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class AppSettings:
    project_root: Path
    backend_root: Path
    data_root: Path
    cases_dir: Path
    logs_dir: Path
    pids_dir: Path
    temp_dir: Path
    tasks_dir: Path
    gpu_lock_file: Path
    task_timeout_seconds: int
    ppgl_model_dir: Path
    ppgl_checkpoint: Path
    ppgl_model_config: Path
    totalseg_weights_path: Path
    totalseg_home_dir: Path
    brain_model_dir: Path
    node_bin: Path | None
    gltfpack_bin: Path | None
    auth_database_url: str
    auth_session_hours: int
    auth_cookie_secure: bool
    auth_session_cookie_name: str
    auth_csrf_cookie_name: str
    case_trash_dir: Path
    brain_cases_dir: Path
    brain_trash_dir: Path

    @classmethod
    def from_env(cls) -> "AppSettings":
        backend_root = Path(__file__).resolve().parent
        project_root = backend_root.parents[1]
        data_root = _path_env(
            "PPGL_DATA_ROOT",
            Path.home() / "ppgl-assist-data",
            project_root,
        )
        cases_dir = _path_env("PPGL_CASES_DIR", data_root / "cases", data_root)
        logs_dir = _path_env("PPGL_LOG_DIR", data_root / "logs", data_root)
        pids_dir = _path_env("PPGL_PID_DIR", data_root / "pids", data_root)
        temp_dir = _path_env("PPGL_TEMP_DIR", data_root / "temp", data_root)
        tasks_dir = _path_env("PPGL_TASKS_DIR", data_root / "tasks", data_root)
        case_trash_dir = _path_env("PPGL_CASE_TRASH_DIR", data_root / "trash" / "ct", data_root)
        brain_cases_dir = _path_env("PPGL_GLIOMA_CASES_DIR", data_root / "brain-cases", data_root)
        brain_trash_dir = _path_env("PPGL_GLIOMA_TRASH_DIR", data_root / "trash" / "brain-mri", data_root)
        database_file = _path_env(
            "PPGL_AUTH_DATABASE_FILE",
            data_root / "database" / "medvision-enterprise.db",
            data_root,
        )
        auth_database_url = os.environ.get("PPGL_AUTH_DATABASE_URL", "").strip()
        if not auth_database_url:
            auth_database_url = f"sqlite:///{database_file.as_posix()}"
        gpu_lock_file = _path_env(
            "PPGL_GPU_LOCK_FILE",
            pids_dir / "gpu.lock",
            data_root,
        )

        ppgl_model_dir = _path_env(
            "PPGL_V5_MODEL_DIR",
            project_root / "ai-backend" / "progress_patch_v5",
            project_root,
        )
        node_raw = os.environ.get("PPGL_NODE_BIN", "").strip()
        gltfpack_raw = os.environ.get("PPGL_GLTFPACK_BIN", "").strip()
        settings = cls(
            project_root=project_root,
            backend_root=backend_root,
            data_root=data_root,
            cases_dir=cases_dir,
            logs_dir=logs_dir,
            pids_dir=pids_dir,
            temp_dir=temp_dir,
            tasks_dir=tasks_dir,
            gpu_lock_file=gpu_lock_file,
            task_timeout_seconds=_int_env("PPGL_TASK_TIMEOUT_SECONDS", 7200),
            ppgl_model_dir=ppgl_model_dir,
            ppgl_checkpoint=_path_env(
                "PPGL_V5_CHECKPOINT",
                ppgl_model_dir / "weights" / "model_best.pth",
                ppgl_model_dir,
            ),
            ppgl_model_config=_path_env(
                "PPGL_V5_MODEL_CONFIG",
                ppgl_model_dir / "model_config.json",
                ppgl_model_dir,
            ),
            totalseg_weights_path=_path_env(
                "TOTALSEG_WEIGHTS_PATH",
                data_root / "models" / "totalsegmentator" / "nnunet" / "results",
                data_root,
            ),
            totalseg_home_dir=_path_env(
                "TOTALSEG_HOME_DIR",
                data_root / "totalsegmentator",
                data_root,
            ),
            brain_model_dir=_path_env(
                "PPGL_NNUNET_MODEL_DIR",
                data_root
                / "models"
                / "brain-tumour"
                / "Dataset001_BrainTumour"
                / "nnUNetTrainer__nnUNetPlans__3d_fullres",
                data_root,
            ),
            node_bin=Path(node_raw).expanduser().resolve() if node_raw else None,
            gltfpack_bin=Path(gltfpack_raw).expanduser().resolve() if gltfpack_raw else None,
            auth_database_url=auth_database_url,
            auth_session_hours=_int_env("PPGL_AUTH_SESSION_HOURS", 8),
            auth_cookie_secure=_bool_env("PPGL_AUTH_COOKIE_SECURE", False),
            auth_session_cookie_name=os.environ.get("PPGL_AUTH_SESSION_COOKIE", "medvision_session").strip() or "medvision_session",
            auth_csrf_cookie_name=os.environ.get("PPGL_AUTH_CSRF_COOKIE", "medvision_csrf").strip() or "medvision_csrf",
            case_trash_dir=case_trash_dir,
            brain_cases_dir=brain_cases_dir,
            brain_trash_dir=brain_trash_dir,
        )
        settings.validate_runtime_boundaries()
        return settings

    def validate_runtime_boundaries(self) -> None:
        runtime_paths = {
            "PPGL_CASES_DIR": self.cases_dir,
            "PPGL_LOG_DIR": self.logs_dir,
            "PPGL_PID_DIR": self.pids_dir,
            "PPGL_TEMP_DIR": self.temp_dir,
            "PPGL_TASKS_DIR": self.tasks_dir,
            "PPGL_GPU_LOCK_FILE": self.gpu_lock_file,
            "TOTALSEG_HOME_DIR": self.totalseg_home_dir,
            "PPGL_CASE_TRASH_DIR": self.case_trash_dir,
            "PPGL_GLIOMA_CASES_DIR": self.brain_cases_dir,
            "PPGL_GLIOMA_TRASH_DIR": self.brain_trash_dir,
        }
        outside = {
            name: str(path)
            for name, path in runtime_paths.items()
            if not _inside(path, self.data_root)
        }
        if outside:
            raise RuntimeError(
                "Runtime paths must stay inside PPGL_DATA_ROOT: "
                + ", ".join(sorted(outside))
            )
        if _inside(self.data_root, self.project_root):
            raise RuntimeError("PPGL_DATA_ROOT must not be inside the source repository")

    def ensure_runtime_directories(self) -> None:
        for directory in (
            self.data_root,
            self.cases_dir,
            self.logs_dir,
            self.pids_dir,
            self.temp_dir,
            self.tasks_dir,
            self.totalseg_home_dir,
            self.case_trash_dir,
            self.brain_cases_dir,
            self.brain_trash_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        if self.auth_database_url.startswith("sqlite:///"):
            database_path = Path(self.auth_database_url.removeprefix("sqlite:///"))
            database_path.parent.mkdir(parents=True, exist_ok=True)


def load_settings(*, create_directories: bool = True) -> AppSettings:
    settings = AppSettings.from_env()
    if create_directories:
        settings.ensure_runtime_directories()
    return settings
