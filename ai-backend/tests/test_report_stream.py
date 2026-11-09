from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from report.router import build_report_router  # noqa: E402


class ReportStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.case_dir = Path(directory.name) / "test-case"
        self.output = self.case_dir / "output"
        self.output.mkdir(parents=True)
        (self.output / "ai_report.md").write_text("Synthetic test report", encoding="utf-8")
        self.start_activity = Mock(return_value="test-activity")
        self.finish_activity = Mock()
        app = FastAPI()
        app.include_router(build_report_router(
            lambda case_id: self.case_dir,
            self.start_activity,
            self.finish_activity,
        ))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def write_result(self, relative_path: str) -> None:
        path = self.output / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    def post_question(self, question: str = "Test question"):
        return self.client.post(
            "/api/cases/test-case/ai-report/chat/stream",
            json={"question": question},
        )

    def test_stream_accepts_ppgl_only_and_existing_organ_results(self) -> None:
        for result_path in ("ppgl/result.json", "result.json"):
            with self.subTest(result_path=result_path):
                self.write_result(result_path)
                chat = {"answer": "Synthetic test answer"}
                self.start_activity.reset_mock()
                self.finish_activity.reset_mock()
                with (
                    patch("report.router.direct_ai_report_answer", return_value=("Test question", chat["answer"], {})),
                    patch("report.router.save_ai_report_chat", return_value=chat) as save_chat,
                    patch("report.router.stream_openai_text") as stream_model,
                ):
                    response = self.post_question()

                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers["content-type"].startswith("application/x-ndjson"))
                events = [json.loads(line) for line in response.text.splitlines()]
                self.assertEqual("".join(event["text"] for event in events if event["type"] == "delta"), chat["answer"])
                self.assertEqual(events[-1], {"type": "done", "chat": chat})
                save_chat.assert_called_once()
                stream_model.assert_not_called()
                self.start_activity.assert_called_once()
                self.finish_activity.assert_called_once_with("test-activity")
                (self.output / result_path).unlink()

    def test_missing_segmentation_still_returns_not_found(self) -> None:
        response = self.post_question()

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "请先完成分割，再进行 AI 问答")
        self.start_activity.assert_not_called()

    def test_ppgl_case_still_requires_a_generated_report(self) -> None:
        self.write_result("ppgl/result.json")
        (self.output / "ai_report.md").unlink()

        response = self.post_question()

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "请先生成 AI 报告，再进行 AI 问答")
        self.start_activity.assert_not_called()

    def test_ppgl_case_still_rejects_blank_questions(self) -> None:
        self.write_result("ppgl/result.json")

        response = self.post_question("  ")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "问题不能为空")
        self.start_activity.assert_not_called()


if __name__ == "__main__":
    unittest.main()
