"""Fail-closed release audit for the MedVision source tree.

The scanner does not inspect generated dependency/build directories. It reports
medical images, model weights, runtime databases/logs, private environment
files, likely credentials, developer-specific absolute paths, and unexpectedly
large source files. Findings are written as JSON for Stage 9 evidence.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any


EXCLUDED_DIRS = {
    ".git",
    ".idea",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
    "target",
}
FORBIDDEN_SUFFIXES = {
    ".dcm",
    ".dicom",
    ".db",
    ".glb",
    ".h5",
    ".nii",
    ".onnx",
    ".pth",
    ".pt",
    ".sqlite",
    ".sqlite3",
}
TEXT_SUFFIXES = {
    ".cmd",
    ".css",
    ".example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".properties",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".vue",
    ".yaml",
    ".yml",
}
PRIVATE_ENV_NAMES = {".env", ".env.local", ".env.production", ".env.development"}
SECRET_PATTERNS = (
    ("private_key", re.compile("-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "credential_url",
        re.compile(r"(?:postgres(?:ql)?(?:\+psycopg)?|mysql|mongodb(?:\+srv)?)://[^\s:/@]+:[^\s/@]+@", re.I),
    ),
    (
        "literal_secret",
        re.compile(
            r"(?im)^\s*(?:[A-Z0-9_]*(?:PASSWORD|TOKEN|SECRET|API_KEY)[A-Z0-9_]*)\s*=\s*(['\"])[^'\"\r\n]{8,}\1"
        ),
    ),
)
SUPPLEMENTAL_PATTERNS = (
    ("personal_home_path", re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/")),
    ("wechat_appid", re.compile(r"\bwx[0-9a-fA-F]{16}\b")),
    ("phone_like_value", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("legacy_account_note", re.compile(r"(?im)^.*(?:演示账号|常用测试账号).*[/：:]\s*\d{6,}")),
)
WINDOWS_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-WY-Z]:[\\/](?!MedVisionModels|MedVisionRuntime)", re.I)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def finding(kind: str, relative: Path, detail: str, severity: str = "error") -> dict[str, str]:
    return {"severity": severity, "kind": kind, "path": relative.as_posix(), "detail": detail}


def scan(root: Path, max_source_mib: int) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    scanned_files = 0
    scanned_bytes = 0
    max_bytes = max_source_mib * 1024 * 1024

    def files():
        for parent, directories, names in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories if name not in EXCLUDED_DIRS]
            for name in names:
                yield Path(parent) / name

    for path in files():
        relative = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if not path.is_file():
            continue
        scanned_files += 1
        size = path.stat().st_size
        scanned_bytes += size
        lower_name = path.name.lower()

        if lower_name in PRIVATE_ENV_NAMES:
            findings.append(
                finding(
                    "private_environment",
                    relative,
                    "本机私有配置存在；制作公开包时必须排除",
                    severity="warning",
                )
            )
        if lower_name.endswith(".nii.gz") or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append(finding("binary_asset", relative, "疑似医学数据、权重或运行数据库"))
        if lower_name.endswith((".log", ".pid")):
            findings.append(finding("runtime_artifact", relative, "运行日志或 PID 文件"))
        if size > max_bytes:
            findings.append(finding("large_file", relative, f"文件大小 {size / 1024 / 1024:.1f} MiB"))

        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"Dockerfile", "Makefile"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            findings.append(finding("text_encoding", relative, "声明为文本但不是有效 UTF-8"))
            continue

        is_test = "tests" in relative.parts
        is_example = lower_name.endswith(".example") or lower_name == ".env.example"
        for name, pattern in SECRET_PATTERNS:
            matches = list(pattern.finditer(text))
            if name == "credential_url":
                matches = [item for item in matches if not any(
                    marker in item.group(0) for marker in ("CHANGE_ME", "YOUR_PASSWORD", "<password>")
                )]
            if matches and not (is_test or is_example):
                findings.append(finding(name, relative, "检测到疑似硬编码凭据"))
        is_scanner = relative.as_posix() == "scripts/windows/audit_release.py"
        if not (is_test or is_example or is_scanner):
            for name, pattern in SUPPLEMENTAL_PATTERNS:
                if pattern.search(text):
                    findings.append(finding(name, relative, "需要人工检查的个人标识、旧账号记录或本机路径"))
        if WINDOWS_ABSOLUTE_PATH.search(text) and not (is_test or is_example or is_scanner):
            findings.append(finding("absolute_path", relative, "检测到开发者机器绝对路径"))

    errors = sum(item["severity"] == "error" for item in findings)
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": root.name,
        "scanned_files": scanned_files,
        "scanned_bytes": scanned_bytes,
        "excluded_directories": sorted(EXCLUDED_DIRS),
        "summary": {"errors": errors, "warnings": len(findings) - errors},
        "findings": findings,
        "status": "passed" if errors == 0 else "failed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="MedVision public-release sensitive-content audit")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-source-mib", type=int, default=100)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    result = scan(root, args.max_source_mib)
    atomic_json(args.output.expanduser().resolve(), result)
    print(
        f"Release audit {result['status']}: {result['scanned_files']} files, "
        f"{result['summary']['errors']} errors"
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
