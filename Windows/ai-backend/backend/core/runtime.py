from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
from typing import Iterable


def configured_path(name: str, default: Path, base_dir: Path) -> Path:
    configured = Path(os.environ.get(name, str(default))).expanduser()
    return (configured if configured.is_absolute() else base_dir / configured).resolve()


def prepend_env_path(env: dict[str, str], key: str, values: Iterable[Path]) -> None:
    existing = [item for item in env.get(key, "").split(os.pathsep) if item]
    additions = [str(path) for path in values if path.exists()]
    merged = list(dict.fromkeys(additions + existing))
    if merged:
        env[key] = os.pathsep.join(merged)


def subprocess_runtime_env(*, python_paths: Iterable[Path] = ()) -> dict[str, str]:
    env = os.environ.copy()
    env_prefix = Path(sys.prefix).resolve()

    if os.name == "nt":
        prepend_env_path(
            env,
            "PATH",
            [env_prefix / "Scripts", env_prefix / "Library" / "bin", env_prefix],
        )
    else:
        prepend_env_path(env, "PATH", [env_prefix / "bin"])
        prepend_env_path(env, "LD_LIBRARY_PATH", [env_prefix / "lib"])

    prepend_env_path(env, "PYTHONPATH", python_paths)
    return env


def find_console_script(name: str) -> str:
    env_prefix = Path(sys.prefix).resolve()
    if os.name == "nt":
        candidates = [
            env_prefix / "Scripts" / f"{name}.exe",
            env_prefix / "Scripts" / f"{name}.cmd",
            env_prefix / "Scripts" / name,
        ]
    else:
        candidates = [env_prefix / "bin" / name]

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    command = shutil.which(name)
    if command:
        return command
    raise FileNotFoundError(f"{name} command not found. Install it in the project Python environment first.")
