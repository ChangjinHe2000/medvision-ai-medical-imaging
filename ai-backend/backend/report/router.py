from __future__ import annotations

import json
import os
from pathlib import Path
import time
import traceback
from typing import Any, Callable, Dict, List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from report_agent import (
    build_ai_report_chat_request,
    chat_with_ai_report,
    direct_ai_report_answer,
    generate_ai_report,
    llm_provider,
    save_ai_report_chat,
    stream_openai_text,
)


class AiReportChatRequest(BaseModel):
    question: str
    history: List[Dict[str, Any]] = Field(default_factory=list)


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def optional_file_path(value: str) -> Path:
    if not str(value or "").strip():
        return Path("__missing__")
    return Path(value)


def stream_event(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def build_report_router(
    case_dir_for: Callable[[str], Path],
    start_runtime_activity: Callable[..., str],
    finish_runtime_activity: Callable[[str], None],
    start_trace: Callable[..., str] | None = None,
    finish_trace: Callable[..., None] | None = None,
) -> APIRouter:
    router = APIRouter()

    def report_trace(request: Request, case_id: str, question: str) -> str:
        if start_trace is None:
            return ""
        return str(start_trace(
            request,
            "report_chat",
            {
                "case_id": case_id,
                "model": os.environ.get("REPORT_OPENAI_MODEL", os.environ.get("CHAT_OPENAI_MODEL", "")),
                "question_fingerprint": {"length": len(question or "")},
            },
        ) or "")

    def finish_report_trace(trace_id: str, status: str, started_at: float, details: dict[str, Any]) -> None:
        if not trace_id or finish_trace is None:
            return
        duration_ms = (time.perf_counter() - started_at) * 1000
        if status == "completed":
            finish_trace(trace_id, status, duration_ms=duration_ms, result=details)
        else:
            finish_trace(trace_id, status, duration_ms=duration_ms, error=details)

    @router.get("/api/cases/{case_id}/report", response_class=PlainTextResponse)
    async def get_case_report(case_id: str):
        case_dir = case_dir_for(case_id)
        result_path = case_dir / "output" / "result.json"
        ppgl_result_path = case_dir / "output" / "ppgl" / "result.json"
        if not result_path.is_file() and not ppgl_result_path.is_file():
            raise HTTPException(status_code=404, detail="请先完成全器官分割或 PPGL 分割")
        result = read_json(result_path) if result_path.is_file() else {}
        ai_report_path = optional_file_path(result.get("outputs", {}).get("ai_report_markdown_path", ""))
        if ai_report_path.is_file():
            return ai_report_path.read_text(encoding="utf-8")

        fallback_ai_report_path = case_dir / "output" / "ai_report.md"
        if fallback_ai_report_path.is_file():
            return fallback_ai_report_path.read_text(encoding="utf-8")

        report_path = optional_file_path(result.get("outputs", {}).get("report_path", ""))
        if not report_path.is_file():
            raise HTTPException(status_code=404, detail="report.md not found")
        return report_path.read_text(encoding="utf-8")

    @router.post("/api/cases/{case_id}/ai-report/generate")
    async def generate_case_ai_report(case_id: str):
        case_dir = case_dir_for(case_id)
        result_path = case_dir / "output" / "result.json"
        ppgl_result_path = case_dir / "output" / "ppgl" / "result.json"
        if not result_path.exists() and not ppgl_result_path.exists():
            raise HTTPException(status_code=404, detail="请先完成分割，再生成 AI 报告")

        try:
            report = generate_ai_report(case_dir)
            (case_dir / "output" / "ai_report_error.log").unlink(missing_ok=True)
            return report
        except RuntimeError as exc:
            error_path = case_dir / "output" / "ai_report_error.log"
            error_path.write_text(str(exc), encoding="utf-8")
            raise HTTPException(status_code=502, detail=str(exc))
        except Exception as exc:
            error_path = case_dir / "output" / "ai_report_error.log"
            error_path.write_text(traceback.format_exc(), encoding="utf-8")
            raise HTTPException(status_code=500, detail=f"AI 报告生成失败：{exc}")

    @router.get("/api/cases/{case_id}/ai-report")
    async def get_case_ai_report(case_id: str):
        case_dir = case_dir_for(case_id)
        report_path = case_dir / "output" / "ai_report.json"
        if not report_path.exists():
            raise HTTPException(status_code=404, detail="ai_report.json not found")
        return read_json(report_path)

    @router.get("/api/cases/{case_id}/ai-report/chat")
    async def get_case_ai_report_chat(case_id: str):
        case_dir = case_dir_for(case_id)
        chat_path = case_dir / "output" / "ai_report_chat.json"
        if not chat_path.exists():
            return {"case_id": case_id, "messages": []}
        return read_json(chat_path)

    @router.post("/api/cases/{case_id}/ai-report/chat")
    async def chat_case_ai_report(case_id: str, request: Request, payload: AiReportChatRequest):
        case_dir = case_dir_for(case_id)
        result_path = case_dir / "output" / "result.json"
        ppgl_result_path = case_dir / "output" / "ppgl" / "result.json"
        if not result_path.exists() and not ppgl_result_path.exists():
            raise HTTPException(status_code=404, detail="请先完成分割，再进行 AI 问答")
        if not payload.question.strip():
            raise HTTPException(status_code=400, detail="问题不能为空")

        output_dir = case_dir / "output"
        if not (output_dir / "ai_report.md").exists() and not (output_dir / "ai_report.json").exists():
            raise HTTPException(status_code=404, detail="请先生成 AI 报告，再进行 AI 问答")

        activity_id = start_runtime_activity(
            "report",
            "AI 报告问答",
            os.environ.get("REPORT_OPENAI_MODEL", os.environ.get("CHAT_OPENAI_MODEL", "")),
        )
        trace_id = report_trace(request, case_id, payload.question)
        trace_started_at = time.perf_counter()
        try:
            response = chat_with_ai_report(case_dir, payload.question, payload.history)
            finish_report_trace(trace_id, "completed", trace_started_at, {"model": os.environ.get("REPORT_OPENAI_MODEL", "")})
            return response
        except RuntimeError as exc:
            finish_report_trace(trace_id, "failed", trace_started_at, {"type": type(exc).__name__, "message": str(exc)[:240]})
            error_path = case_dir / "output" / "ai_report_chat_error.log"
            error_path.write_text(str(exc), encoding="utf-8")
            raise HTTPException(status_code=502, detail=str(exc))
        except Exception as exc:
            finish_report_trace(trace_id, "failed", trace_started_at, {"type": type(exc).__name__, "message": str(exc)[:240]})
            error_path = case_dir / "output" / "ai_report_chat_error.log"
            error_path.write_text(traceback.format_exc(), encoding="utf-8")
            raise HTTPException(status_code=500, detail=f"AI 问答失败：{exc}")
        finally:
            finish_runtime_activity(activity_id)

    @router.post("/api/cases/{case_id}/ai-report/chat/stream")
    async def stream_chat_case_ai_report(case_id: str, request: Request, payload: AiReportChatRequest):
        case_dir = case_dir_for(case_id)
        result_path = case_dir / "output" / "result.json"
        ppgl_result_path = case_dir / "output" / "ppgl" / "result.json"
        if not result_path.exists() and not ppgl_result_path.exists():
            raise HTTPException(status_code=404, detail="请先完成分割，再进行 AI 问答")
        if not payload.question.strip():
            raise HTTPException(status_code=400, detail="问题不能为空")

        output_dir = case_dir / "output"
        if not (output_dir / "ai_report.md").exists() and not (output_dir / "ai_report.json").exists():
            raise HTTPException(status_code=404, detail="请先生成 AI 报告，再进行 AI 问答")

        def generate():
            answer_parts: list[str] = []
            trace_id = report_trace(request, case_id, payload.question)
            trace_started_at = time.perf_counter()
            activity_id = start_runtime_activity(
                "report",
                "AI 报告问答",
                os.environ.get("REPORT_OPENAI_MODEL", os.environ.get("CHAT_OPENAI_MODEL", "")),
            )
            try:
                direct = direct_ai_report_answer(case_dir, payload.question)
                if direct is not None:
                    clean_question, answer, metadata = direct
                    for index in range(0, len(answer), 18):
                        yield stream_event({"type": "delta", "text": answer[index : index + 18]})
                    chat = save_ai_report_chat(case_dir, clean_question, answer, {**metadata, "stream": True})
                    finish_report_trace(trace_id, "completed", trace_started_at, {"mode": "deterministic", "output_characters": len(answer)})
                    yield stream_event({"type": "done", "chat": chat})
                    return

                clean_question, prompt = build_ai_report_chat_request(
                    case_dir,
                    payload.question,
                    payload.history,
                )
                for delta in stream_openai_text(prompt):
                    answer_parts.append(delta)
                    yield stream_event({"type": "delta", "text": delta})

                answer = "".join(answer_parts).strip()
                if not answer:
                    raise RuntimeError("OpenAI API returned an empty answer")

                chat = save_ai_report_chat(
                    case_dir,
                    clean_question,
                    answer,
                    {"provider": llm_provider(), "stream": True},
                )
                finish_report_trace(trace_id, "completed", trace_started_at, {"model": os.environ.get("REPORT_OPENAI_MODEL", ""), "output_characters": len(answer)})
                yield stream_event({"type": "done", "chat": chat})
            except RuntimeError as exc:
                finish_report_trace(trace_id, "failed", trace_started_at, {"type": type(exc).__name__, "message": str(exc)[:240]})
                error_path = case_dir / "output" / "ai_report_chat_error.log"
                error_path.write_text(str(exc), encoding="utf-8")
                yield stream_event({"type": "error", "detail": str(exc)})
            except Exception as exc:
                finish_report_trace(trace_id, "failed", trace_started_at, {"type": type(exc).__name__, "message": str(exc)[:240]})
                error_path = case_dir / "output" / "ai_report_chat_error.log"
                error_path.write_text(traceback.format_exc(), encoding="utf-8")
                yield stream_event({"type": "error", "detail": f"AI 问答失败：{exc}"})
            finally:
                finish_runtime_activity(activity_id)

        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
