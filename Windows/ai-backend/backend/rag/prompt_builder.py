from __future__ import annotations

import json
import re


def build_knowledge_rag_prompt(question: str, results: list[dict]) -> str:
    evidence_blocks = []
    for index, item in enumerate(results, start=1):
        source = item.get("title") or item.get("document_id") or "未知来源"
        section = item.get("section") or ""
        content = item.get("content") or ""
        version = item.get("version") or "未标注"
        pages = _page_text(item)
        evidence_blocks.append(
            f"[{index}] 来源：{source}；版本：{version}；位置：{pages}\n"
            f"章节：{section}\n"
            f"证据：{content}"
        )
    evidence = "\n\n".join(evidence_blocks)
    return (
        "你是 CT、PPGL 与脑肿瘤 MRI 医学知识库助手。请使用中文回答用户问题。\n"
        "只能使用下方检索证据，不得编造证据中没有的医学结论。\n"
        "每个关键结论后必须使用 [1]、[2] 这样的编号引用对应证据。\n"
        "如果证据不足，明确说明“当前知识库证据不足”。\n"
        "不得声称能够替代临床诊断或医生决策。\n\n"
        f"【用户问题】\n{question.strip()}\n\n"
        f"【检索证据】\n{evidence}"
    )


def citation_payload(results: list[dict]) -> list[dict]:
    citations = []
    for index, item in enumerate(results, start=1):
        citations.append(
            {
                "id": index,
                "score": item.get("score"),
                "dense_score": item.get("dense_score"),
                "bm25_score": item.get("bm25_score"),
                "rrf_score": item.get("rrf_score"),
                "reranker_score": item.get("reranker_score"),
                "title": item.get("title"),
                "organization": item.get("organization"),
                "version": item.get("version"),
                "section": item.get("section"),
                "page_start": item.get("page_start"),
                "page_end": item.get("page_end"),
                "chunk_id": item.get("chunk_id"),
                "document_id": item.get("document_id"),
                "doi": item.get("doi"),
                "pmcid": item.get("pmcid"),
                "source_url": item.get("source_url"),
                "content": item.get("content"),
            }
        )
    return citations


def evidence_assessment(answer: str, results: list[dict]) -> dict:
    cited_ids = {
        int(match)
        for match in re.findall(r"\[(\d+)\]", answer or "")
        if match.isdigit()
    }
    valid_cited_ids = sorted(index for index in cited_ids if 1 <= index <= len(results))
    top_scores = [
        float(item.get("score"))
        for item in results[:5]
        if item.get("score") is not None
    ]
    mean_top_score = sum(top_scores) / len(top_scores) if top_scores else 0.0
    citation_coverage = len(valid_cited_ids) / max(1, min(len(results), 5))

    warnings = []
    if not results:
        warnings.append("没有检索到可用证据")
    if not valid_cited_ids:
        warnings.append("回答中没有检测到有效证据引用")
    if citation_coverage < 0.4 and results:
        warnings.append("回答引用覆盖偏低，建议查看右侧原始证据")
    if mean_top_score < 0.2 and top_scores:
        warnings.append("检索相关度偏低，建议换一种问法或补充知识库")
    if "当前知识库证据不足" in (answer or ""):
        warnings.append("模型已声明当前知识库证据不足")

    if warnings:
        level = "low" if not valid_cited_ids or mean_top_score < 0.2 else "medium"
    else:
        level = "high"

    return {
        "level": level,
        "level_text": {
            "high": "证据较充分",
            "medium": "证据需复核",
            "low": "证据不足",
        }[level],
        "cited_count": len(valid_cited_ids),
        "cited_ids": valid_cited_ids,
        "retrieved_count": len(results),
        "citation_coverage": round(citation_coverage, 3),
        "mean_top_score": round(mean_top_score, 4),
        "warnings": warnings,
    }


def build_case_rag_prompt(question: str, case_context: dict, results: list[dict]) -> str:
    evidence_blocks = []
    for index, item in enumerate(results, start=1):
        evidence_blocks.append(
            f"[{index}] 来源：{item.get('title') or item.get('document_id')}；"
            f"版本：{item.get('version') or '未标注'}；位置：{_page_text(item)}\n"
            f"章节：{item.get('section', '')}\n"
            f"证据：{item.get('content', '')}"
        )
    return (
        "你是 CT、PPGL 与脑肿瘤 MRI 病例感知型 RAG 助手。\n"
        "请明确区分两类信息：\n"
        "1. 病例事实：只能来自【当前病例结构化数据】，引用为 [病例数据]。\n"
        "2. 医学知识：只能来自【检索证据】，引用为 [1]、[2]。\n"
        "不得根据器官体积擅自推断肿瘤、病灶、良恶性或最终诊断。\n"
        "如果当前病例没有肿瘤分割，必须明确说明无法回答病灶位置、大小或血管距离。\n"
        "建议按“病例事实”、“医学证据”、“局限性”组织回答。\n"
        "本系统不代替临床诊断或医生决策。\n\n"
        f"【用户问题】\n{question.strip()}\n\n"
        f"【当前病例结构化数据】\n{json.dumps(case_context, ensure_ascii=False, indent=2)}\n\n"
        f"【检索证据】\n" + "\n\n".join(evidence_blocks)
    )


def _page_text(item: dict) -> str:
    start = item.get("page_start")
    end = item.get("page_end")
    if start and end and start != end:
        return f"第 {start}-{end} 页"
    if start:
        return f"第 {start} 页"
    return f"片段 {item.get('chunk_id') or item.get('chunk_index') or '未标注'}"
