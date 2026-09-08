from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import start_system


class WindowsLauncherTests(unittest.TestCase):
    def test_explicit_environment_wins_over_old_ppgl_environment(self) -> None:
        configured = Path('custom-env/python.exe')
        with (
            patch.object(start_system, 'command_path', return_value=configured),
            patch.object(start_system.subprocess, 'run') as run,
        ):
            self.assertEqual(start_system.find_ppgl_python({'PPGL_PYTHON_BIN': str(configured)}), configured)
            run.assert_not_called()

    def test_invalid_explicit_python_does_not_fall_back_to_another_environment(self) -> None:
        with (
            patch.object(start_system, 'command_path', return_value=None),
            patch.object(start_system, 'setting', return_value='missing/python.exe'),
        ):
            self.assertIsNone(start_system.find_ppgl_python({'PPGL_PYTHON_BIN': 'missing/python.exe'}))

    def test_no_command_defaults_to_start(self) -> None:
        with (
            patch.object(start_system.sys, "argv", ["start_system.py"]),
            patch.object(start_system, "read_dotenv", return_value={}),
            patch.object(start_system, "start", return_value=0) as start,
        ):
            self.assertEqual(start_system.main(), 0)
        start.assert_called_once_with(
            {},
            no_browser=False,
            lan=False,
            timeout=90.0,
        )

    def test_dotenv_is_parsed_as_data_without_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env.local"
            path.write_text(
                "SAFE=value\nPASSWORD='a$b`c$(ignored)'\n# comment\n",
                encoding="utf-8",
            )
            values = start_system.read_dotenv(path)
        self.assertEqual(values["SAFE"], "value")
        self.assertEqual(values["PASSWORD"], "a$b`c$(ignored)")

    def test_invalid_dotenv_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env.local"
            path.write_text("export BAD=value\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                start_system.read_dotenv(path)

    def test_plain_executable_name_is_resolved_from_path(self) -> None:
        with patch.object(start_system.shutil, "which", return_value=r"D:\Tools\python.exe"):
            result = start_system.command_path({"PPGL_PYTHON_BIN": "python"}, "PPGL_PYTHON_BIN", ("python.exe", "python"))
        self.assertEqual(result, Path(r"D:\Tools\python.exe"))

    def test_process_environment_overrides_local_file(self) -> None:
        context = {
            "python": Path(r"D:\Anaconda3\envs\PPGL\python.exe"),
            "node": Path(r"D:\Tools\node.exe"),
            "gltfpack": None,
        }
        with patch.dict(os.environ, {"PPGL_DATA_ROOT": r"D:\isolated acceptance"}, clear=False):
            environment = start_system.child_environment({"PPGL_DATA_ROOT": r"E:\daily runtime"}, context)
        self.assertEqual(environment["PPGL_DATA_ROOT"], r"D:\isolated acceptance")

    def test_safe_url_removes_database_credentials(self) -> None:
        value = start_system.safe_url("postgresql://user:secret@127.0.0.1:5432/medvision")
        self.assertNotIn("user", value)
        self.assertNotIn("secret", value)
        self.assertEqual(value, "postgresql://127.0.0.1:5432/medvision")

    def test_external_service_is_never_terminated(self) -> None:
        with patch.object(start_system, "process_info") as process_info:
            result = start_system.terminate_entry({"name": "Ollama", "owned": False})
        self.assertEqual(result, "保留外部服务")
        process_info.assert_not_called()

    def test_pid_identity_requires_all_command_markers(self) -> None:
        entry = {"pid": 22, "executable": "python.exe", "markers": ["backend.main:app", "medvision"]}
        with patch.object(start_system, "process_info", return_value={
            "executable": r"D:\Anaconda3\envs\PPGL\python.exe",
            "command_line": "python -m uvicorn backend.main:app --app-dir medvision",
        }):
            self.assertTrue(start_system.process_matches(entry))
        with patch.object(start_system, "process_info", return_value={
            "executable": r"D:\Anaconda3\envs\PPGL\python.exe",
            "command_line": "python unrelated.py",
        }):
            self.assertFalse(start_system.process_matches(entry))

    def test_pid_identity_falls_back_to_executable_and_creation_time(self) -> None:
        entry = {
            "pid": 23,
            "executable": r"D:\Anaconda3\envs\PPGL\python.exe",
            "markers": ["backend.main:app"],
            "creation_epoch_ms": 100_000,
        }
        with patch.object(start_system, "process_info", return_value={
            "executable": r"D:\Anaconda3\envs\PPGL\python.exe",
            "command_line": "",
            "creation_epoch_ms": "100500",
        }):
            self.assertTrue(start_system.process_matches(entry))
        with patch.object(start_system, "process_info", return_value={
            "executable": r"D:\Anaconda3\envs\PPGL\python.exe",
            "command_line": "",
            "creation_epoch_ms": "200000",
        }):
            self.assertFalse(start_system.process_matches(entry))

    def test_pid_identity_uses_creation_time_when_windows_hides_process_paths(self) -> None:
        entry = {
            "pid": 24,
            "executable": r"D:\Anaconda3\envs\PPGL\python.exe",
            "markers": ["backend.main:app"],
            "creation_epoch_ms": 100_000,
        }
        with patch.object(start_system, "process_info", return_value={
            "executable": "",
            "command_line": "",
            "creation_epoch_ms": "100001",
        }):
            self.assertTrue(start_system.process_matches(entry))
        with patch.object(start_system, "process_info", return_value={
            "executable": "",
            "command_line": "",
            "creation_epoch_ms": "200000",
        }):
            self.assertFalse(start_system.process_matches(entry))

    def test_state_write_is_atomic_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            start_system.atomic_json(path, {"launch_id": "test", "services": []})
            self.assertEqual(start_system.load_state(path)["launch_id"], "test")
            self.assertFalse(any(Path(directory).glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
