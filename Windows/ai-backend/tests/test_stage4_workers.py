from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
import time
import unittest
from unittest.mock import patch

import nibabel as nib
import numpy as np

from backend.config import AppSettings
from backend.domain.task_contract import (
    RESULT_MANIFEST_SCHEMA,
    TaskErrorCode,
    TaskRecord,
    TaskStatus,
    TaskType,
    result_manifest,
)
from backend.workers.brain_nnunet.worker import BrainNnunetJob, BrainNnunetWorker
from backend.workers.ppgl_v5.worker import ProgressPatchV5Job, ProgressPatchV5Worker
from backend.workers.process_runner import (
    ProcessRunner,
    TaskExecutionContext,
    WorkerExecutionError,
    classify_failure,
)
from backend.workers.task_queue import UnifiedGpuTaskQueue
from backend.workers.validation import runtime_relative, validate_segmentation
from backend.workers.totalsegmentator.worker import TotalSegmentatorJob, TotalSegmentatorWorker


class TaskContractTests(unittest.TestCase):
    def test_all_required_statuses_and_manifest_contract(self) -> None:
        self.assertEqual(
            {item.value for item in TaskStatus},
            {"queued", "running", "completed", "failed", "cancelled", "timed_out"},
        )
        record = TaskRecord("task_demo", TaskType.PPGL_V5, "case_demo")
        record.transition(TaskStatus.RUNNING, message="运行中", progress=5)
        record.transition(TaskStatus.COMPLETED, message="完成", progress=100)
        self.assertEqual(record.to_dict()["status"], "completed")
        manifest = result_manifest(
            case_id="case_demo",
            workflow="ppgl_ct",
            segmentation="cases/case_demo/output/mask.nii.gz",
            metrics="cases/case_demo/output/metrics.json",
            visualization_manifest="cases/case_demo/output/slices/manifest.json",
            mesh="cases/case_demo/output/meshes/scene.glb",
        )
        self.assertEqual(manifest["schema_version"], RESULT_MANIFEST_SCHEMA)
        self.assertEqual(set(manifest["outputs"]), {"segmentation", "metrics", "visualization_manifest", "mesh", "report"})

    def test_invalid_terminal_transition_is_rejected(self) -> None:
        record = TaskRecord("task_demo", TaskType.PPGL_V5, "case_demo")
        record.transition(TaskStatus.CANCELLED, message="取消", progress=0)
        with self.assertRaises(ValueError):
            record.transition(TaskStatus.RUNNING, message="错误", progress=5)


class ConfigurationTests(unittest.TestCase):
    def test_windows_style_space_and_chinese_runtime_path(self) -> None:
        with TemporaryDirectory(prefix="MedVision 空格路径 ") as temporary:
            root = Path(temporary).resolve()
            env = {
                "PPGL_DATA_ROOT": str(root),
                "PPGL_CASES_DIR": str(root / "病例 数据"),
                "PPGL_LOG_DIR": str(root / "运行 日志"),
                "PPGL_PID_DIR": str(root / "进程 记录"),
                "PPGL_TEMP_DIR": str(root / "临时 文件"),
                "PPGL_TASKS_DIR": str(root / "任务 队列"),
                "PPGL_GPU_LOCK_FILE": str(root / "进程 记录" / "gpu.lock"),
                "TOTALSEG_HOME_DIR": str(root / "Total Segmentator"),
            }
            with patch.dict(os.environ, env, clear=False):
                settings = AppSettings.from_env()
                settings.ensure_runtime_directories()
            self.assertTrue(settings.tasks_dir.is_dir())
            self.assertTrue(settings.cases_dir.is_dir())

    def test_runtime_directory_cannot_escape_data_root(self) -> None:
        with TemporaryDirectory() as root, TemporaryDirectory() as outside:
            with patch.dict(
                os.environ,
                {
                    "PPGL_DATA_ROOT": root,
                    "PPGL_CASES_DIR": outside,
                },
                clear=False,
            ):
                with self.assertRaisesRegex(RuntimeError, "PPGL_CASES_DIR"):
                    AppSettings.from_env()


class ProcessRunnerTests(unittest.TestCase):
    def test_cuda_oom_is_classified_with_stable_code(self) -> None:
        code, message, retryable = classify_failure("RuntimeError: CUDA out of memory")
        self.assertEqual(code, TaskErrorCode.CUDA_OUT_OF_MEMORY)
        self.assertEqual(message, "GPU 显存不足")
        self.assertTrue(retryable)

    def test_missing_command_has_stable_error_code(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(WorkerExecutionError) as captured:
                ProcessRunner().run(
                    [str(root / "missing command.exe")],
                    cwd=root,
                    env=os.environ.copy(),
                    stdout_path=root / "stdout.log",
                    stderr_path=root / "stderr.log",
                    timeout_seconds=10,
                    context=TaskExecutionContext("task_missing_command"),
                )
            self.assertEqual(captured.exception.code, TaskErrorCode.COMMAND_NOT_FOUND.value)
    def test_success_logs_are_redacted_and_pid_is_persisted(self) -> None:
        with TemporaryDirectory(prefix="worker test ") as temporary:
            root = Path(temporary).resolve()
            secret = "TOP_SECRET_VALUE"
            runner = ProcessRunner(redaction_roots=(root,))
            env = os.environ.copy()
            env["DEMO_API_TOKEN"] = secret
            context = TaskExecutionContext("task_success")
            result = runner.run(
                [sys.executable, "-c", "import os; print(os.getcwd()); print(os.environ['DEMO_API_TOKEN']); print(r'D:\\private\\model.pth')"],
                cwd=root,
                env=env,
                stdout_path=root / "logs" / "stdout.log",
                stderr_path=root / "logs" / "stderr.log",
                timeout_seconds=10,
                context=context,
                pid_path=root / "pids" / "process.json",
            )
            text = (root / "logs" / "stdout.log").read_text(encoding="utf-8")
            pid_record = json.loads((root / "pids" / "process.json").read_text(encoding="utf-8"))
            self.assertEqual(result.return_code, 0)
            self.assertNotIn(str(root), text)
            self.assertNotIn(secret, text)
            self.assertNotIn(r"D:\private\model.pth", text)
            self.assertIn("已脱敏", text)
            self.assertEqual(pid_record["status"], "stopped")

    def test_timeout_has_stable_error_code(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(WorkerExecutionError) as captured:
                ProcessRunner().run(
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    cwd=root,
                    env=os.environ.copy(),
                    stdout_path=root / "stdout.log",
                    stderr_path=root / "stderr.log",
                    timeout_seconds=1,
                    context=TaskExecutionContext("task_timeout"),
                )
            self.assertEqual(captured.exception.code, TaskErrorCode.PROCESS_TIMEOUT.value)

    def test_running_process_can_be_cancelled(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = TaskExecutionContext("task_cancel")
            errors: list[WorkerExecutionError] = []

            def execute() -> None:
                try:
                    ProcessRunner().run(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        cwd=root,
                        env=os.environ.copy(),
                        stdout_path=root / "stdout.log",
                        stderr_path=root / "stderr.log",
                        timeout_seconds=60,
                        context=context,
                    )
                except WorkerExecutionError as exc:
                    errors.append(exc)

            thread = Thread(target=execute)
            thread.start()
            deadline = time.monotonic() + 5
            while context._process is None and time.monotonic() < deadline:
                time.sleep(0.02)
            context.cancel()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors[0].code, TaskErrorCode.PROCESS_CANCELLED.value)


class UnifiedQueueTests(unittest.TestCase):
    def test_gpu_handlers_run_serially(self) -> None:
        with TemporaryDirectory() as temporary:
            queue = UnifiedGpuTaskQueue(Path(temporary) / "tasks")
            gate = Lock()
            active = 0
            peak = 0

            def handler(context: TaskExecutionContext) -> dict:
                nonlocal active, peak
                with gate:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.15)
                with gate:
                    active -= 1
                return {"worker": context.task_id}

            first = queue.submit(task_type=TaskType.PPGL_V5, case_id="case_a", handler=handler)
            second = queue.submit(task_type=TaskType.BRAIN_NNUNET, case_id="case_b", handler=handler)
            self.assertEqual(queue.wait(first["task_id"], 5)["status"], "completed")
            self.assertEqual(queue.wait(second["task_id"], 5)["status"], "completed")
            queue.shutdown()
            self.assertEqual(peak, 1)

    def test_queued_task_cancel_and_observer(self) -> None:
        with TemporaryDirectory() as temporary:
            queue = UnifiedGpuTaskQueue(Path(temporary) / "tasks")
            release = Event()
            observed: list[str] = []

            def blocker(_: TaskExecutionContext) -> dict:
                release.wait(5)
                return {}

            first = queue.submit(task_type=TaskType.PPGL_V5, case_id="case_a", handler=blocker)
            second = queue.submit(
                task_type=TaskType.TOTALSEGMENTATOR,
                case_id="case_b",
                handler=lambda _: {},
                observer=lambda record: observed.append(record["status"]),
            )
            cancelled = queue.cancel(second["task_id"])
            release.set()
            queue.wait(first["task_id"], 5)
            queue.shutdown()
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(observed, ["queued", "cancelled"])

    def test_restart_recovers_interrupted_record(self) -> None:
        with TemporaryDirectory() as temporary:
            tasks_dir = Path(temporary) / "tasks"
            record_path = tasks_dir / "task_abcd" / "task.json"
            record_path.parent.mkdir(parents=True)
            record_path.write_text(
                json.dumps(
                    {
                        "task_id": "task_abcd",
                        "task_type": "ppgl_v5",
                        "case_id": "case_a",
                        "status": "running",
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            queue = UnifiedGpuTaskQueue(tasks_dir, autostart=False)
            recovered = queue.get("task_abcd")
            queue.shutdown()
            self.assertEqual(recovered["status"], "failed")
            self.assertEqual(recovered["error_code"], TaskErrorCode.WORKER_RESTARTED.value)
            self.assertTrue(recovered["retryable"])


class WorkerValidationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows extended path regression")
    def test_runtime_relative_accepts_windows_extended_length_path(self) -> None:
        root = Path(r"E:\runtime")
        extended = Path(r"\\?\E:\runtime\cases\case_1\output\segmentation.nii.gz")
        self.assertEqual(
            runtime_relative(extended, root),
            "cases/case_1/output/segmentation.nii.gz",
        )

    def test_ppgl_worker_rejects_missing_weight_before_launch(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "ct.nii.gz"
            nib.save(nib.Nifti1Image(np.ones((3, 3, 3), dtype=np.float32), np.eye(4)), image)
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "infer_single_case.py").write_text("", encoding="utf-8")
            config = model_dir / "model_config.json"
            config.write_text("{}", encoding="utf-8")
            job = ProgressPatchV5Job(
                case_id="case_demo",
                image_path=image,
                output_dir=root / "output",
                model_dir=model_dir,
                checkpoint=model_dir / "missing.pth",
                model_config=config,
                data_root=root,
                timeout_seconds=10,
            )
            with self.assertRaises(WorkerExecutionError) as captured:
                ProgressPatchV5Worker(ProcessRunner()).run(job, TaskExecutionContext("task_ppgl"))
            self.assertEqual(captured.exception.code, TaskErrorCode.MODEL_WEIGHT_MISSING.value)

    def test_totalseg_worker_rejects_missing_weight_before_launch(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "ct.nii.gz"
            nib.save(nib.Nifti1Image(np.ones((3, 3, 3), dtype=np.float32), np.eye(4)), image)
            job = TotalSegmentatorJob(
                case_id="case_demo",
                image_path=image,
                output_dir=root / "output",
                ai_backend_dir=root,
                data_root=root,
                weights_path=root / "missing-weights",
                home_dir=root / "totalseg-home",
                timeout_seconds=10,
            )
            with self.assertRaises(WorkerExecutionError) as captured:
                TotalSegmentatorWorker(ProcessRunner()).run(job, TaskExecutionContext("task_totalseg"))
            self.assertEqual(captured.exception.code, TaskErrorCode.MODEL_WEIGHT_MISSING.value)

    def test_segmentation_empty_and_spatial_mismatch_are_distinct(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.nii.gz"
            empty = root / "empty.nii.gz"
            shifted = root / "shifted.nii.gz"
            nib.save(nib.Nifti1Image(np.zeros((4, 4, 4), dtype=np.float32), np.eye(4)), reference)
            nib.save(nib.Nifti1Image(np.zeros((4, 4, 4), dtype=np.uint8), np.eye(4)), empty)
            affine = np.eye(4)
            affine[0, 3] = 2
            nib.save(nib.Nifti1Image(np.ones((4, 4, 4), dtype=np.uint8), affine), shifted)
            with self.assertRaises(WorkerExecutionError) as empty_error:
                validate_segmentation(empty, reference)
            with self.assertRaises(WorkerExecutionError) as spatial_error:
                validate_segmentation(shifted, reference)
            self.assertEqual(empty_error.exception.code, TaskErrorCode.OUTPUT_EMPTY.value)
            self.assertEqual(spatial_error.exception.code, TaskErrorCode.OUTPUT_SPATIAL_MISMATCH.value)

    def test_brain_worker_rejects_missing_model_before_launch(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            job = BrainNnunetJob(
                case_id="case_demo",
                input_dir=input_dir,
                flair_path=input_dir / "case_demo_0000.nii.gz",
                output_dir=root / "output",
                model_dir=root / "missing-model",
                data_root=root,
                timeout_seconds=10,
            )
            with self.assertRaises(WorkerExecutionError) as captured:
                BrainNnunetWorker(ProcessRunner()).run(job, TaskExecutionContext("task_model"))
            self.assertEqual(captured.exception.code, TaskErrorCode.MODEL_WEIGHT_MISSING.value)

    def test_brain_worker_runs_with_space_path_and_fake_predictor(self) -> None:
        with TemporaryDirectory(prefix="脑肿瘤 worker 空格 ") as temporary:
            root = Path(temporary)
            case_id = "case_demo"
            input_dir = root / "输入 数据"
            output_dir = root / "输出 数据"
            model_dir = root / "模型 快照"
            input_dir.mkdir()
            (model_dir / "fold_0").mkdir(parents=True)
            (model_dir / "fold_0" / "checkpoint_best.pth").write_bytes(b"test")
            image = nib.Nifti1Image(np.ones((5, 5, 5), dtype=np.float32), np.eye(4))
            for suffix in ("0000", "0001", "0002", "0003"):
                nib.save(image, input_dir / f"{case_id}_{suffix}.nii.gz")
            predictor = root / "fake predictor.py"
            predictor.write_text(
                "import argparse, pathlib, shutil\n"
                "p=argparse.ArgumentParser(add_help=False)\n"
                "p.add_argument('-i'); p.add_argument('-o'); p.add_argument('-m')\n"
                "p.add_argument('-f'); p.add_argument('-chk'); p.add_argument('-device')\n"
                "p.add_argument('-step_size'); p.add_argument('-npp'); p.add_argument('-nps')\n"
                "p.add_argument('--disable_progress_bar', action='store_true')\n"
                "p.add_argument('--disable_tta', action='store_true')\n"
                "a=p.parse_args(); source=next(pathlib.Path(a.i).glob('*_0000.nii.gz'))\n"
                "shutil.copy2(source, pathlib.Path(a.o)/(source.name[:-12]+'.nii.gz'))\n",
                encoding="utf-8",
            )
            result = BrainNnunetWorker(ProcessRunner(redaction_roots=(root,))).run(
                BrainNnunetJob(
                    case_id=case_id,
                    input_dir=input_dir,
                    flair_path=input_dir / f"{case_id}_0000.nii.gz",
                    output_dir=output_dir,
                    model_dir=model_dir,
                    data_root=root,
                    timeout_seconds=10,
                    predict_command=[sys.executable, str(predictor)],
                ),
                TaskExecutionContext("task_fake_brain"),
            )
            self.assertEqual(result["manifest"]["workflow"], "brats_brain_tumour")
            self.assertTrue((output_dir / f"{case_id}.nii.gz").is_file())


if __name__ == "__main__":
    unittest.main()
