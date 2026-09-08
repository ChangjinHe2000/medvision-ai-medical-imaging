from __future__ import annotations

import json
import os
from pathlib import Path
from typing import BinaryIO


class GpuLockBusyError(RuntimeError):
    code = "GPU_BUSY_TRAINING_OR_INFERENCE"
    message = "GPU 已被训练或其他推理任务占用"
    details: dict = {}

    def __init__(self, lock_file: Path):
        super().__init__(self.message)
        self.details = {"lock_file": str(lock_file)}


class GpuFileLock:
    """Cross-process one-byte lock shared by nnU-Net training and inference."""

    def __init__(self, lock_file: Path, owner: str):
        self.lock_file = lock_file.expanduser().resolve()
        self.owner = owner
        self._stream: BinaryIO | None = None

    def acquire(self) -> None:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        stream = self.lock_file.open("a+b")
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            stream.close()
            raise GpuLockBusyError(self.lock_file) from exc
        self._stream = stream
        metadata = json.dumps(
            {"pid": os.getpid(), "owner": self.owner},
            ensure_ascii=False,
        ).encode("utf-8")
        stream.seek(1)
        stream.truncate()
        stream.write(metadata)
        stream.flush()

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._stream = None

    def __enter__(self) -> "GpuFileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

