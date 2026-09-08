"""Repeatable Stage 9 API acceptance against an isolated MedVision runtime.

The script never prints or stores passwords. Set MEDVISION_ACCEPTANCE_PASSWORD in
the parent process and point the server at a dedicated acceptance database/root.
Real MRI inference is optional and requires a directory containing flair.nii.gz,
t1.nii.gz, t1ce.nii.gz and t2.nii.gz.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
from typing import Any

import httpx


TERMINAL_TASK_STATES = {"completed", "failed", "cancelled", "timed_out"}


class AcceptanceFailure(RuntimeError):
    pass


def require(response: httpx.Response, expected: int | tuple[int, ...], label: str) -> httpx.Response:
    values = (expected,) if isinstance(expected, int) else expected
    if response.status_code not in values:
        detail = response.text[:400].replace("\n", " ")
        raise AcceptanceFailure(f"{label}: HTTP {response.status_code}, expected {values}: {detail}")
    return response


def gpu_sample() -> dict[str, float | str] | None:
    command = [
        "nvidia-smi",
        "--query-gpu=name,memory.used,memory.total,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        name, used, total, utilization = [part.strip() for part in result.stdout.splitlines()[0].split(",")]
        return {"name": name, "memory_used_mib": float(used), "memory_total_mib": float(total), "utilization_percent": float(utilization)}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def stream_events(client: httpx.Client, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    event_types: list[str] = []
    delta_count = 0
    character_count = 0
    with client.stream("POST", url, json=payload, headers=headers, timeout=180) as response:
        require(response, 200, f"stream {url}")
        for line in response.iter_lines():
            if not line.strip():
                continue
            event = json.loads(line)
            event_type = str(event.get("type") or "")
            event_types.append(event_type)
            if event_type == "delta":
                delta_count += 1
                character_count += len(str(event.get("text") or ""))
            if event_type == "error":
                raise AcceptanceFailure(f"stream {url}: {event.get('detail')}")
    if "done" not in event_types or delta_count < 2 or character_count < 10:
        raise AcceptanceFailure(f"stream {url}: incomplete stream ({event_types})")
    return {"event_types": event_types, "delta_count": delta_count, "character_count": character_count}


def run(args: argparse.Namespace) -> dict[str, Any]:
    password = os.environ.get("MEDVISION_ACCEPTANCE_PASSWORD", "")
    if len(password) < 16:
        raise AcceptanceFailure("MEDVISION_ACCEPTANCE_PASSWORD must contain at least 16 characters")
    doctor_password = secrets.token_urlsafe(24) + "!Aa1"
    base_url = args.base_url.rstrip("/")
    started = time.perf_counter()
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "checks": [],
        "mri": {"requested": bool(args.mri_dir), "real_inference_requested": args.run_mri_inference},
    }

    def passed(name: str, details: dict[str, Any] | None = None) -> None:
        result["checks"].append({"name": name, "status": "passed", "details": details or {}})

    with httpx.Client(base_url=base_url, timeout=60, follow_redirects=False) as admin:
        health = require(admin.get("/health"), 200, "health").json()
        ready = require(admin.get("/ready"), 200, "ready").json()
        if health.get("status") != "ok" or ready.get("status") != "ready":
            raise AcceptanceFailure("health/readiness payload is not ready")
        passed("FastAPI 存活与就绪", {"gpu_models_loaded": ready.get("gpu_models_loaded")})

        setup_status = require(admin.get("/api/v1/auth/setup/status"), 200, "setup status")
        if setup_status.json().get("required") is not True:
            raise AcceptanceFailure("acceptance database is not fresh")
        require(admin.post("/api/v1/auth/setup", json={
            "username": "acceptance_admin",
            "display_name": "验收管理员",
            "password": password,
        }), 201, "initial admin setup")
        login = require(admin.post("/api/v1/auth/login", json={"username": "acceptance_admin", "password": password}), 200, "admin login")
        admin_headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        me = require(admin.get("/api/v1/auth/me"), 200, "admin me")
        if me.json()["user"]["role"] != "admin":
            raise AcceptanceFailure("initial account is not admin")
        passed("全新管理员初始化与登录")

        security_headers = {key.lower(): value for key, value in me.headers.items()}
        required_headers = {
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer",
        }
        for key, value in required_headers.items():
            if value not in security_headers.get(key, ""):
                raise AcceptanceFailure(f"missing security header: {key}")
        passed("API 禁止缓存与安全响应头")

        with httpx.Client(base_url=base_url, timeout=30) as anonymous:
            require(anonymous.get("/api/v1/cases"), 401, "anonymous case list")
            require(anonymous.get("/api/v1/cases/not-a-case/mri/segmentation"), 401, "anonymous MRI asset")
            require(anonymous.get("/api/v1/cases/not-a-case/ct/assets/mask"), 401, "anonymous CT asset")
            cors = require(anonymous.get("/api/v1/auth/setup/status", headers={"Origin": "https://untrusted.invalid"}), 200, "untrusted CORS")
            if cors.headers.get("access-control-allow-origin"):
                raise AcceptanceFailure("untrusted origin received CORS permission")
        passed("未登录资源隔离与受限 CORS")

        csrf_blocked = admin.post("/api/v1/cases", json={"case_type": "ppgl_ct", "display_name": "缺少令牌"})
        require(csrf_blocked, 403, "CSRF rejection")
        sensitive = admin.post("/api/v1/cases", headers=admin_headers, json={"case_type": "ppgl_ct", "display_name": "患者姓名：张三"})
        require(sensitive, 422, "sensitive display name")
        passed("CSRF 与病例身份信息拦截")

        doctor = require(admin.post("/api/v1/admin/users", headers=admin_headers, json={
            "username": "acceptance_doctor",
            "display_name": "验收医生",
            "password": doctor_password,
            "role": "doctor",
        }), 201, "create doctor").json()["user"]
        require(admin.patch(f"/api/v1/admin/users/{doctor['id']}/status", headers=admin_headers, json={"active": False}), 200, "disable doctor")
        with httpx.Client(base_url=base_url, timeout=30) as disabled:
            require(disabled.post("/api/v1/auth/login", json={"username": "acceptance_doctor", "password": doctor_password}), 401, "disabled login")
        require(admin.patch(f"/api/v1/admin/users/{doctor['id']}/status", headers=admin_headers, json={"active": True}), 200, "enable doctor")
        passed("账号创建、停用与恢复")

        ct_case = require(admin.post("/api/v1/cases", headers=admin_headers, json={"case_type": "ppgl_ct", "display_name": "CT 验收病例"}), 201, "create CT case").json()
        mri_case = require(admin.post("/api/v1/cases", headers=admin_headers, json={"case_type": "brain_mri", "display_name": "MRI 验收病例"}), 201, "create MRI case").json()
        require(admin.patch(f"/api/v1/cases/{mri_case['case_id']}", headers=admin_headers, json={"display_name": "MRI 验收病例-已重命名"}), 200, "rename MRI case")
        passed("统一病例创建与重命名")

        with httpx.Client(base_url=base_url, timeout=60) as doctor_client:
            doctor_login = require(doctor_client.post("/api/v1/auth/login", json={"username": "acceptance_doctor", "password": doctor_password}), 200, "doctor login")
            doctor_headers = {"X-CSRF-Token": doctor_login.json()["csrf_token"]}
            require(doctor_client.get("/api/v1/admin/users"), 403, "doctor admin boundary")
            require(doctor_client.get(f"/api/v1/cases/{mri_case['case_id']}"), 403, "doctor ungranted case")
            require(admin.put(f"/api/v1/cases/{mri_case['case_id']}/grants", headers=admin_headers, json={"user_id": doctor["id"], "permission": "read"}), 200, "grant MRI read")
            require(doctor_client.get(f"/api/v1/cases/{mri_case['case_id']}"), 200, "doctor granted read")
            require(doctor_client.patch(f"/api/v1/cases/{mri_case['case_id']}", headers=doctor_headers, json={"display_name": "越权修改"}), 403, "doctor read-only edit")
            sent = require(admin.post("/api/v1/messages", headers=admin_headers, json={"recipient_id": doctor["id"], "content": "请完成验收病例复核"}), 201, "send message").json()
            conversation = require(doctor_client.get(f"/api/v1/messages/{sent['sender_id']}"), 200, "read conversation").json()
            if not conversation.get("messages"):
                raise AcceptanceFailure("private message was not delivered")
            require(doctor_client.post(f"/api/v1/messages/{sent['sender_id']}/read", headers=doctor_headers), 200, "mark message read")
        passed("角色菜单后端边界、病例共享与私聊")

        oversized = b"0" * (2 * 1024 * 1024)
        upload = admin.put(
            f"/api/v1/cases/{ct_case['case_id']}/ct/image",
            headers=admin_headers,
            files={"file": ("acceptance.nii.gz", oversized, "application/gzip")},
        )
        require(upload, 413, "CT upload capacity limit")
        passed("CT 大文件容量限制")

        require(admin.request("DELETE", f"/api/v1/cases/{ct_case['case_id']}", headers=admin_headers, json={"reason_code": "test_cleanup"}), 200, "trash CT")
        require(admin.post(f"/api/v1/trash/{ct_case['case_id']}/restore", headers=admin_headers), 200, "restore CT")
        require(admin.request("DELETE", f"/api/v1/cases/{ct_case['case_id']}", headers=admin_headers, json={"reason_code": "test_cleanup"}), 200, "trash CT again")
        require(admin.request("DELETE", f"/api/v1/trash/{ct_case['case_id']}", headers=admin_headers, json={"reason_code": "approved_purge", "confirmation": ct_case["case_id"]}), 200, "purge CT")
        passed("软删除、恢复与永久清除")

        if args.mri_dir:
            source = Path(args.mri_dir).expanduser().resolve()
            modalities = {name: source / f"{name}.nii.gz" for name in ("flair", "t1", "t1ce", "t2")}
            missing = [name for name, path in modalities.items() if not path.is_file()]
            if missing:
                raise AcceptanceFailure(f"MRI sample is incomplete: {missing}")
            sizes = {name: path.stat().st_size for name, path in modalities.items()}
            require(admin.post(f"/api/v1/cases/{mri_case['case_id']}/mri/uploads/preflight", headers=admin_headers, json={"modalities": sizes}), 200, "MRI preflight")
            for name, path in modalities.items():
                with path.open("rb") as stream:
                    response = admin.put(
                        f"/api/v1/cases/{mri_case['case_id']}/mri/images/{name}",
                        headers=admin_headers,
                        files={"file": (path.name, stream, "application/gzip")},
                        timeout=300,
                    )
                require(response, 200, f"upload MRI {name}")
            completed_upload = require(admin.post(f"/api/v1/cases/{mri_case['case_id']}/mri/uploads/complete", headers=admin_headers), 200, "complete MRI upload").json()
            passed("MRI 四序列上传与空间一致性", {"status": completed_upload.get("status"), "total_bytes": sum(sizes.values())})

            if args.run_mri_inference:
                model = require(admin.get("/api/v1/cases/models/brain"), 200, "brain model status").json()
                if model.get("status") != "ready" or not model.get("inference_enabled"):
                    raise AcceptanceFailure(f"brain model is not ready: {model}")
                baseline_gpu = gpu_sample()
                baseline_disk = shutil.disk_usage(args.runtime_root).free if args.runtime_root else None
                submitted_at = time.perf_counter()
                require(admin.post(f"/api/v1/cases/{mri_case['case_id']}/mri/segment", headers=admin_headers), 202, "submit MRI inference")
                peak_gpu = baseline_gpu.get("memory_used_mib", 0.0) if baseline_gpu else 0.0
                final_state: dict[str, Any] = {}
                deadline = time.monotonic() + args.inference_timeout
                while time.monotonic() < deadline:
                    final_state = require(admin.get(f"/api/v1/cases/{mri_case['case_id']}/mri/task"), 200, "poll MRI task").json()
                    sample = gpu_sample()
                    if sample:
                        peak_gpu = max(peak_gpu, float(sample["memory_used_mib"]))
                    if final_state.get("status") in TERMINAL_TASK_STATES:
                        break
                    time.sleep(args.poll_interval)
                elapsed = time.perf_counter() - submitted_at
                if final_state.get("status") != "completed":
                    raise AcceptanceFailure(f"MRI inference did not complete: {final_state}")
                metrics = require(admin.get(f"/api/v1/cases/{mri_case['case_id']}/mri/metrics"), 200, "MRI metrics").json()
                segmentation = require(admin.get(f"/api/v1/cases/{mri_case['case_id']}/mri/segmentation"), 200, "MRI segmentation")
                visualizations = require(admin.get(f"/api/v1/cases/{mri_case['case_id']}/mri/visualizations"), 200, "MRI visualizations").json()
                report = require(admin.post(f"/api/v1/cases/{mri_case['case_id']}/report/generate", headers=admin_headers), 201, "MRI report")
                for kind in ("markdown", "json", "pdf"):
                    require(admin.get(f"/api/v1/cases/{mri_case['case_id']}/report/{kind}"), 200, f"download report {kind}")
                require(admin.put(f"/api/v1/cases/{mri_case['case_id']}/review", headers=admin_headers, json={
                    "opinion": "建议结合四序列原始影像复核分割边界。",
                    "doctor_name": "验收医生",
                    "review_date": datetime.now().date().isoformat(),
                }), 200, "doctor review")
                disk_used = None
                if baseline_disk is not None:
                    disk_used = max(0, baseline_disk - shutil.disk_usage(args.runtime_root).free)
                result["mri"].update({
                    "status": "passed",
                    "elapsed_seconds": round(elapsed, 2),
                    "baseline_gpu_memory_mib": baseline_gpu.get("memory_used_mib") if baseline_gpu else None,
                    "peak_gpu_memory_mib": round(peak_gpu, 1),
                    "runtime_disk_growth_bytes": disk_used,
                    "segmentation_bytes": len(segmentation.content),
                    "metrics_top_level_keys": sorted(metrics.keys()),
                    "visualization_top_level_keys": sorted(visualizations.keys()),
                    "report_status": report.status_code,
                })
                passed("真实 nnU-Net、定量、可视化、报告与医生复核", {"elapsed_seconds": round(elapsed, 2)})
                case_stream = stream_events(admin, f"/api/v1/cases/{mri_case['case_id']}/assistant/stream", {
                    "question": "请说明该定量结果的影像复核重点和下一步就医路径。",
                    "history": [],
                    "max_tokens": 256,
                }, admin_headers)
                result["mri"]["case_assistant_stream"] = case_stream
                passed("病例助手真实流式输出", case_stream)

        general_stream = stream_events(admin, "/api/v1/llm/chat/stream", {
            "question": "请简要说明脑肿瘤 MRI 四序列各自的一般影像学作用。",
            "history": [],
            "max_tokens": 256,
        }, admin_headers)
        passed("普通助手真实流式输出", general_stream)

        integrity = require(admin.get("/api/v1/admin/audit/integrity"), 200, "audit integrity").json()
        if not integrity.get("valid"):
            raise AcceptanceFailure(f"audit chain invalid: {integrity}")
        audit = require(admin.get("/api/v1/admin/audit?limit=1000"), 200, "audit list").json()
        export = require(admin.get("/api/v1/admin/audit/export.csv"), 200, "audit export")
        if len(export.content) < 100 or audit.get("total", 0) < 10:
            raise AcceptanceFailure("audit output is unexpectedly empty")
        passed("操作审计、导出与哈希链", {"records": audit.get("total"), "csv_bytes": len(export.content)})

        require(admin.post("/api/v1/auth/logout", headers=admin_headers), 200, "logout")
        require(admin.get("/api/v1/auth/me"), 401, "session cleared after logout")
        passed("退出登录与会话清理")

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    result["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    result["summary"] = {"passed": len(result["checks"]), "failed": 0}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="MedVision Stage 9 isolated API acceptance")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--mri-dir", type=Path)
    parser.add_argument("--run-mri-inference", action="store_true")
    parser.add_argument("--inference-timeout", type=int, default=3600)
    parser.add_argument("--poll-interval", type=float, default=3.0)
    args = parser.parse_args()
    try:
        result = run(args)
    except Exception as exc:
        result = {
            "schema_version": "1.0",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "summary": {"passed": 0, "failed": 1},
            "failure_type": type(exc).__name__,
            "failure": str(exc),
        }
        atomic_json(args.output, result)
        print(f"Stage 9 acceptance failed: {type(exc).__name__}: {exc}")
        return 1
    atomic_json(args.output, result)
    print(f"Stage 9 acceptance passed: {result['summary']['passed']} checks")
    print(f"Result: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
