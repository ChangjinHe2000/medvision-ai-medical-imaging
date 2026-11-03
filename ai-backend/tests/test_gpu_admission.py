from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from core.gpu_workbench import GpuWorkbench  # noqa: E402


class GpuAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workbench = GpuWorkbench(Path(directory.name))
        self.required_free_mb = self.workbench.required_free_mb("ppgl")

    @staticmethod
    def snapshot(free_mb: int) -> dict:
        return {"available": True, "primary": {"memory_free_mb": free_mb}}

    def test_low_memory_polls_after_one_unload_attempt_even_without_models(self) -> None:
        for unloaded_models in ([], ["test-model"]):
            with self.subTest(unloaded_models=unloaded_models):
                snapshots = [self.snapshot(0), self.snapshot(0), self.snapshot(self.required_free_mb)]
                on_wait = Mock()
                with (
                    patch.object(self.workbench, "gpu_snapshot", side_effect=snapshots),
                    patch.object(self.workbench, "unload_loaded_models", return_value=unloaded_models) as unload,
                    patch("core.gpu_workbench.time.sleep") as sleep,
                ):
                    task = self.workbench.begin("ppgl", "test-poll-" + str(len(unloaded_models)), on_wait)

                unload.assert_called_once_with()
                sleep.assert_called_once_with(self.workbench.poll_seconds)
                self.assertEqual(on_wait.call_count, 2)
                on_wait.assert_called_with(0, self.required_free_mb)
                self.assertEqual(task["status"], "running")
                self.assertEqual(task["admission"], "started_after_wait")
                self.assertEqual(task.get("unloaded_models", []), unloaded_models)

    def test_successful_unload_still_reports_waiting_before_start(self) -> None:
        on_wait = Mock()
        with (
            patch.object(self.workbench, "gpu_snapshot", side_effect=[self.snapshot(0), self.snapshot(self.required_free_mb)]),
            patch.object(self.workbench, "unload_loaded_models", return_value=["test-model"]),
            patch("core.gpu_workbench.time.sleep") as sleep,
        ):
            task = self.workbench.begin("ppgl", "test-unload", on_wait)

        on_wait.assert_called_once_with(0, self.required_free_mb)
        sleep.assert_not_called()
        self.assertEqual(task["admission"], "started_after_wait")
        self.assertEqual(task["unloaded_models"], ["test-model"])

    def test_sufficient_memory_starts_without_unloading_or_waiting(self) -> None:
        on_wait = Mock()
        with (
            patch.object(self.workbench, "gpu_snapshot", return_value=self.snapshot(self.required_free_mb)),
            patch.object(self.workbench, "unload_loaded_models") as unload,
            patch("core.gpu_workbench.time.sleep") as sleep,
        ):
            task = self.workbench.begin("ppgl", "test-ready", on_wait)

        unload.assert_not_called()
        sleep.assert_not_called()
        on_wait.assert_not_called()
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["admission"], "started")

    def test_unavailable_telemetry_preserves_existing_admission_behavior(self) -> None:
        with (
            patch.object(self.workbench, "gpu_snapshot", return_value={"available": False}),
            patch.object(self.workbench, "unload_loaded_models") as unload,
            patch("core.gpu_workbench.time.sleep") as sleep,
        ):
            task = self.workbench.begin("ppgl", "test-no-telemetry")

        unload.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["admission"], "started")


if __name__ == "__main__":
    unittest.main()
