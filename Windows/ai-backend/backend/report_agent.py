from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


OPENAI_RESPONSES_PATH = "/responses"
OPENAI_CHAT_COMPLETIONS_PATH = "/chat/completions"
CHAT_COMPLETIONS_PROVIDERS = {"openai_compat", "chat_completions", "local", "minicpm"}
CHAT_SYSTEM_TEXT = (
    "你是一个中文对话助手。请按用户提供的最终提示、上下文和格式要求回答。"
)
ANSWER_STOP_MARKERS = (
    "\n\n用户：",
    "\n用户：",
    "\n\n用户:",
    "\n用户:",
    "\n\n用户",
    "\n用户",
    "\n\n医生：",
    "\n医生：",
    "\n\n医生:",
    "\n医生:",
    "\n\n医生",
    "\n医生",
    "\n\n助手：",
    "\n助手：",
    "\n\n助手:",
    "\n助手:",
    "\n\n助手",
    "\n助手",
    "\n\nUser:",
    "\nUser:",
    "\n\nAssistant:",
    "\nAssistant:",
)
TRAILING_ROLE_LABELS = ("用户", "用户：", "用户:", "医生", "医生：", "医生:", "助手", "助手：", "助手:")
TRAILING_ROLE_LABEL_RE = re.compile(r"(^|[\s\r\n]+)(用户|医生|助手)([:：])?\s*$")
STREAM_HOLD_BACK_CHARS = max(len(marker) for marker in ANSWER_STOP_MARKERS)


REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "case_id": {"type": "string"},
        "overall_risk": {
            "type": "string",
            "enum": ["low", "moderate", "high", "uncertain"],
        },
        "risk_summary": {"type": "string"},
        "key_findings": {
            "type": "array",
            "items": {"type": "string"},
        },
        "risk_reasons": {
            "type": "array",
            "items": {"type": "string"},
        },
        "anatomic_relationships": {
            "type": "array",
            "items": {"type": "string"},
        },
        "surgical_considerations": {
            "type": "array",
            "items": {"type": "string"},
        },
        "follow_up_suggestions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "missing_information": {
            "type": "array",
            "items": {"type": "string"},
        },
        "limitations": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "case_id",
        "overall_risk",
        "risk_summary",
        "key_findings",
        "risk_reasons",
        "anatomic_relationships",
        "surgical_considerations",
        "follow_up_suggestions",
        "missing_information",
        "limitations",
    ],
}

RISK_LEVEL_LABELS = {
    "low": "低风险",
    "moderate": "中等风险",
    "high": "高风险",
    "uncertain": "风险不确定",
}


def normalize_report_text(value: Any, fallback: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or fallback


def report_bullets(report: dict[str, Any], field: str, fallback: str) -> str:
    values = report.get(field, [])
    if not isinstance(values, list):
        values = []
    items = [normalize_report_text(value, "") for value in values]
    items = [value for value in items if value]
    if not items:
        items = [fallback]
    return "\n".join(f"- {value}" for value in items)


def render_report_markdown(report: dict[str, Any]) -> str:
    metadata = report.get("_metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    risk_code = str(report.get("overall_risk", "uncertain")).strip().lower()
    risk_label = RISK_LEVEL_LABELS.get(risk_code, RISK_LEVEL_LABELS["uncertain"])
    case_id = normalize_report_text(report.get("case_id"), "未提供")
    generated_at = normalize_report_text(metadata.get("generated_at"), "未记录")
    risk_summary = normalize_report_text(report.get("risk_summary"), "当前结构化指标不足以形成风险摘要。")

    return "\n".join(
        [
            "# PPGL AI 影像辅助分析报告",
            "",
            "## 一、报告基本信息",
            f"- 病例编号：{case_id}",
            f"- 报告生成时间：{generated_at}",
            "- 报告类型：基于自动分割结果的 AI 影像辅助分析",
            f"- 总体风险分层：{risk_label}",
            "",
            "## 二、影像分析摘要",
            risk_summary,
            "",
            "## 三、关键影像发现",
            report_bullets(report, "key_findings", "当前结构化结果未提供明确的关键影像发现。"),
            "",
            "## 四、风险评估依据",
            report_bullets(report, "risk_reasons", "当前结构化结果未提供明确的风险评估依据。"),
            "",
            "## 五、解剖毗邻关系",
            report_bullets(report, "anatomic_relationships", "当前结构化结果未提供明确的解剖毗邻关系。"),
            "",
            "## 六、术前关注事项",
            report_bullets(report, "surgical_considerations", "暂无可由自动分割结果直接支持的术前关注事项。"),
            "",
            "## 七、进一步检查与随访建议",
            report_bullets(report, "follow_up_suggestions", "请结合原始 CT、临床表现及相关检查由医生评估。"),
            "",
            "## 八、当前缺失信息",
            report_bullets(report, "missing_information", "未发现需要额外声明的缺失信息。"),
            "",
            "## 九、报告局限性",
            report_bullets(report, "limitations", "自动分割及 AI 分析结果可能存在误差，需由医生复核原始 CT。"),
            "",
            "## 十、辅助分析声明",
            "本报告由 AI 系统基于自动分割和结构化影像指标生成，仅供医生辅助参考，不能替代临床诊断、病理结论或最终治疗决策。",
            "",
        ]
    )


REPORT_ORGAN_LABELS = {
    "aorta": "主动脉",
    "kidney_left": "左肾",
    "kidney_right": "右肾",
    "inferior_vena_cava": "下腔静脉",
    "adrenal_gland_left": "左肾上腺",
    "adrenal_gland_right": "右肾上腺",
    "iliopsoas_left": "左髂腰肌",
    "iliopsoas_right": "右髂腰肌",
    "vertebrae": "椎体",
}


def report_string_list(value: Any, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [normalize_report_text(item, "") for item in value]
    return [item for item in items if item][:limit]


def finite_report_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def format_report_number(value: float, digits: int = 3) -> str:
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def report_risk_code(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in RISK_LEVEL_LABELS:
        return text
    if "高风险" in text or text == "high":
        return "high"
    if any(token in text for token in ("中等风险", "中风险", "moderate", "medium", "intermediate")):
        return "moderate"
    if "低风险" in text or text == "low":
        return "low"
    if "不确定" in text or text == "uncertain":
        return "uncertain"
    return None


def deterministic_report_fields(context: dict[str, Any]) -> dict[str, Any]:
    metrics = context.get("selected_metrics", {}) or {}
    components = metrics.get("components", []) or []
    components = [item for item in components if isinstance(item, dict)]
    relations = metrics.get("organ_relations", {}) or {}
    relations = relations if isinstance(relations, dict) else {}

    volume = finite_report_number(metrics.get("tumor_volume_ml"))
    component_count = finite_report_number(metrics.get("tumor_component_count"))
    side = str(metrics.get("tumor_side_by_nearest_kidney") or "").strip().lower()
    side_label = {"left": "左侧", "right": "右侧"}.get(side)

    diameters: list[float] = []
    for component in components:
        diameter = finite_report_number(component.get("max_diameter_mm"))
        if diameter is None:
            bbox = component.get("bbox_size_mm")
            bbox_values = [finite_report_number(item) for item in bbox] if isinstance(bbox, list) else []
            bbox_values = [item for item in bbox_values if item is not None]
            diameter = max(bbox_values) if bbox_values else None
        if diameter is not None:
            diameters.append(diameter)
    maximum_diameter = max(diameters) if diameters else None

    key_findings: list[str] = []
    if volume is not None:
        key_findings.append(f"自动分割显示肿瘤总体积约 {format_report_number(volume)} mL。")
    if component_count is not None:
        count_text = format_report_number(component_count, 0)
        if maximum_diameter is not None:
            key_findings.append(
                f"共识别 {count_text} 个肿瘤成分，最大成分最大径约 {format_report_number(maximum_diameter)} mm。"
            )
        else:
            key_findings.append(f"共识别 {count_text} 个肿瘤成分。")
    elif maximum_diameter is not None:
        key_findings.append(f"最大肿瘤成分最大径约 {format_report_number(maximum_diameter)} mm。")
    if side_label:
        key_findings.append(f"按与双肾的最近距离计算，肿瘤侧别为{side_label}。")

    ranked_relations: list[tuple[str, dict[str, Any], bool, float | None]] = []
    for key, raw_item in relations.items():
        if not isinstance(raw_item, dict):
            continue
        relation_key = str(raw_item.get("name") or key)
        overlap_voxels = finite_report_number(raw_item.get("overlap_voxels")) or 0.0
        contact = bool(raw_item.get("contact_or_overlap")) or overlap_voxels > 0
        distance = finite_report_number(raw_item.get("min_surface_distance_mm"))
        ranked_relations.append((relation_key, raw_item, contact, distance))
    ranked_relations.sort(
        key=lambda item: (
            not item[2],
            item[3] if item[3] is not None else float("inf"),
            item[0],
        )
    )

    anatomic_relationships: list[str] = []
    contact_labels: list[str] = []
    near_labels: list[str] = []
    for key, _, contact, distance in ranked_relations[:6]:
        label = REPORT_ORGAN_LABELS.get(key, key)
        if contact:
            contact_labels.append(label)
            anatomic_relationships.append(f"肿瘤与{label}的分割掩膜存在接触或重叠，需结合原始 CT 复核。")
        elif distance is not None:
            if distance <= 5.0:
                near_labels.append(label)
            anatomic_relationships.append(
                f"肿瘤与{label}的最小表面距离约 {format_report_number(distance)} mm。"
            )

    risk_reasons: list[str] = []
    if volume is not None or maximum_diameter is not None:
        measurements = []
        if volume is not None:
            measurements.append(f"总体积约 {format_report_number(volume)} mL")
        if maximum_diameter is not None:
            measurements.append(f"最大径约 {format_report_number(maximum_diameter)} mm")
        risk_reasons.append("自动分割提供的肿瘤定量依据为" + "、".join(measurements) + "。")
    if contact_labels:
        risk_reasons.append(f"分割掩膜显示肿瘤与{'、'.join(contact_labels)}存在接触或重叠。")
    if near_labels:
        risk_reasons.append(f"肿瘤与{'、'.join(near_labels)}的计算距离不超过 5 mm。")

    surgical_considerations: list[str] = []
    if contact_labels:
        surgical_considerations.append(
            f"术前需重点复核肿瘤与{'、'.join(contact_labels)}的真实边界及解剖关系。"
        )
    if near_labels:
        surgical_considerations.append(
            f"建议结合原始 CT 多平面影像确认肿瘤与{'、'.join(near_labels)}的毗邻关系。"
        )
    if not surgical_considerations and (volume is not None or anatomic_relationships):
        surgical_considerations.append("术前需由医生结合原始 CT 复核自动分割边界和病灶定位。")

    if key_findings or anatomic_relationships:
        risk_summary = "自动分割已提供肿瘤定量结果和毗邻结构距离，风险分层需结合原始 CT 及临床资料由医生复核。"
    else:
        risk_summary = "当前结构化分割结果未提供可用的肿瘤定量指标。"

    return {
        "case_id": str(context.get("case_id") or ""),
        "overall_risk": "uncertain",
        "risk_summary": risk_summary,
        "key_findings": key_findings,
        "risk_reasons": risk_reasons,
        "anatomic_relationships": anatomic_relationships,
        "surgical_considerations": surgical_considerations,
        "follow_up_suggestions": ["建议由医生结合原始 CT、临床表现及相关检查进行综合评估。"],
        "missing_information": ["当前结构化输入未提供患者症状、生化检查、遗传检测和病理结果。"],
        "limitations": ["自动分割及距离计算可能存在误差，必须由医生对照原始 CT 复核。"],
    }


def normalize_generated_report(raw_report: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    report = raw_report if isinstance(raw_report, dict) else {}
    nested = report.get("ai_analysis_report", {})
    nested = nested if isinstance(nested, dict) else {}
    fallback = deterministic_report_fields(context)

    nested_risk = nested.get("risk_assessment", {})
    nested_risk = nested_risk if isinstance(nested_risk, dict) else {}

    overall_risk = report_risk_code(report.get("overall_risk"))
    if overall_risk is None:
        nested_levels = [
            report_risk_code(value)
            for key, value in nested_risk.items()
            if not str(key).endswith("_reason")
        ]
        nested_levels = [value for value in nested_levels if value]
        if "high" in nested_levels:
            overall_risk = "high"
        elif "moderate" in nested_levels:
            overall_risk = "moderate"
        elif nested_levels and all(value == "low" for value in nested_levels):
            overall_risk = "low"
    overall_risk = overall_risk or fallback["overall_risk"]

    direct_risk_summary = normalize_report_text(report.get("risk_summary"), "")
    risk_summary = direct_risk_summary or fallback["risk_summary"]

    def normalized_list(field: str, nested_value: Any = None) -> list[str]:
        direct = report_string_list(report.get(field))
        if direct:
            return direct
        converted = report_string_list(nested_value)
        if converted:
            return converted
        return list(fallback[field])

    normalized = {
        "case_id": normalize_report_text(report.get("case_id") or nested.get("case_id") or context.get("case_id"), fallback["case_id"]),
        "overall_risk": overall_risk,
        "risk_summary": risk_summary,
        "key_findings": normalized_list("key_findings", nested.get("key_findings")),
        "risk_reasons": normalized_list("risk_reasons"),
        "anatomic_relationships": normalized_list("anatomic_relationships", nested.get("anatomic_relationships")),
        "surgical_considerations": normalized_list("surgical_considerations", nested.get("surgical_considerations")),
        "follow_up_suggestions": normalized_list("follow_up_suggestions", nested.get("follow_up_suggestions")),
        "missing_information": normalized_list("missing_information", nested.get("missing_information")),
        "limitations": normalized_list("limitations", nested.get("limitations")),
    }
    metadata = report.get("_metadata", {})
    normalized["_metadata"] = metadata if isinstance(metadata, dict) else {}
    return normalized


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    temp_path.replace(path)


def optional_file_path(value: str) -> Path:
    if not str(value or "").strip():
        return Path("__missing__")
    return Path(value)


def normalize_base_url(value: str) -> str:
    base = value.strip().rstrip("/")
    if not base:
        base = "http://127.0.0.1:11434/v1"
    if not base.endswith("/v1"):
        base = base + "/v1"
    return base


def normalize_api_key(value: str) -> str:
    key = str(value or "").strip()
    if key.lower().startswith("bearer "):
        key = key.split(None, 1)[1].strip()
    try:
        key.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError("OPENAI_API_KEY contains non-ASCII characters. Please paste only the raw API key.") from exc
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
        raise RuntimeError(
            "OPENAI_API_KEY format is invalid. Please paste only the raw API key, "
            "without export/read/cd/python commands or newlines."
        )
    return key


def clean_provider(value: str) -> str:
    return str(value or "").strip().lower() or "openai"


def report_llm_provider() -> str:
    return clean_provider(os.environ.get("REPORT_LLM_PROVIDER", "openai_compat"))


def llm_provider() -> str:
    return clean_provider(os.environ.get("CHAT_LLM_PROVIDER", os.environ.get("LLM_PROVIDER", "openai_compat")))


def provider_for_scope(scope: str) -> str:
    return report_llm_provider() if scope == "report" else llm_provider()


def use_chat_completions_api(provider: str | None = None) -> bool:
    return clean_provider(provider or llm_provider()) in CHAT_COMPLETIONS_PROVIDERS


def model_for_scope(scope: str) -> str:
    ollama_model = os.environ.get("PPGL_OLLAMA_MODEL", "").strip()
    if scope == "report":
        return os.environ.get("REPORT_OPENAI_MODEL", os.environ.get("CHAT_OPENAI_MODEL", ollama_model or "qwen2.5:3b-instruct-q4_0")).strip() or ollama_model or "qwen2.5:3b-instruct-q4_0"
    return os.environ.get("CHAT_OPENAI_MODEL", ollama_model or "qwen2.5:3b-instruct-q4_0").strip() or ollama_model or "qwen2.5:3b-instruct-q4_0"


def base_url_for_scope(scope: str) -> str:
    ollama_url = os.environ.get("PPGL_OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/") + "/v1"
    if scope == "report":
        return normalize_base_url(os.environ.get("REPORT_OPENAI_BASE_URL", os.environ.get("CHAT_OPENAI_BASE_URL", ollama_url)))
    return normalize_base_url(
        os.environ.get("CHAT_OPENAI_BASE_URL", ollama_url)
    )


def api_key_for_scope(scope: str) -> str:
    if scope == "report":
        return normalize_api_key(os.environ.get("REPORT_OPENAI_API_KEY", os.environ.get("CHAT_OPENAI_API_KEY", "ollama")))
    return normalize_api_key(os.environ.get("CHAT_OPENAI_API_KEY", "ollama"))


def timeout_for_scope(scope: str) -> float:
    if scope == "report":
        return float(os.environ.get("REPORT_OPENAI_TIMEOUT_SECONDS", os.environ.get("PPGL_OLLAMA_READ_TIMEOUT_SECONDS", os.environ.get("OPENAI_TIMEOUT_SECONDS", "120"))))
    return float(os.environ.get("CHAT_OPENAI_TIMEOUT_SECONDS", os.environ.get("PPGL_OLLAMA_READ_TIMEOUT_SECONDS", os.environ.get("OPENAI_TIMEOUT_SECONDS", "240"))))


def reasoning_effort_for_scope(scope: str) -> str:
    if scope == "report":
        return os.environ.get("REPORT_OPENAI_REASONING_EFFORT", os.environ.get("OPENAI_REASONING_EFFORT", "xhigh")).strip().lower()
    return os.environ.get("CHAT_OPENAI_REASONING_EFFORT", os.environ.get("OPENAI_REASONING_EFFORT", "")).strip().lower()


def auth_headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def compact_case_context(case_dir: Path) -> dict[str, Any]:
    result_path = case_dir / "output" / "result.json"
    ppgl_result_path = case_dir / "output" / "ppgl" / "result.json"
    if not result_path.exists() and not ppgl_result_path.exists():
        raise FileNotFoundError(f"segmentation result not found: {case_dir / 'output'}")

    result = read_json(result_path) if result_path.exists() else {}
    ppgl_result = read_json(ppgl_result_path) if ppgl_result_path.exists() else {}
    outputs = result.get("outputs", {})
    llm_context_path = optional_file_path(outputs.get("llm_context_path", ""))
    metrics_path = optional_file_path(outputs.get("clinical_metrics_path", ""))

    llm_context = read_json(llm_context_path) if llm_context_path.is_file() else {}
    metrics = read_json(metrics_path) if metrics_path.is_file() else {}
    metrics.update(ppgl_result.get("metrics") or {})

    # Keep prompt payload structured and privacy-minimized. Do not include image data.
    return {
        "case_id": case_dir.name,
        "summary": result.get("summary", {}),
        "risk_assessment": result.get("risk_assessment", {}),
        "segmentation": {
            "label_count": result.get("segmentation", {}).get("label_count"),
            "tumor_priority": result.get("segmentation", {}).get("tumor_priority"),
            "ppgl_model": ppgl_result.get("model", {}).get("name"),
            "tumor_detected": ppgl_result.get("segmentation", {}).get("tumor_detected"),
        },
        "llm_context": {
            "schema_version": llm_context.get("schema_version"),
            "purpose": llm_context.get("purpose"),
            "report_guidance": llm_context.get("report_guidance", []),
            "risk_assessment": llm_context.get("risk_assessment", {}),
            "origin_assessment": llm_context.get("origin_assessment", {}),
            "nearest_anatomic_structures": llm_context.get("nearest_anatomic_structures", [])[:12],
            "ct_intensity_descriptive_stats": llm_context.get("ct_intensity_descriptive_stats", {}),
            "segmentation_quality": llm_context.get("segmentation_quality", {}),
        },
        "selected_metrics": {
            "tumor_volume_ml": metrics.get("tumor_volume_ml"),
            "tumor_component_count": metrics.get("tumor_component_count"),
            "components": metrics.get("components", [])[:3],
            "anchor_distances_mm": metrics.get("anchor_distances_mm", {}),
            "tumor_side_by_nearest_kidney": metrics.get("tumor_side_by_nearest_kidney"),
            "organ_relations": metrics.get("organ_relations", {}),
        },
    }


def build_prompt(context: dict[str, Any]) -> str:
    return (
        "请基于以下 PPGL/嗜铬细胞瘤术前影像分割结构化指标，生成面向医生端的中文 AI 辅助分析报告。\n"
        "要求：\n"
        "1. 只基于输入指标，不要编造患者症状、实验室、生化、遗传、病理或治疗信息。\n"
        "2. 使用谨慎语言，例如“影像提示”“自动分割结果显示”“需结合原始 CT 复核”。\n"
        "3. 明确说明本报告不能替代医生诊断，不能给出最终治疗决策。\n"
        "4. 风险判断要解释原因，特别关注肿瘤体积、最大径、左右侧、与肾上腺/肾脏/主动脉/下腔静脉/肝脏的距离或重叠。\n"
        "5. 输出必须严格符合 JSON Schema；系统会将结构化字段填入固定报告模板。\n\n"
        "结构化输入如下：\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}"
    )


def is_minicpm_runtime(scope: str = "chat") -> bool:
    model = model_for_scope(scope).lower()
    provider = provider_for_scope(scope)
    return provider in {"local", "minicpm"} or "minicpm" in model


def report_output_token_limit() -> int:
    default = "512" if is_minicpm_runtime("report") else "3000"
    value = os.environ.get("OPENAI_REPORT_MAX_OUTPUT_TOKENS", default)
    try:
        return max(256, min(3000, int(value)))
    except ValueError:
        return int(default)


def chat_output_token_limit(max_output_tokens: int) -> int:
    if not is_minicpm_runtime("chat"):
        return max_output_tokens
    value = os.environ.get("CHAT_OPENAI_MAX_OUTPUT_TOKENS", "384")
    try:
        return min(max_output_tokens, max(128, min(1024, int(value))))
    except ValueError:
        return min(max_output_tokens, 384)


def compact_context_for_small_model_report(context: dict[str, Any]) -> dict[str, Any]:
    metrics = context.get("selected_metrics", {}) or {}
    relations = metrics.get("organ_relations", {}) or {}
    ranked_relations = sorted(
        relations.values(),
        key=lambda item: (
            not bool(item.get("contact_or_overlap")),
            float(item.get("min_surface_distance_mm") or 9999),
        ),
    )
    return {
        "case_id": context.get("case_id"),
        "summary": context.get("summary", {}),
        "risk_assessment": context.get("risk_assessment", {}),
        "origin_assessment": (context.get("llm_context", {}) or {}).get("origin_assessment", {}),
        "segmentation_quality": (context.get("llm_context", {}) or {}).get("segmentation_quality", {}),
        "tumor": {
            "volume_ml": metrics.get("tumor_volume_ml"),
            "component_count": metrics.get("tumor_component_count"),
            "side": metrics.get("tumor_side_by_nearest_kidney"),
            "components": metrics.get("components", [])[:1],
        },
        "anchor_distances_mm": metrics.get("anchor_distances_mm", {}),
        "nearest_anatomic_structures": (context.get("llm_context", {}) or {}).get("nearest_anatomic_structures", [])[:8],
        "important_organ_relations": ranked_relations[:10],
    }


def compact_context_for_small_model_chat(context: dict[str, Any]) -> dict[str, Any]:
    metrics = context.get("selected_metrics", {}) or {}
    relations = metrics.get("organ_relations", {}) or {}
    ranked_relations = sorted(
        relations.values(),
        key=lambda item: (
            not bool(item.get("contact_or_overlap")),
            float(item.get("min_surface_distance_mm") or 9999),
        ),
    )
    brief_relations = []
    for item in ranked_relations[:6]:
        brief_relations.append(
            {
                "name": item.get("name"),
                "group": item.get("group"),
                "min_surface_distance_mm": item.get("min_surface_distance_mm"),
                "distance_category": item.get("distance_category"),
                "overlap_voxels": item.get("overlap_voxels"),
            }
        )

    component = {}
    components = metrics.get("components", []) or []
    if components:
        component = {
            "volume_ml": components[0].get("volume_ml"),
            "bbox_size_mm": components[0].get("bbox_size_mm"),
            "centroid_mm": components[0].get("centroid_mm"),
        }

    risk = context.get("risk_assessment", {}) or {}
    return {
        "case_id": context.get("case_id"),
        "overall_risk": risk.get("overall_level"),
        "surgical_complexity": risk.get("surgical_complexity_level"),
        "segmentation_confidence": risk.get("segmentation_confidence"),
        "tumor_volume_ml": metrics.get("tumor_volume_ml"),
        "tumor_component_count": metrics.get("tumor_component_count"),
        "tumor_side": metrics.get("tumor_side_by_nearest_kidney"),
        "largest_component": component,
        "anchor_distances_mm": metrics.get("anchor_distances_mm", {}),
        "nearest_relations": brief_relations,
    }


def build_minicpm_report_prompt(context: dict[str, Any]) -> str:
    compact_context = compact_context_for_small_model_report(context)
    return (
        "请基于以下 PPGL/嗜铬细胞瘤术前影像分割指标生成医生端中文辅助报告。\n"
        "只能依据输入指标，不要编造症状、生化、遗传、病理、治疗或生存期信息。\n"
        "必须只输出一个合法 JSON 对象，不能输出 Markdown 代码围栏、解释文字或注释。\n"
        "JSON 必须包含这些字段：case_id, overall_risk, risk_summary, key_findings, risk_reasons, "
        "anatomic_relationships, surgical_considerations, follow_up_suggestions, missing_information, "
        "limitations。\n"
        "overall_risk 只能是 low、moderate、high、uncertain 之一。\n"
        "所有数组最多 2 条，每条尽量不超过 45 个中文字符。\n"
        "强调自动分割需医生结合原始 CT 复核，不能替代临床诊断。\n\n"
        "结构化输入：\n"
        f"{json.dumps(compact_context, ensure_ascii=False, indent=2)}"
    )


def clip_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[内容过长，已截断]"


def load_ai_report_text(case_dir: Path) -> str:
    output_dir = case_dir / "output"
    markdown_path = output_dir / "ai_report.md"
    if markdown_path.is_file():
        return markdown_path.read_text(encoding="utf-8")

    json_path = output_dir / "ai_report.json"
    if json_path.is_file():
        report = read_json(json_path)
        text = report.get("report_markdown", "")
        if text:
            return str(text)

    raise FileNotFoundError(f"AI report not found for case: {case_dir.name}")


def load_ai_report_summary(case_dir: Path) -> dict[str, Any]:
    json_path = case_dir / "output" / "ai_report.json"
    if not json_path.is_file():
        return {}
    report = read_json(json_path)
    def clip_list(name: str, count: int, limit: int) -> list[str]:
        return [clip_text(str(item), limit) for item in (report.get(name, []) or [])[:count]]

    return {
        "overall_risk": report.get("overall_risk"),
        "risk_summary": clip_text(str(report.get("risk_summary", "")), 260),
        "key_findings": clip_list("key_findings", 3, 90),
        "risk_reasons": clip_list("risk_reasons", 3, 100),
        "anatomic_relationships": clip_list("anatomic_relationships", 3, 100),
        "surgical_considerations": clip_list("surgical_considerations", 2, 100),
        "limitations": clip_list("limitations", 1, 100),
    }


def format_chat_history(history: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    history_limit = 3 if is_minicpm_runtime("chat") else 8
    item_limit = 350 if is_minicpm_runtime("chat") else 1200
    for item in (history or [])[-history_limit:]:
        role = str(item.get("role", "")).strip()
        if role not in {"user", "assistant"}:
            continue
        content = clip_text(str(item.get("content", "")), item_limit)
        if not content:
            continue
        if content.lstrip().startswith(("医生：", "医生:", "助手：", "助手:", "用户：", "用户:")):
            continue
        if any(
            marker in content
            for marker in (
                "你是什么模型",
                "是什么模型",
                "当前问答模型",
                "问答模型是谁",
                "当前 AI 问答调用的是本地",
                "结构化 AI 报告生成仍使用云端强模型",
                "接口地址是 http://127.0.0.1:11434/v1",
            )
        ):
            continue
        label = "用户" if role == "user" else "AI"
        rows.append(f"{label}: {content}")
    return "\n\n".join(rows)


def build_minicpm_chat_prompt(
    context: dict[str, Any],
    report_summary: dict[str, Any],
    question: str,
    history: list[dict[str, Any]],
) -> str:
    compact_context = compact_context_for_small_model_chat(context)
    history_text = format_chat_history(history)
    question_text = question.strip()
    serious_keywords = ("严重", "风险", "危险", "高吗", "重吗")
    answer_hint = ""
    if any(keyword in question_text for keyword in serious_keywords):
        answer_hint = (
            "本问题询问严重程度/风险高低。必须先回答：按当前 AI 影像辅助评估，"
            f"总体风险为 {report_summary.get('overall_risk') or compact_context.get('overall_risk') or '未知'}。"
            "如果风险为 high，要说明这是影像解剖风险/手术复杂度风险，不等同于恶性或生存期判断。"
        )
    return (
        "请基于病例影像报告和结构化指标，用中文回答用户问题。\n"
        "输出要求：\n"
        "1. 不要寒暄，不要说“很高兴为您服务”。\n"
        "2. 不要输出“医生：”“助手：”“用户：”“问题：”等角色标签。\n"
        "3. 不要复述用户问题，直接给答案正文。\n"
        "4. 只能根据下方报告摘要和指标回答，不要编造症状、生化、遗传、病理或治疗信息。\n"
        "5. 问严重程度或风险高低时，必须根据 overall_risk 和关键距离回答；不要泛泛说资料不足。\n"
        "6. 只有在询问恶性、确诊、寿命、生存期、治疗方案、血压/生化风险且输入缺失时，才说“当前资料不足以判断”。\n"
        "7. 回答控制在 3 句话以内；结尾提示需医生结合原始 CT 和临床资料判断。\n\n"
        "回答要点：\n"
        f"{answer_hint or '根据报告摘要和关键指标直接回答。'}\n\n"
        "报告摘要：\n"
        f"{json.dumps(report_summary, ensure_ascii=False, indent=2)}\n\n"
        "关键指标：\n"
        f"{json.dumps(compact_context, ensure_ascii=False, indent=2)}\n\n"
        "最近对话摘要：\n"
        f"{history_text or '无'}\n\n"
        "用户问题：\n"
        f"{question_text}\n\n"
        "只输出答案正文："
    )


def build_chat_prompt(
    context: dict[str, Any],
    ai_report_text: str,
    question: str,
    history: list[dict[str, Any]],
) -> str:
    history_text = format_chat_history(history)
    local_chat = is_minicpm_runtime("chat")
    context_for_prompt = compact_context_for_small_model_report(context) if local_chat else context
    report_limit = 2200 if local_chat else 9000
    return (
        "请基于下面已有 AI 影像辅助报告和结构化分割指标，回答医生端用户的追问。\n"
        "要求：\n"
        "1. 只能基于已有报告和输入指标作答，不要编造症状、实验室、生化、遗传、病理或治疗信息。\n"
        "2. 如果输入中没有足够信息，直接说明“当前资料不足以判断”。\n"
        "3. 使用中文，回答要简洁、可复核，必要时说明依据来自哪个分割/指标。\n"
        "4. 不要给出最终诊断或治疗决策，需提示结合原始 CT 和临床资料由医生判断。\n\n"
        "已有 AI 报告：\n"
        f"{clip_text(ai_report_text, report_limit)}\n\n"
        "结构化指标摘要：\n"
        f"{json.dumps(context_for_prompt, ensure_ascii=False, indent=2)}\n\n"
        "最近对话：\n"
        f"{history_text or '无'}\n\n"
        "本轮问题：\n"
        f"{question.strip()}"
    )


def extract_response_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]

    chunks: list[str] = []
    for item in payload.get("output", []) or []:
        for content in item.get("content", []) or []:
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks).strip()


def extract_stream_delta(payload: dict[str, Any]) -> str:
    if payload.get("type") == "response.output_text.delta":
        delta = payload.get("delta")
        return delta if isinstance(delta, str) else ""

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        delta = choices[0].get("delta", {})
        content = delta.get("content")
        return content if isinstance(content, str) else ""

    return ""


def find_answer_stop_marker_index(text: str) -> int:
    indexes = [str(text or "").find(marker) for marker in ANSWER_STOP_MARKERS]
    indexes = [index for index in indexes if index >= 0]
    return min(indexes) if indexes else -1


def sanitize_ai_answer_text(text: str) -> str:
    clean = str(text or "").strip()
    stop_index = find_answer_stop_marker_index(clean)
    if stop_index >= 0:
        clean = clean[:stop_index].strip()

    changed = True
    while changed:
        changed = False
        clean = clean.rstrip()
        new_clean = TRAILING_ROLE_LABEL_RE.sub("", clean).rstrip()
        if new_clean != clean:
            clean = new_clean
            changed = True
        for label in TRAILING_ROLE_LABELS:
            for suffix in (f"\n\n{label}", f"\n{label}"):
                if clean.endswith(suffix):
                    clean = clean[: -len(suffix)].rstrip()
                    changed = True
            if clean == label:
                clean = ""
                changed = True
    return clean


def extract_chat_completion_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message", {})
    content = message.get("content")
    return content.strip() if isinstance(content, str) else ""


def ollama_native_base_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def is_ollama_endpoint(base_url: str) -> bool:
    return "11434" in base_url


def ollama_keep_alive_value() -> int | str:
    value = os.environ.get("OLLAMA_KEEP_ALIVE", os.environ.get("PPGL_OLLAMA_KEEP_ALIVE", "-1")).strip() or "-1"
    try:
        return int(value)
    except ValueError:
        return value


def ollama_chat_messages(prompt: str, system_text: str | None = None) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": system_text or CHAT_SYSTEM_TEXT,
        },
        {"role": "user", "content": prompt},
    ]


def call_ollama_chat_text(
    prompt: str,
    max_output_tokens: int,
    system_text: str | None = None,
    temperature: float = 0.2,
    scope: str = "chat",
) -> tuple[str, dict[str, Any]]:
    model = model_for_scope(scope)
    base_url = ollama_native_base_url(base_url_for_scope(scope))
    timeout = timeout_for_scope(scope)
    body: dict[str, Any] = {
        "model": model,
        "messages": ollama_chat_messages(prompt, system_text),
        "stream": False,
        "think": False,
        "keep_alive": ollama_keep_alive_value(),
        "options": {
            "num_predict": max_output_tokens,
            "temperature": temperature,
        },
    }

    request = urllib.request.Request(
        base_url + "/api/chat",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Ollama API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama API network error: {exc}") from exc

    payload = json.loads(raw)
    text = sanitize_ai_answer_text(payload.get("message", {}).get("content", ""))
    if not text:
        raise RuntimeError("Ollama API returned an empty answer")
    return text, {
        "provider": "ollama",
        "model": model,
        "response_id": "",
    }


def extract_json_object(text: str) -> dict[str, Any]:
    clean = str(text or "").strip()
    if clean.startswith("```"):
        clean = clean.strip("`").strip()
        if clean.startswith("json"):
            clean = clean[4:].strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        start = clean.find("{")
        end = clean.rfind("}")
        if start >= 0 and end > start:
            return json.loads(clean[start : end + 1])
        raise


def call_chat_completions_text(
    prompt: str,
    max_output_tokens: int = 1600,
    system_text: str | None = None,
    temperature: float | None = None,
    scope: str = "chat",
) -> tuple[str, dict[str, Any]]:
    api_key = api_key_for_scope(scope)
    model = model_for_scope(scope)
    base_url = base_url_for_scope(scope)
    timeout = timeout_for_scope(scope)
    max_output_tokens = chat_output_token_limit(max_output_tokens) if scope == "chat" else max_output_tokens

    if temperature is None:
        temperature = float(os.environ.get("OPENAI_TEMPERATURE", "0.2"))

    if is_ollama_endpoint(base_url):
        return call_ollama_chat_text(
            prompt,
            max_output_tokens=max_output_tokens,
            system_text=system_text,
            temperature=temperature,
            scope=scope,
        )

    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_text or CHAT_SYSTEM_TEXT,
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_output_tokens,
        "temperature": temperature,
    }
    request = urllib.request.Request(
        base_url + OPENAI_CHAT_COMPLETIONS_PATH,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=auth_headers(api_key),
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenAI-compatible API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI-compatible API network error: {exc}") from exc

    response_payload = json.loads(raw)
    text = sanitize_ai_answer_text(extract_chat_completion_text(response_payload))
    if not text:
        raise RuntimeError("OpenAI-compatible API returned an empty answer")
    metadata = {
        "provider": provider_for_scope(scope),
        "model": model,
        "response_id": response_payload.get("id", ""),
    }
    return text, metadata


def stream_ollama_chat_text(prompt: str, max_output_tokens: int = 1600):
    model = model_for_scope("chat")
    base_url = ollama_native_base_url(base_url_for_scope("chat"))
    timeout = timeout_for_scope("chat")
    body: dict[str, Any] = {
        "model": model,
        "messages": ollama_chat_messages(prompt),
        "stream": True,
        "think": False,
        "keep_alive": ollama_keep_alive_value(),
        "options": {
            "num_predict": max_output_tokens,
            "temperature": float(os.environ.get("OPENAI_TEMPERATURE", "0.2")),
        },
    }

    request = urllib.request.Request(
        base_url + "/api/chat",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    generated = ""
    sent = ""
    stopped = False

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue

                delta = payload.get("message", {}).get("content", "")
                if not isinstance(delta, str) or not delta:
                    if payload.get("done"):
                        break
                    continue

                generated += delta
                stop_index = find_answer_stop_marker_index(generated)
                if stop_index >= 0:
                    stopped = True

                visible = generated[:stop_index] if stopped else generated
                if stopped:
                    visible_to_send = sanitize_ai_answer_text(visible)
                else:
                    visible_to_send = visible[:-STREAM_HOLD_BACK_CHARS] if len(visible) > STREAM_HOLD_BACK_CHARS else ""

                if len(visible_to_send) > len(sent):
                    yield visible_to_send[len(sent) :]
                    sent = visible_to_send
                if stopped:
                    break

        final_text = sanitize_ai_answer_text(generated)
        if len(final_text) > len(sent):
            yield final_text[len(sent) :]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Ollama API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama API network error: {exc}") from exc


def stream_chat_completions_text(prompt: str, max_output_tokens: int = 1600):
    api_key = api_key_for_scope("chat")
    model = model_for_scope("chat")
    base_url = base_url_for_scope("chat")
    timeout = timeout_for_scope("chat")
    max_output_tokens = chat_output_token_limit(max_output_tokens)

    if is_ollama_endpoint(base_url):
        yield from stream_ollama_chat_text(prompt, max_output_tokens)
        return

    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": CHAT_SYSTEM_TEXT,
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_output_tokens,
        "temperature": float(os.environ.get("OPENAI_TEMPERATURE", "0.2")),
        "stream": True,
    }
    request = urllib.request.Request(
        base_url + OPENAI_CHAT_COMPLETIONS_PATH,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=auth_headers(api_key),
    )

    generated = ""
    sent = ""
    stopped = False

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue

                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue

                delta = extract_stream_delta(payload)
                if not delta:
                    continue

                generated += delta
                stop_index = find_answer_stop_marker_index(generated)
                if stop_index >= 0:
                    stopped = True

                visible = generated[:stop_index] if stopped else generated
                if stopped:
                    visible_to_send = sanitize_ai_answer_text(visible)
                else:
                    visible_to_send = visible[:-STREAM_HOLD_BACK_CHARS] if len(visible) > STREAM_HOLD_BACK_CHARS else ""

                if len(visible_to_send) > len(sent):
                    yield visible_to_send[len(sent) :]
                    sent = visible_to_send
                if stopped:
                    break

        final_text = sanitize_ai_answer_text(generated)
        if len(final_text) > len(sent):
            yield final_text[len(sent) :]
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenAI-compatible API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI-compatible API network error: {exc}") from exc


def call_chat_completions_report(context: dict[str, Any]) -> dict[str, Any]:
    prompt = build_minicpm_report_prompt(context) if is_minicpm_runtime("report") else (
        build_prompt(context)
        + "\n\n请只输出一个 UTF-8 JSON 对象，不要输出 Markdown 代码围栏或额外解释。"
    )
    text, metadata = call_chat_completions_text(
        prompt,
        max_output_tokens=report_output_token_limit(),
        system_text=(
            "你是一个医学影像术前辅助分析 Agent。"
            "你的输出用于医生端复核，必须谨慎、结构化、可追溯，不能替代临床诊断。"
            "本轮必须只输出 JSON 对象。"
        ),
        temperature=0.0,
        scope="report",
    )
    report = extract_json_object(text)
    report["_metadata"] = {
        **metadata,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return report


def call_openai_report(context: dict[str, Any]) -> dict[str, Any]:
    if use_chat_completions_api(report_llm_provider()):
        return call_chat_completions_report(context)

    api_key = api_key_for_scope("report")
    if not api_key or api_key == "EMPTY":
        raise RuntimeError("REPORT_OPENAI_API_KEY is not set")

    model = model_for_scope("report")
    reasoning_effort = reasoning_effort_for_scope("report")
    base_url = base_url_for_scope("report")
    timeout = timeout_for_scope("report")

    body = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "你是一个医学影像术前辅助分析 Agent。"
                            "你的输出用于医生端复核，必须谨慎、结构化、可追溯，不能替代临床诊断。"
                        ),
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": build_prompt(context)}],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "ppgl_ai_report",
                "strict": True,
                "schema": REPORT_SCHEMA,
            }
        },
        "max_output_tokens": 3000,
    }
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}

    request = urllib.request.Request(
        base_url + OPENAI_RESPONSES_PATH,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"LLM API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM API network error: {exc}") from exc

    response_payload = json.loads(raw)
    text = extract_response_text(response_payload)
    if not text:
        raise RuntimeError("LLM API returned an empty report")

    report = json.loads(text)
    report["_metadata"] = {
        "provider": report_llm_provider(),
        "model": model,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "response_id": response_payload.get("id", ""),
    }
    return report


def call_openai_text(prompt: str, max_output_tokens: int = 1600) -> tuple[str, dict[str, Any]]:
    max_output_tokens = chat_output_token_limit(max_output_tokens)
    if use_chat_completions_api(llm_provider()):
        return call_chat_completions_text(prompt, max_output_tokens)

    api_key = api_key_for_scope("chat")
    if not api_key:
        raise RuntimeError("CHAT_OPENAI_API_KEY is not set")

    model = model_for_scope("chat")
    reasoning_effort = reasoning_effort_for_scope("chat")
    base_url = base_url_for_scope("chat")
    timeout = timeout_for_scope("chat")

    body: dict[str, Any] = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": CHAT_SYSTEM_TEXT,
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        ],
        "max_output_tokens": max_output_tokens,
    }
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}

    request = urllib.request.Request(
        base_url + OPENAI_RESPONSES_PATH,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"LLM API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM API network error: {exc}") from exc

    response_payload = json.loads(raw)
    text = extract_response_text(response_payload)
    if not text:
        raise RuntimeError("LLM API returned an empty answer")
    metadata = {
        "provider": llm_provider(),
        "model": model,
        "response_id": response_payload.get("id", ""),
    }
    return text, metadata


def stream_openai_text(prompt: str, max_output_tokens: int = 1600):
    max_output_tokens = chat_output_token_limit(max_output_tokens)
    if use_chat_completions_api(llm_provider()):
        yield from stream_chat_completions_text(prompt, max_output_tokens)
        return

    api_key = api_key_for_scope("chat")
    if not api_key:
        raise RuntimeError("CHAT_OPENAI_API_KEY is not set")

    model = model_for_scope("chat")
    reasoning_effort = reasoning_effort_for_scope("chat")
    base_url = base_url_for_scope("chat")
    timeout = timeout_for_scope("chat")

    body: dict[str, Any] = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": CHAT_SYSTEM_TEXT,
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        ],
        "max_output_tokens": max_output_tokens,
        "stream": True,
    }
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}

    request = urllib.request.Request(
        base_url + OPENAI_RESPONSES_PATH,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue

                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue

                delta = extract_stream_delta(payload)
                if delta:
                    yield delta
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"LLM API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM API network error: {exc}") from exc


def generate_ai_report(case_dir: Path) -> dict[str, Any]:
    context = compact_case_context(case_dir)
    raw_report = call_openai_report(context)
    report = normalize_generated_report(raw_report, context)
    report["report_markdown"] = render_report_markdown(report)

    output_dir = case_dir / "output"
    json_path = output_dir / "ai_report.json"
    markdown_path = output_dir / "ai_report.md"
    report["ai_report_json_path"] = str(json_path)
    report["ai_report_markdown_path"] = str(markdown_path)

    write_json(json_path, report)
    markdown_path.write_text(report.get("report_markdown", ""), encoding="utf-8")

    result_path = output_dir / "result.json"
    if result_path.exists():
        result = read_json(result_path)
        result.setdefault("outputs", {})
        result["outputs"]["ai_report_json_path"] = str(json_path)
        result["outputs"]["ai_report_markdown_path"] = str(markdown_path)
        write_json(result_path, result)

    return report


def build_ai_report_chat_request(
    case_dir: Path,
    question: str,
    history: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    clean_question = str(question or "").strip()
    if not clean_question:
        raise ValueError("question is empty")

    context = compact_case_context(case_dir)
    if is_minicpm_runtime("chat"):
        prompt = build_minicpm_chat_prompt(
            context,
            load_ai_report_summary(case_dir),
            clean_question,
            history or [],
        )
        return clean_question, prompt

    ai_report_text = load_ai_report_text(case_dir)
    prompt = build_chat_prompt(context, ai_report_text, clean_question, history or [])
    return clean_question, prompt


def direct_ai_report_answer(
    case_dir: Path,
    question: str,
) -> tuple[str, str, dict[str, Any]] | None:
    clean_question = str(question or "").strip()
    if not clean_question:
        return None

    lower_question = clean_question.lower()

    time_intent = any(
        keyword in clean_question
        for keyword in (
            "现在几点",
            "几点了",
            "现在是几点",
            "当前时间",
            "现在时间",
            "今天几号",
            "今天日期",
            "现在日期",
        )
    )
    if time_intent:
        answer = f"当前服务器时间是 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}。"
        metadata = {
            "provider": llm_provider(),
            "model": "structured-basic-rule",
            "intent": "current_time",
            "source": "runtime_clock",
        }
        return clean_question, answer, metadata

    assistant_identity_intent = any(
        keyword in clean_question
        for keyword in (
            "你是谁",
            "你是干什么的",
            "你能做什么",
            "你可以做什么",
            "你有什么用",
        )
    )
    if assistant_identity_intent:
        answer = (
            "我是 PPGL 术前影像辅助问答助手，可以基于当前病例的自动分割结果、"
            "肿瘤指标、器官毗邻关系和 AI 报告回答影像相关问题；我的回答仅供医生辅助参考，不能替代临床诊断。"
        )
        metadata = {
            "provider": llm_provider(),
            "model": "structured-basic-rule",
            "intent": "assistant_identity",
            "source": "runtime_config",
        }
        return clean_question, answer, metadata

    ppgl_definition_intent = "ppgl" in lower_question and any(
        keyword in clean_question for keyword in ("是什么", "什么意思", "知道", "介绍", "解释")
    )
    if ppgl_definition_intent:
        answer = (
            "PPGL 是 pheochromocytoma/paraganglioma 的缩写，中文通常指嗜铬细胞瘤和副神经节瘤。"
            "它们来源于肾上腺髓质或肾上腺外副神经节相关组织，部分病例可分泌儿茶酚胺，"
            "影像评估重点包括病灶位置、大小、与大血管和邻近器官的关系；最终诊断仍需结合临床、生化、病理等资料。"
        )
        metadata = {
            "provider": llm_provider(),
            "model": "structured-basic-rule",
            "intent": "ppgl_definition",
            "source": "medical_knowledge",
        }
        return clean_question, answer, metadata

    greeting_intent = clean_question in {"你好", "您好", "在吗", "hi", "hello", "Hi", "Hello"}
    if greeting_intent:
        answer = "你好，我可以基于当前病例的 AI 报告和结构化分割指标回答影像相关问题。"
        metadata = {
            "provider": llm_provider(),
            "model": "structured-basic-rule",
            "intent": "greeting",
            "source": "runtime_config",
        }
        return clean_question, answer, metadata

    model_identity_intent = any(
        keyword in clean_question
        for keyword in (
            "你是什么模型",
            "是什么模型",
            "当前问答模型",
            "问答模型是谁",
            "本地模型",
            "1b",
            "MiniCPM",
            "minicpm",
        )
    )
    if model_identity_intent:
        answer = (
            f"当前 AI 问答调用的是 {model_for_scope('chat')}，接口地址是 {base_url_for_scope('chat')}；"
            f"结构化 AI 报告调用的是 {model_for_scope('report')}，接口地址是 {base_url_for_scope('report')}。"
        )
        metadata = {
            "provider": llm_provider(),
            "model": "structured-runtime-rule",
            "intent": "model_identity",
            "source": "runtime_config",
        }
        return clean_question, answer, metadata

    context = compact_case_context(case_dir)
    metrics = context.get("selected_metrics", {}) or {}
    risk = context.get("risk_assessment", {}) or (context.get("llm_context", {}) or {}).get("risk_assessment", {}) or {}
    relations = metrics.get("organ_relations", {}) or {}
    components = metrics.get("components", []) or []

    def numeric_value(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def format_number(value: float, decimals: int = 1) -> str:
        return f"{value:.{decimals}f}".rstrip("0").rstrip(".")

    main_reason_intent = any(
        keyword in clean_question
        for keyword in (
            "主要原因",
            "为什么",
            "原因",
            "依据",
            "怎么判断",
            "凭什么",
            "最大风险",
            "最大的风险",
            "最高风险",
            "核心风险",
            "最大的问题",
            "最主要风险",
        )
    )
    other_risk_intent = any(
        keyword in clean_question
        for keyword in ("还有什么风险", "还有哪些风险", "其他风险", "别的风险", "风险点", "还要注意", "还需要关注")
    )
    severity_intent = any(
        keyword in clean_question
        for keyword in ("严重吗", "严不严重", "风险高吗", "风险大吗", "危险吗", "重吗", "高吗", "高风险吗", "是不是高风险")
    )
    tumor_size_intent = any(
        keyword in clean_question
        for keyword in ("体积", "容积", "大小", "多大", "最大径", "直径", "尺寸", "长宽高", "长宽")
    ) and any(keyword in clean_question for keyword in ("肿瘤", "病灶", "瘤"))
    if not (main_reason_intent or other_risk_intent or severity_intent or tumor_size_intent):
        return None

    volume = numeric_value(metrics.get("tumor_volume_ml"))
    component_count = numeric_value(metrics.get("tumor_component_count"))
    tumor_data_available = volume is not None or component_count is not None or bool(components) or bool(relations)
    if not tumor_data_available:
        answer = (
            "当前病例只有全器官分割，没有可用的肿瘤分割或病灶量化数据，"
            "因此无法判断病灶大小、解剖风险或严重程度。请先完成肿瘤分割，并由医生结合原始 CT 复核。"
        )
        metadata = {
            "provider": llm_provider(),
            "model": "structured-risk-rule",
            "intent": "insufficient_tumor_data",
            "source": "clinical_metrics",
        }
        return clean_question, answer, metadata

    bbox_size = components[0].get("bbox_size_mm") if components and isinstance(components[0], dict) else None
    numeric_bbox = [numeric_value(value) for value in bbox_size] if isinstance(bbox_size, list) else []
    numeric_bbox = [value for value in numeric_bbox if value is not None]
    max_diameter = max(numeric_bbox) if numeric_bbox else None

    relation_labels = {
        "adrenal_gland_left": "左肾上腺",
        "adrenal_gland_right": "右肾上腺",
        "aorta": "主动脉",
        "inferior_vena_cava": "下腔静脉",
        "kidney_left": "左肾",
        "kidney_right": "右肾",
        "liver": "肝脏",
        "pancreas": "胰腺",
        "portal_vein_and_splenic_vein": "门静脉及脾静脉",
        "spleen": "脾脏",
    }
    ranked_relations = []
    for relation_key, relation_value in relations.items():
        if not isinstance(relation_value, dict):
            continue
        distance = numeric_value(relation_value.get("min_surface_distance_mm"))
        overlap = numeric_value(relation_value.get("overlap_voxels"))
        contact = bool(relation_value.get("contact_or_overlap")) or bool(overlap and overlap > 0)
        if distance is None and overlap is None and not contact:
            continue
        ranked_relations.append(
            (
                0 if contact else 1,
                distance if distance is not None else float("inf"),
                str(relation_key),
                relation_value,
                distance,
                overlap,
            )
        )
    ranked_relations.sort(key=lambda item: item[:3])

    relation_statements = []
    for _, _, relation_key, relation_value, distance, overlap in ranked_relations[:3]:
        label = relation_labels.get(relation_key) or relation_value.get("name") or relation_key
        details = []
        if distance is not None:
            details.append(f"最小表面距离约 {format_number(distance)} mm")
        if overlap is not None and overlap > 0:
            details.append(f"重叠体素约 {format_number(overlap, 0)} 个")
        if details:
            relation_statements.append(f"病灶与{label}{'，'.join(details)}")

    side_value = str(metrics.get("tumor_side_by_nearest_kidney") or "").strip().lower()
    side_text = {
        "left": "左侧",
        "right": "右侧",
        "左": "左侧",
        "右": "右侧",
        "左侧": "左侧",
        "右侧": "右侧",
    }.get(side_value)

    level = str(risk.get("overall_level") or "").strip().lower()
    level_text = {
        "high": "高风险",
        "moderate": "中等风险",
        "low": "低风险",
        "uncertain": "风险不确定",
    }.get(level)

    size_statements = []
    if volume is not None:
        size_statements.append(f"肿瘤体积约 {format_number(volume, 2)} ml")
    if component_count is not None:
        size_statements.append(f"肿瘤组件数为 {format_number(component_count, 0)} 个")
    if numeric_bbox:
        size_statements.append("包围盒尺寸约 " + " × ".join(format_number(value) for value in numeric_bbox) + " mm")
    if max_diameter is not None:
        size_statements.append(f"最大径约 {format_number(max_diameter)} mm")
    if side_text:
        size_statements.append(f"病灶更接近{side_text}肾脏")

    if tumor_size_intent:
        if size_statements:
            answer = "按当前肿瘤分割结果，" + "，".join(size_statements) + "。该数值仍需结合原始 CT 由医生复核。"
        else:
            answer = "当前已有肿瘤相关结果，但没有可用的体积或尺寸数值，无法回答病灶大小。"
        intent = "tumor_size_metric"
    elif main_reason_intent:
        if level_text:
            answer = f"当前结构化风险分层为{level_text}。"
        else:
            answer = "当前结构化数据没有提供有效的总体风险分层，不能判断属于高、中或低风险。"
        if relation_statements:
            answer += "可核对的主要解剖依据包括：" + "；".join(relation_statements) + "。"
        else:
            answer += "当前也没有可用的病灶毗邻距离或重叠数据。"
        answer += "风险结论需结合原始 CT、增强期影像和临床资料由医生复核。"
        intent = "main_risk_reasons"
    elif other_risk_intent:
        if relation_statements:
            answer = "当前可量化的其他解剖关注点包括：" + "；".join(relation_statements) + "。"
        else:
            answer = "当前结构化数据没有提供更多可量化的病灶毗邻关系，无法补充其他风险点。"
        answer += "这些指标不能单独判断血管侵犯、良恶性或最终手术风险，需由医生复核原始影像。"
        intent = "additional_risk_points"
    else:
        if not level_text:
            answer = "当前结构化数据没有提供有效的总体风险分层，因此无法判断风险高低。"
        else:
            answer = f"按当前结构化影像指标，这个病例的风险分层为{level_text}。"
        if relation_statements:
            answer += "主要解剖依据包括：" + "；".join(relation_statements) + "。"
        if size_statements:
            answer += "病灶量化信息为：" + "，".join(size_statements) + "。"
        answer += "这里的风险仅指影像解剖和手术复杂度风险，不等同于恶性或生存期判断，需由医生结合原始 CT 和临床资料复核。"
        intent = "risk_level"
    metadata = {
        "provider": llm_provider(),
        "model": "structured-risk-rule",
        "intent": intent,
        "source": "clinical_metrics",
    }
    return clean_question, answer, metadata


def save_ai_report_chat(
    case_dir: Path,
    question: str,
    answer: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    answer = sanitize_ai_answer_text(answer)
    output_dir = case_dir / "output"
    chat_path = output_dir / "ai_report_chat.json"
    if chat_path.exists():
        chat = read_json(chat_path)
    else:
        chat = {
            "case_id": case_dir.name,
            "messages": [],
        }

    now = datetime.now().isoformat(timespec="seconds")
    chat.setdefault("messages", [])
    chat["messages"].append(
        {
            "role": "user",
            "content": question,
            "created_at": now,
        }
    )
    chat["messages"].append(
        {
            "role": "assistant",
            "content": answer,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "metadata": metadata or {},
        }
    )
    chat["updated_at"] = datetime.now().isoformat(timespec="seconds")
    chat["chat_path"] = str(chat_path)
    write_json(chat_path, chat)
    return chat


def chat_with_ai_report(
    case_dir: Path,
    question: str,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    direct = direct_ai_report_answer(case_dir, question)
    if direct is not None:
        clean_question, answer, metadata = direct
        return save_ai_report_chat(case_dir, clean_question, answer, metadata)

    clean_question, prompt = build_ai_report_chat_request(case_dir, question, history)
    answer, metadata = call_openai_text(prompt)
    return save_ai_report_chat(case_dir, clean_question, answer, metadata)
