from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .models import CaseType
from .storage import UnifiedCaseStorage


REPORT_SCHEMA_VERSION = "2.0"
REPORT_TEMPLATE_VERSION = "2.0.0"


class UnifiedReportError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UnifiedReportError(f"无法读取报告来源：{path.name}") from exc
    return value if isinstance(value, dict) else {}


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def _find(payload: Any, *names: str) -> Any:
    wanted = {name.casefold() for name in names}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).casefold() in wanted and value not in (None, ""):
                return value
        for value in payload.values():
            found = _find(value, *names)
            if found not in (None, ""):
                return found
    if isinstance(payload, list):
        for value in payload:
            found = _find(value, *names)
            if found not in (None, ""):
                return found
    return None


def _number(value: Any, digits: int = 3) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "未生成"


def _font(size: int) -> ImageFont.ImageFont:
    candidates = []
    if os.environ.get("WINDIR"):
        fonts = Path(os.environ["WINDIR"]) / "Fonts"
        candidates.extend((fonts / "msyh.ttc", fonts / "simhei.ttf", fonts / "simsun.ttc"))
    candidates.extend((Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")))
    path = next((item for item in candidates if item.is_file()), None)
    return ImageFont.truetype(str(path), size=size) if path else ImageFont.load_default()


def _render_pdf(markdown: str, output: Path) -> None:
    width, height, margin = 1240, 1754, 88
    body, heading, title = _font(25), _font(32), _font(42)
    pages: list[Image.Image] = []
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    y = margin

    def new_page() -> None:
        nonlocal image, draw, y
        pages.append(image)
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        y = margin

    for raw in markdown.splitlines():
        font = title if raw.startswith("# ") else heading if raw.startswith("## ") else body
        text = raw.lstrip("# ")
        line_height = int(getattr(font, "size", 25) * 1.55)
        wrapped, current = [], ""
        for character in text or " ":
            candidate = current + character
            if current and draw.textbbox((0, 0), candidate, font=font)[2] > width - 2 * margin:
                wrapped.append(current)
                current = character
            else:
                current = candidate
        wrapped.append(current)
        if y + len(wrapped) * line_height + 10 > height - margin:
            new_page()
        for line in wrapped:
            draw.text((margin, y), line, fill="#111827", font=font)
            y += line_height
        y += 8
    pages.append(image)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        pages[0].save(temporary, format="PDF", resolution=150, save_all=True, append_images=pages[1:])
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()


class UnifiedReportService:
    """Generate reports from deterministic result files; the LLM never calculates metrics."""

    def __init__(self, storage: UnifiedCaseStorage):
        self.storage = storage

    def _root(self, case_id: str, case_type: str) -> Path:
        return self.storage.case_root(case_id, case_type)

    def _source(self, case_id: str, case_type: str) -> tuple[dict[str, Any], list[str]]:
        root = self._root(case_id, case_type)
        if case_type == CaseType.BRAIN_MRI.value:
            candidates = (root / "output" / "metrics.json",)
        else:
            candidates = (
                root / "output" / "ppgl" / "metrics.json",
                root / "output" / "clinical_metrics.json",
                root / "output" / "ppgl" / "result.json",
                root / "output" / "result.json",
            )
        existing = [path for path in candidates if path.is_file()]
        if not existing:
            raise UnifiedReportError("必须先完成分割并生成确定性指标")
        merged: dict[str, Any] = {}
        for path in reversed(existing):
            merged[path.relative_to(root).as_posix()] = _read_json(path)
        return merged, list(merged)

    def _mri_sections(self, source: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        values = {
            "edema_ml": _find(source, "edema_volume_ml", "ed_volume_ml"),
            "net_ml": _find(source, "net_volume_ml"),
            "et_ml": _find(source, "et_volume_ml"),
            "tc_ml": _find(source, "tc_volume_ml"),
            "wt_ml": _find(source, "wt_volume_ml"),
            "maximum_3d_diameter_mm": _find(source, "maximum_3d_diameter_mm", "max_3d_diameter_mm"),
            "lesion_count": _find(source, "lesion_component_count", "lesion_count"),
        }
        # Current metrics.json stores most values as nested {value} or {volume_ml} records.
        regions = _find(source, "regions") or {}
        for key, aliases in {"edema_ml": ("edema", "ed"), "net_ml": ("net",), "et_ml": ("et",), "tc_ml": ("tc",), "wt_ml": ("wt",)}.items():
            if values[key] is None and isinstance(regions, dict):
                region = next((regions.get(alias) for alias in aliases if isinstance(regions.get(alias), dict)), {})
                values[key] = region.get("volume_ml")
        measurements = _find(source, "measurements") or {}
        if values["maximum_3d_diameter_mm"] is None and isinstance(measurements, dict):
            item = measurements.get("maximum_3d_diameter") or {}
            values["maximum_3d_diameter_mm"] = item.get("value") if isinstance(item, dict) else item
        if values["lesion_count"] is None and isinstance(measurements, dict):
            item = measurements.get("lesion_component_count") or {}
            values["lesion_count"] = item.get("value") if isinstance(item, dict) else item
        lines = [
            f"- 水肿 ED 体积：{_number(values['edema_ml'])} mL",
            f"- 非增强肿瘤 NET 体积：{_number(values['net_ml'])} mL",
            f"- 增强肿瘤 ET 体积：{_number(values['et_ml'])} mL",
            f"- 肿瘤核心 TC 体积：{_number(values['tc_ml'])} mL",
            f"- 全肿瘤 WT 体积：{_number(values['wt_ml'])} mL",
            f"- WT 最大三维径：{_number(values['maximum_3d_diameter_mm'])} mm",
            f"- 病灶组件数：{_number(values['lesion_count'], 0)}",
        ]
        return lines, values

    def _ct_sections(self, source: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        values = {
            # A generic ``volume_ml`` may belong to any segmented organ.
            "tumor_volume_ml": _find(source, "tumor_volume_ml", "ppgl_volume_ml", "lesion_volume_ml"),
            "maximum_diameter_mm": _find(source, "maximum_3d_diameter_mm", "max_diameter_mm", "maximum_diameter_mm"),
            "side": _find(source, "tumor_side_by_nearest_kidney", "side", "laterality"),
            "centroid_mm": _find(source, "centroid_mm", "tumor_centroid_mm", "center_mm"),
            "organ_relations": _find(source, "organ_relations", "important_organ_relations", "nearest_anatomic_structures"),
            "organ_count": _find(source, "organ_count", "segmented_organ_count", "label_count"),
        }
        lines = [
            f"- PPGL 自动分割体积：{_number(values['tumor_volume_ml'])} mL",
            f"- PPGL 最大径：{_number(values['maximum_diameter_mm'])} mm",
            f"- 侧别/空间位置：{values['side'] or '未生成'}",
            f"- 质心坐标（mm）：{json.dumps(values['centroid_mm'], ensure_ascii=False) if values['centroid_mm'] is not None else '未生成'}",
            f"- TotalSegmentator 解剖结构数量：{_number(values['organ_count'], 0)}",
            f"- 邻近器官关系：{json.dumps(values['organ_relations'], ensure_ascii=False) if values['organ_relations'] is not None else '未生成'}",
        ]
        return lines, values

    def generate(self, case_id: str, case_type: str, *, model_versions: dict[str, str]) -> dict[str, Any]:
        source, source_files = self._source(case_id, case_type)
        generated_at = datetime.now(timezone.utc).isoformat()
        is_mri = case_type == CaseType.BRAIN_MRI.value
        findings, deterministic = self._mri_sections(source) if is_mri else self._ct_sections(source)
        imaging = "脑肿瘤四序列 MRI" if is_mri else "PPGL CT"
        inputs = ["FLAIR", "T1", "T1CE", "T2"] if is_mri else ["CT NIfTI"]
        method = "nnU-Net 脑肿瘤自动分割" if is_mri else "TotalSegmentator 解剖结构 + ProgressPatchV5 PPGL 自动分割"
        limitations = [
            "自动分割可能受扫描参数、伪影、配准误差和数据分布差异影响。",
            "所有边界、数值、空间关系和可视化结果必须由医生结合完整原始影像复核。",
            "本报告不能自动判断良恶性、WHO 分级、病理类型、分期、预后或确定治疗方案。",
        ]
        markdown = "\n".join([
            f"# {imaging} 定量影像辅助报告", "",
            f"- 病例内部编号：`{case_id}`", f"- 影像类型：{imaging}",
            f"- 输入：{'、'.join(inputs)}", f"- 分割方法：{method}",
            f"- 模型版本：{json.dumps(model_versions, ensure_ascii=False)}", f"- 生成时间：{generated_at}",
            "- 医疗用途：AI 定量影像辅助，必须由医生复核，不构成诊断或治疗结论", "",
            "## 自动分割与确定性定量结果", "", *findings, "",
            "## 规则化影像说明", "",
            "以上数值由确定性程序从分割标签计算，大模型不得重新计算或修改这些数值。", "",
            "## AI 辅助文本", "", "尚未生成。AI 仅可解释影像负荷、提示复核重点、建议补充资料和就诊科室路径。", "",
            "## 局限性", "", *(f"- {item}" for item in limitations), "",
            "## 医生复核", "", "医生复核意见、复核医生和日期保存在独立审计记录中。", "",
        ])
        root = self._root(case_id, case_type)
        output = root / "output"
        _atomic_text(output / "report.md", markdown)
        pdf_generated = True
        try:
            _render_pdf(markdown, output / "report.pdf")
        except Exception:
            pdf_generated = False
        payload = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "template_version": REPORT_TEMPLATE_VERSION,
            "case_id": case_id,
            "case_type": case_type,
            "imaging_type": imaging,
            "inputs": inputs,
            "model_versions": model_versions,
            "generated_at": generated_at,
            "source_files": source_files,
            "sections": {
                "deterministic_metrics": deterministic,
                "rule_explanation": "数值由确定性服务计算，LLM 不参与计算。",
                "ai_assistance": None,
                "limitations": limitations,
            },
            "files": {"markdown": "report.md", "json": "report.json", "pdf": "report.pdf" if pdf_generated else None},
            "medical_review_required": True,
            "diagnostic_use": False,
        }
        _atomic_json(output / "report.json", payload)
        return {**payload, "report_markdown": markdown}

    def require(self, case_id: str, case_type: str) -> dict[str, Any]:
        root = self._root(case_id, case_type)
        metadata, markdown = root / "output" / "report.json", root / "output" / "report.md"
        if not metadata.is_file() or not markdown.is_file():
            raise UnifiedReportError("辅助报告尚未生成")
        payload = _read_json(metadata)
        payload["report_markdown"] = markdown.read_text(encoding="utf-8")
        review = root / "output" / "doctor_review.json"
        payload["review"] = _read_json(review) if review.is_file() else None
        return payload

    def safe_context(self, case_id: str, case_type: str) -> dict[str, Any]:
        report = self.require(case_id, case_type)
        return {
            "case_type": report["case_type"],
            "imaging_type": report["imaging_type"],
            "inputs": report["inputs"],
            "model_versions": report["model_versions"],
            "deterministic_metrics": report["sections"]["deterministic_metrics"],
            "limitations": report["sections"]["limitations"],
        }

    def apply_review(self, case_id: str, case_type: str, review: dict[str, Any]) -> dict[str, Any]:
        """Persist a reviewed report consistently in JSON, Markdown and PDF."""
        report = self.require(case_id, case_type)
        markdown = report["report_markdown"]
        marker = "## 医生复核"
        if marker in markdown:
            markdown = markdown.split(marker, 1)[0].rstrip()
        markdown += (
            "\n\n## 医生复核\n\n"
            f"- 复核医生：{review['doctor_name']}\n"
            f"- 复核日期：{review['review_date']}\n"
            f"- 复核意见：{review['opinion']}\n"
        )
        report["review"] = review
        report["sections"]["doctor_review"] = {
            "opinion": review["opinion"],
            "doctor_name": review["doctor_name"],
            "review_date": review["review_date"],
        }
        report.pop("report_markdown", None)
        root = self._root(case_id, case_type) / "output"
        _atomic_text(root / "report.md", markdown)
        try:
            _render_pdf(markdown, root / "report.pdf")
            report["files"]["pdf"] = "report.pdf"
        except Exception:
            report["files"]["pdf"] = None
        _atomic_json(root / "report.json", report)
        return {**report, "report_markdown": markdown}

    def file(self, case_id: str, case_type: str, kind: str) -> Path:
        names = {"markdown": "report.md", "json": "report.json", "pdf": "report.pdf"}
        if kind not in names:
            raise UnifiedReportError("报告格式无效")
        path = self._root(case_id, case_type) / "output" / names[kind]
        if not path.is_file():
            raise UnifiedReportError("请求的报告文件尚未生成")
        return path
