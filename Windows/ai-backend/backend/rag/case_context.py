from __future__ import annotations

import json
from pathlib import Path
from typing import Any


BASE_ORGANS = {
    "adrenal_gland_left",
    "adrenal_gland_right",
    "aorta",
    "inferior_vena_cava",
    "kidney_left",
    "kidney_right",
    "liver",
    "pancreas",
    "portal_vein_and_splenic_vein",
    "spleen",
}

ORGAN_ALIASES = {
    "左肾上腺": "adrenal_gland_left",
    "右肾上腺": "adrenal_gland_right",
    "主动脉": "aorta",
    "下腔静脉": "inferior_vena_cava",
    "左肾": "kidney_left",
    "右肾": "kidney_right",
    "肝": "liver",
    "胰": "pancreas",
    "门静脉": "portal_vein_and_splenic_vein",
    "脾": "spleen",
    "aorta": "aorta",
    "inferior vena cava": "inferior_vena_cava",
    "adrenal": "adrenal_gland_left",
    "kidney": "kidney_left",
    "liver": "liver",
    "pancreas": "pancreas",
    "spleen": "spleen",
}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def optional_output_json(result: dict, key: str) -> dict:
    raw_path = str((result.get("outputs") or {}).get(key, "")).strip()
    if not raw_path:
        return {}
    path = Path(raw_path)
    return read_json(path) if path.is_file() else {}


def selected_organ_names(question: str, organs: dict) -> set[str]:
    selected = set(BASE_ORGANS)
    normalized_question = question.casefold()
    for alias, organ_name in ORGAN_ALIASES.items():
        if alias.casefold() in normalized_question:
            selected.add(organ_name)
            if organ_name.endswith("_left"):
                selected.add(organ_name[:-5] + "_right")
    return selected.intersection(organs)


def compact_organ_measurements(question: str, organs: dict) -> dict:
    selected = selected_organ_names(question, organs)
    measurements = {}
    for name in sorted(selected):
        value = organs.get(name) or {}
        measurements[name] = {
            "volume_ml": value.get("volume_ml"),
            "voxel_count": value.get("voxel_count"),
        }
    return measurements


def compact_relations(metrics: dict) -> list[dict]:
    relations = list((metrics.get("organ_relations") or {}).values())
    relations.sort(
        key=lambda item: (
            not bool(item.get("contact_or_overlap")),
            float(item.get("min_surface_distance_mm") or 999999),
        )
    )
    return [
        {
            "name": item.get("name"),
            "group": item.get("group"),
            "min_surface_distance_mm": item.get("min_surface_distance_mm"),
            "distance_category": item.get("distance_category"),
            "overlap_voxels": item.get("overlap_voxels"),
        }
        for item in relations[:12]
    ]


def load_case_context(case_dir: Path, question: str) -> dict:
    result_path = case_dir / "output" / "result.json"
    ppgl_result_path = case_dir / "output" / "ppgl" / "result.json"
    if not result_path.is_file() and not ppgl_result_path.is_file():
        raise FileNotFoundError("请先完成分割，再进行病例 RAG 问答")
    result = read_json(result_path) if result_path.is_file() else {}
    ppgl_result = read_json(ppgl_result_path) if ppgl_result_path.is_file() else {}
    metrics = result.get("clinical_metrics") or optional_output_json(result, "clinical_metrics_path")
    metrics = {**metrics, **(ppgl_result.get("metrics") or {})}
    llm_context = optional_output_json(result, "llm_context_path")
    summary = result.get("summary") or {}
    organs = summary.get("organs") or metrics.get("organs") or {}
    segmentation = result.get("segmentation") or {}

    tumor_volume = metrics.get("tumor_volume_ml")
    tumor_components = metrics.get("components") or []
    tumor_available = ppgl_result_path.is_file() or tumor_volume is not None or bool(tumor_components) or bool(segmentation.get("tumor_priority"))

    source = "TotalSegmentator 及 ProgressPatchV5 量化分析结果"
    if not result_path.is_file():
        source = "ProgressPatchV5 PPGL 量化分析结果"
    elif not ppgl_result_path.is_file():
        source = "TotalSegmentator 量化分析结果"

    return {
        "case_id": case_dir.name,
        "source": source,
        "segmentation": {
            "task": summary.get("task") or metrics.get("task"),
            "label_count": segmentation.get("label_count"),
            "organ_count": summary.get("organ_count") or metrics.get("organ_count"),
            "tumor_analysis_available": tumor_available,
        },
        "organ_measurements": compact_organ_measurements(question, organs),
        "tumor_measurements": {
            "volume_ml": tumor_volume,
            "max_diameter_mm": metrics.get("max_diameter_mm"),
            "component_count": metrics.get("tumor_component_count"),
            "side_by_nearest_kidney": metrics.get("tumor_side_by_nearest_kidney"),
            "components": tumor_components[:3],
            "anchor_distances_mm": metrics.get("anchor_distances_mm") or {},
            "nearest_relations": compact_relations(metrics),
        } if tumor_available else None,
        "risk_assessment": result.get("risk_assessment") or llm_context.get("risk_assessment") or None,
        "limitations": [
            "病例数值来自自动分割，必须结合原始 CT 由医生复核。",
            *([] if tumor_available else ["当前病例只有全器官分割，没有可用的肿瘤分割或病灶量化数据。"]),
        ],
    }
