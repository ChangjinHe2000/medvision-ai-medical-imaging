from __future__ import annotations

import json
from pathlib import Path
import traceback
from typing import Any, Callable, Dict, List

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.report_agent import (
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


def build_report_router(case_dir_for: Callable[[str], Path]) -> APIRouter:
    router = APIRouter()

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
    async def chat_case_ai_report(case_id: str, payload: AiReportChatRequest):
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

        try:
            return chat_with_ai_report(case_dir, payload.question, payload.history)
        except RuntimeError as exc:
            error_path = case_dir / "output" / "ai_report_chat_error.log"
            error_path.write_text(str(exc), encoding="utf-8")
            raise HTTPException(status_code=502, detail=str(exc))
        except Exception as exc:
            error_path = case_dir / "output" / "ai_report_chat_error.log"
            error_path.write_text(traceback.format_exc(), encoding="utf-8")
            raise HTTPException(status_code=500, detail=f"AI 问答失败：{exc}")

    @router.post("/api/cases/{case_id}/ai-report/chat/stream")
    async def stream_chat_case_ai_report(case_id: str, payload: AiReportChatRequest):
        case_dir = case_dir_for(case_id)
        result_path = case_dir / "output" / "result.json"
        if not result_path.exists():
            raise HTTPException(status_code=404, detail="请先完成分割，再进行 AI 问答")
        if not payload.question.strip():
            raise HTTPException(status_code=400, detail="问题不能为空")

        output_dir = case_dir / "output"
        if not (output_dir / "ai_report.md").exists() and not (output_dir / "ai_report.json").exists():
            raise HTTPException(status_code=404, detail="请先生成 AI 报告，再进行 AI 问答")

        def generate():
            answer_parts: list[str] = []
            try:
                direct = direct_ai_report_answer(case_dir, payload.question)
                if direct is not None:
                    clean_question, answer, metadata = direct
                    for index in range(0, len(answer), 18):
                        yield stream_event({"type": "delta", "text": answer[index : index + 18]})
                    chat = save_ai_report_chat(case_dir, clean_question, answer, {**metadata, "stream": True})
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
                yield stream_event({"type": "done", "chat": chat})
            except RuntimeError as exc:
                error_path = case_dir / "output" / "ai_report_chat_error.log"
                error_path.write_text(str(exc), encoding="utf-8")
                yield stream_event({"type": "error", "detail": str(exc)})
            except Exception as exc:
                error_path = case_dir / "output" / "ai_report_chat_error.log"
                error_path.write_text(traceback.format_exc(), encoding="utf-8")
                yield stream_event({"type": "error", "detail": f"AI 问答失败：{exc}"})

        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
