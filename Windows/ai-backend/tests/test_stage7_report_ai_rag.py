from __future__ import annotations

import json
from pathlib import Path

from backend.enterprise.privacy import contains_sensitive_text, redact_text
from backend.enterprise.report_service import UnifiedReportService
from backend.rag.prompt_builder import build_knowledge_rag_prompt, citation_payload


class TestStorage:
    def __init__(self, root: Path):
        self.root = root

    def case_root(self, case_id: str, case_type: str) -> Path:
        del case_id, case_type
        return self.root


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_mri_unified_report_uses_deterministic_metrics(tmp_path: Path) -> None:
    write_json(tmp_path / "output" / "metrics.json", {
        "regions": {"ed": {"volume_ml": 3.1}, "net": {"volume_ml": 2.2},
                    "et": {"volume_ml": 1.1}, "tc": {"volume_ml": 3.3}, "wt": {"volume_ml": 6.4}},
        "measurements": {"maximum_3d_diameter": {"value": 31.2}, "lesion_component_count": {"value": 2}},
    })
    service = UnifiedReportService(TestStorage(tmp_path))
    result = service.generate("internal-case", "brain_mri", model_versions={"segmentation": "checkpoint_best.pth"})
    assert result["sections"]["deterministic_metrics"]["wt_ml"] == 6.4
    assert result["sections"]["ai_assistance"] is None
    assert (tmp_path / "output" / "report.json").is_file()
    assert "大模型不得重新计算" in result["report_markdown"]
    reviewed = service.apply_review("internal-case", "brain_mri", {
        "opinion": "建议结合原始序列复核边界。", "doctor_name": "测试医生", "review_date": "2026-08-29",
        "reviewer_user_id": "test-user", "updated_at": "2026-08-29T00:00:00+00:00",
    })
    assert reviewed["review"]["doctor_name"] == "测试医生"
    assert "建议结合原始序列复核边界" in (tmp_path / "output" / "report.md").read_text(encoding="utf-8")


def test_ct_report_never_uses_generic_organ_volume(tmp_path: Path) -> None:
    write_json(tmp_path / "output" / "result.json", {"organs": [{"name": "liver", "volume_ml": 1200}]})
    write_json(tmp_path / "output" / "ppgl" / "metrics.json", {
        "tumor_volume_ml": 18.5, "maximum_diameter_mm": 42.0,
        "side": "left", "organ_count": 117,
    })
    result = UnifiedReportService(TestStorage(tmp_path)).generate(
        "internal-case", "ppgl_ct", model_versions={"anatomy": "TotalSegmentator", "ppgl": "model_best.pth"}
    )
    metrics = result["sections"]["deterministic_metrics"]
    assert metrics["tumor_volume_ml"] == 18.5
    assert metrics["tumor_volume_ml"] != 1200


def test_safe_context_excludes_case_id_and_paths(tmp_path: Path) -> None:
    write_json(tmp_path / "output" / "metrics.json", {"wt_volume_ml": 5.0})
    service = UnifiedReportService(TestStorage(tmp_path))
    service.generate("secret-case-id", "brain_mri", model_versions={"segmentation": "best.pth"})
    context = service.safe_context("secret-case-id", "brain_mri")
    text = json.dumps(context, ensure_ascii=False)
    assert "secret-case-id" not in text
    assert str(tmp_path) not in text


def test_privacy_rejects_identity_and_local_paths() -> None:
    for value in ("患者姓名：张三", "病例号：ABC123", "手机号 13800138000", r"E:\hospital\patient.nii.gz"):
        assert contains_sensitive_text(value)
        assert "已脱敏" in redact_text(value)


def test_rag_citations_include_version_page_and_chunk() -> None:
    evidence = [{"title": "指南", "organization": "学会", "version": "2026", "section": "影像",
                 "page_start": 8, "page_end": 9, "chunk_id": "chunk-1", "source_url": "https://example.org",
                 "content": "需要结合完整影像复核。", "score": 0.8}]
    prompt = build_knowledge_rag_prompt("如何复核？", evidence)
    citations = citation_payload(evidence)
    assert "版本：2026" in prompt and "第 8-9 页" in prompt
    assert citations[0]["version"] == "2026"
    assert citations[0]["chunk_id"] == "chunk-1"
