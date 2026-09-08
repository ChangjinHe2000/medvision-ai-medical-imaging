"""Windows-first launcher for the MedVision Vue/FastAPI workstation.

The launcher never evaluates .env.local as code. Runtime state, PID metadata and
logs are stored below PPGL_DATA_ROOT, outside the source repository.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, build_opener
import uuid
import webbrowser


PROJECT_ROOT = Path(__file__).resolve().parent
BACKEND_DIR = PROJECT_ROOT / "ai-backend"
FRONTEND_DIR = PROJECT_ROOT / "frontend-vue-prototype"
ENV_FILE = PROJECT_ROOT / ".env.local"
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DIRECT_HTTP = build_opener(ProxyHandler({}))
STATE_NAME = "medvision-services.json"
MIN_FREE_BYTES = 5 * 1024**3


@dataclass
class Check:
    name: str
    status: str
    detail: str
    required: bool = True


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE without shell expansion, interpolation or execution."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(f".env.local 第 {number} 行缺少等号")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not ENV_KEY.fullmatch(key):
            raise RuntimeError(f".env.local 第 {number} 行变量名无效")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def setting(values: dict[str, str], key: str, default: str = "") -> str:
    return str(os.environ.get(key, values.get(key, default))).strip()


def boolean(values: dict[str, str], key: str, default: bool = False) -> bool:
    value = setting(values, key, "true" if default else "false").casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{key} 必须是 true 或 false")


def integer(values: dict[str, str], key: str, default: int) -> int:
    try:
        value = int(setting(values, key, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{key} 必须是整数") from exc
    if not 1 <= value <= 65535:
        raise RuntimeError(f"{key} 必须在 1-65535 之间")
    return value


def resolve_path(value: str, *, base: Path = PROJECT_ROOT) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def configured_path(values: dict[str, str], key: str, default: Path) -> Path:
    value = setting(values, key)
    return resolve_path(value) if value else default.resolve()


def command_path(values: dict[str, str], key: str, names: tuple[str, ...]) -> Path | None:
    configured = setting(values, key)
    if configured:
        raw = Path(configured)
        if not raw.is_absolute() and raw.parent == Path("."):
            found = shutil.which(configured)
            if found:
                return Path(found).resolve()
        path = resolve_path(configured)
        return path if path.is_file() else None
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    return None


def find_ppgl_python(values: dict[str, str]) -> Path | None:
    configured = command_path(values, "PPGL_PYTHON_BIN", ("python.exe", "python"))
    if configured:
        return configured
    if setting(values, "PPGL_PYTHON_BIN"):
        return None
    candidates: list[Path] = []
    if configured:
        candidates.append(configured)
    current = Path(sys.executable).resolve()
    candidates.append(current)
    conda = shutil.which("conda.exe") or shutil.which("conda")
    if conda:
        try:
            result = subprocess.run(
                [conda, "env", "list", "--json"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=20, check=False,
            )
            for root in json.loads(result.stdout or "{}").get("envs", []):
                candidate = Path(root) / ("python.exe" if os.name == "nt" else "bin/python")
                if Path(root).name.casefold() == "ppgl":
                    candidates.insert(0, candidate)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
    for candidate in candidates:
        if candidate.is_file() and candidate.parent.name.casefold() == "ppgl":
            return candidate.resolve()
    return configured if configured and configured.is_file() else None


def find_node_tools(values: dict[str, str]) -> tuple[Path | None, Path | None]:
    node = command_path(values, "PPGL_NODE_BIN", ("node.exe", "node"))
    npm = command_path(values, "PPGL_NPM_BIN", ("npm.cmd", "npm"))
    if node and not npm:
        candidate = node.parent / ("npm.cmd" if os.name == "nt" else "npm")
        npm = candidate if candidate.is_file() else None
    return node, npm


def run_text(command: list[str], *, timeout: float = 30, cwd: Path | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
        return result.returncode, (result.stdout or result.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, type(exc).__name__


def safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "已配置（地址格式无效）"
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))


def state_paths(values: dict[str, str]) -> tuple[Path, Path, Path]:
    data_root = configured_path(values, "PPGL_DATA_ROOT", PROJECT_ROOT.parent / "MedVisionRuntime")
    pid_dir = configured_path(values, "PPGL_PID_DIR", data_root / "pids")
    log_dir = configured_path(values, "PPGL_LOG_DIR", data_root / "logs")
    return data_root, pid_dir / STATE_NAME, log_dir


def http_json(url: str, timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        with DIRECT_HTTP.open(url, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                return None
            value = json.loads(response.read(1024 * 1024).decode("utf-8"))
            return value if isinstance(value, dict) else None
    except Exception:
        return None


def http_ready(url: str, timeout: float = 1.0) -> bool:
    try:
        with DIRECT_HTTP.open(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except Exception:
        return False


def port_open(host: str, port: int) -> bool:
    target = "127.0.0.1" if host in {"0.0.0.0", "::", "localhost"} else host
    family = socket.AF_INET6 if ":" in target else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.4)
            return probe.connect_ex((target, port)) == 0
    except OSError:
        return False


def model_checks(values: dict[str, str], python: Path | None) -> list[Check]:
    checks: list[Check] = []
    data_root, _, _ = state_paths(values)
    ppgl_model_dir = configured_path(values, "PPGL_V5_MODEL_DIR", BACKEND_DIR / "progress_patch_v5")
    checkpoint_value = setting(values, "PPGL_V5_CHECKPOINT")
    config_value = setting(values, "PPGL_V5_MODEL_CONFIG")
    ppgl_checkpoint = resolve_path(checkpoint_value, base=ppgl_model_dir) if checkpoint_value else ppgl_model_dir / "weights" / "model_best.pth"
    ppgl_config = resolve_path(config_value, base=ppgl_model_dir) if config_value else ppgl_model_dir / "model_config.json"
    checks.append(Check("ProgressPatchV5 权重", "ok" if ppgl_checkpoint.is_file() else "error", str(ppgl_checkpoint)))
    checks.append(Check("ProgressPatchV5 配置", "ok" if ppgl_config.is_file() else "error", str(ppgl_config)))

    total_value = setting(values, "TOTALSEG_WEIGHTS_PATH")
    total_root = resolve_path(total_value, base=data_root) if total_value else Path.home() / ".totalsegmentator" / "nnunet" / "results"
    required_datasets = (291, 292, 293, 294, 295, 298, 300)
    available = {int(match.group(1)) for item in total_root.glob("Dataset*") if (match := re.match(r"Dataset(\d{3})", item.name))}
    missing = [str(item) for item in required_datasets if item not in available]
    checks.append(Check("TotalSegmentator 离线权重", "ok" if not missing else "error", str(total_root) if not missing else f"缺少 Dataset {', '.join(missing)}"))

    brain_value = setting(values, "PPGL_NNUNET_MODEL_DIR") or setting(values, "PPGL_GLIOMA_MODEL_DIR")
    brain_root = resolve_path(brain_value, base=data_root) if brain_value else data_root / "models" / "brain-tumour"
    checkpoint = setting(values, "PPGL_NNUNET_CHECKPOINT", "checkpoint_best.pth")
    brain_ready = brain_root.is_dir() and any(brain_root.rglob(checkpoint))
    brain_required = boolean(values, "PPGL_GLIOMA_ENABLED", False)
    checks.append(Check("脑肿瘤 nnU-Net 稳定快照", "ok" if brain_ready else ("error" if brain_required else "warning"), str(brain_root), brain_required))

    if python:
        scripts = python.parent / ("Scripts" if os.name == "nt" else "bin")
        for label, filename, required in (
            ("TotalSegmentator 命令", "TotalSegmentator.exe" if os.name == "nt" else "TotalSegmentator", True),
            ("nnUNetv2_predict 命令", "nnUNetv2_predict.exe" if os.name == "nt" else "nnUNetv2_predict", brain_required),
        ):
            path = scripts / filename
            checks.append(Check(label, "ok" if path.is_file() else ("error" if required else "warning"), str(path), required))
    return checks


def perform_checks(values: dict[str, str], *, for_start: bool = False) -> tuple[list[Check], dict[str, Any]]:
    checks: list[Check] = []
    context: dict[str, Any] = {}
    if not ENV_FILE.is_file():
        checks.append(Check("本机配置", "error", f"缺少 {ENV_FILE.name}，请从 .env.example 复制后修改"))
    else:
        checks.append(Check("本机配置", "ok", str(ENV_FILE)))

    python = find_ppgl_python(values)
    context["python"] = python
    if not python:
        checks.append(Check("PPGL Python", "error", "未找到 PPGL Conda 环境"))
    else:
        code, output = run_text([str(python), "-c", "import platform; print(platform.python_version())"])
        good = code == 0 and output.startswith("3.10.")
        checks.append(Check("PPGL Python", "ok" if good else "error", f"{python}（{output}）"))
        imports = "import fastapi,uvicorn,torch,numpy,nibabel,SimpleITK,sqlalchemy,psycopg,pgvector,fitz,docx"
        code, output = run_text([str(python), "-c", imports], timeout=60)
        checks.append(Check("Python 关键依赖", "ok" if code == 0 else "error", "已安装" if code == 0 else output[-300:]))
        cuda_code = "import torch; print(torch.cuda.is_available(), torch.version.cuda or '')"
        code, output = run_text([str(python), "-c", cuda_code], timeout=30)
        cuda_ok = code == 0 and output.casefold().startswith("true")
        inference_required = boolean(values, "PPGL_GLIOMA_ENABLED", False)
        checks.append(Check("CUDA 可用性", "ok" if cuda_ok else ("error" if inference_required else "warning"), output or "不可用", inference_required))

    node, npm = find_node_tools(values)
    context.update({"node": node, "npm": npm})
    for label, executable, expected in (("Node.js", node, "v24."), ("npm", npm, "11.")):
        if not executable:
            checks.append(Check(label, "error", "未找到可执行文件"))
            continue
        code, output = run_text([str(executable), "--version"])
        checks.append(Check(label, "ok" if code == 0 and output.startswith(expected) else "error", f"{executable}（{output}）"))
    checks.append(Check("Vue 依赖", "ok" if (FRONTEND_DIR / "node_modules" / "vite").is_dir() else "error", str(FRONTEND_DIR / "node_modules")))

    gltfpack = command_path(values, "PPGL_GLTFPACK_BIN", ("gltfpack.cmd", "gltfpack"))
    if not gltfpack:
        candidate = FRONTEND_DIR / "node_modules" / ".bin" / ("gltfpack.cmd" if os.name == "nt" else "gltfpack")
        gltfpack = candidate.resolve() if candidate.is_file() else None
    context["gltfpack"] = gltfpack
    checks.append(Check("gltfpack", "ok" if gltfpack else "warning", str(gltfpack) if gltfpack else "未找到，将跳过网格压缩", False))

    data_root, state_file, log_dir = state_paths(values)
    context.update({"data_root": data_root, "state_file": state_file, "log_dir": log_dir})
    try:
        data_root.relative_to(PROJECT_ROOT)
        outside = False
    except ValueError:
        outside = True
    checks.append(Check("外部运行目录", "ok" if outside else "error", str(data_root)))
    existing_parent = next((item for item in (data_root, *data_root.parents) if item.exists()), None)
    if existing_parent:
        free = shutil.disk_usage(existing_parent).free
        checks.append(Check("运行盘剩余空间", "ok" if free >= MIN_FREE_BYTES else "error", f"{free / 1024**3:.1f} GB"))
    if for_start:
        try:
            for directory in (
                data_root, log_dir, state_file.parent,
                configured_path(values, "PPGL_CASES_DIR", data_root / "cases"),
                configured_path(values, "PPGL_GLIOMA_CASES_DIR", data_root / "brain-cases"),
                configured_path(values, "PPGL_TEMP_DIR", data_root / "temp"),
                configured_path(values, "PPGL_TASKS_DIR", data_root / "tasks"),
            ):
                directory.mkdir(parents=True, exist_ok=True)
            probe = data_root / ".write-check"
            probe.write_text("ok", encoding="ascii")
            probe.unlink()
            checks.append(Check("运行目录写入", "ok", str(data_root)))
        except OSError as exc:
            checks.append(Check("运行目录写入", "error", type(exc).__name__))

    checks.extend(model_checks(values, python))

    backend_host = setting(values, "PPGL_API_HOST", "127.0.0.1")
    backend_port = integer(values, "PPGL_API_PORT", 8000)
    frontend_host = setting(values, "VITE_HOST", "127.0.0.1")
    frontend_port = integer(values, "VITE_PORT", 5173)
    context.update({"backend_host": backend_host, "backend_port": backend_port, "frontend_host": frontend_host, "frontend_port": frontend_port})
    for label, host, port in (("FastAPI 端口", backend_host, backend_port), ("Vue 端口", frontend_host, frontend_port)):
        occupied = port_open(host, port)
        checks.append(Check(label, "error" if occupied and for_start else ("warning" if occupied else "ok"), f"{host}:{port}" + (" 已占用" if occupied else " 可用"), for_start))

    ollama_enabled = boolean(values, "PPGL_ENABLE_OLLAMA", False)
    ollama_url = setting(values, "PPGL_OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    ollama_model = setting(values, "PPGL_OLLAMA_MODEL", "qwen2.5:3b-instruct-q4_0")
    tags = http_json(ollama_url + "/api/tags") if ollama_enabled else None
    models = {str(item.get("name") or item.get("model") or "") for item in (tags or {}).get("models", []) if isinstance(item, dict)}
    model_ready = ollama_model in models or f"{ollama_model}:latest" in models
    checks.append(Check("Ollama", "ok" if model_ready else ("error" if ollama_enabled else "warning"), f"{safe_url(ollama_url)}；模型 {ollama_model}" if tags else "未启用或服务离线", ollama_enabled))
    context.update({"ollama_enabled": ollama_enabled, "ollama_url": ollama_url, "ollama_model": ollama_model, "ollama_online": tags is not None})

    auth_url = setting(values, "PPGL_AUTH_DATABASE_URL", "")
    rag_enabled = boolean(values, "PPGL_RAG_ENABLED", False)
    postgres_required = auth_url.startswith("postgresql") or rag_enabled
    postgres_running = False
    if os.name == "nt":
        code, output = run_text(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "@(Get-Service -ErrorAction SilentlyContinue | Where-Object {$_.Name -like 'postgres*' -and $_.Status -eq 'Running'}).Count"])
        postgres_running = code == 0 and output.strip().isdigit() and int(output.strip()) > 0
    checks.append(Check("PostgreSQL", "ok" if postgres_running else ("error" if postgres_required else "warning"), "服务正在运行" if postgres_running else "未启用或未检测到运行服务", postgres_required))

    return checks, context


def print_checks(checks: list[Check]) -> None:
    labels = {"ok": "通过", "warning": "提示", "error": "错误"}
    for item in checks:
        print(f"[{labels[item.status]}] {item.name}：{item.detail}")


def checks_failed(checks: list[Check]) -> bool:
    return any(item.status == "error" and item.required for item in checks)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def process_info(pid: int) -> dict[str, str] | None:
    if pid <= 0:
        return None
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return {"executable": "", "command_line": ""}
        except OSError:
            return None
    command = (
        f"$c=Get-CimInstance Win32_Process -Filter \"ProcessId = {int(pid)}\" -ErrorAction SilentlyContinue;"
        f"$p=Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue;"
        "if($null -ne $p){"
        "$cmd=if($null -ne $c){[string]$c.CommandLine}else{''};"
        "$exe=if($null -ne $c -and $c.ExecutablePath){[string]$c.ExecutablePath}else{[string]$p.Path};"
        "$created=([DateTimeOffset]$p.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds();"
        "[pscustomobject]@{ExecutablePath=$exe;CommandLine=$cmd;CreationEpochMs=$created}|ConvertTo-Json -Compress}"
    )
    code, output = run_text(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], timeout=10)
    if code != 0 or not output:
        return None
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        return None
    return {
        "executable": str(value.get("ExecutablePath") or ""),
        "command_line": str(value.get("CommandLine") or ""),
        "creation_epoch_ms": str(value.get("CreationEpochMs") or ""),
    }


def process_matches(entry: dict[str, Any]) -> bool:
    info = process_info(int(entry.get("pid") or 0))
    if not info:
        return False
    command = info["command_line"].casefold()
    executable = info["executable"].casefold()
    expected = str(entry.get("executable") or "").casefold()
    markers = [str(item).casefold() for item in entry.get("markers", [])]
    # Non-elevated Windows sessions can read StartTime while both WMI
    # CommandLine and Process.Path are access-denied. In that case the PID plus
    # recorded creation time remains the process identity guard. Compare the
    # executable whenever Windows exposes it, then fall back below.
    executable_ok = not expected or not executable or Path(executable).name == Path(expected).name
    if not executable_ok:
        return False
    if command:
        return all(marker in command for marker in markers)
    try:
        launched = int(entry.get("creation_epoch_ms") or 0)
        observed = int(info.get("creation_epoch_ms") or 0)
    except (TypeError, ValueError):
        return False
    return launched > 0 and observed > 0 and abs(observed - launched) <= 10_000


def wait_http(url: str, entries: list[dict[str, Any]], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for entry in entries:
            if entry.get("owned") and not process_matches(entry):
                raise RuntimeError(f"{entry['name']} 启动时提前退出，请查看日志")
        if http_ready(url):
            return
        time.sleep(0.35)
    raise RuntimeError(f"等待服务超时：{url}")


def child_environment(values: dict[str, str], context: dict[str, Any]) -> dict[str, str]:
    # Keep the same precedence used by setting(): explicit parent-process
    # variables override the local file. This is required for isolated tests and
    # prevents a temporary acceptance run from touching the daily database.
    environment = dict(values)
    environment.update(os.environ)
    # nnUNet queries Windows commands such as hostname through subprocess. On a
    # Chinese Windows code page, forcing UTF-8 mode makes that library decode
    # native command output incorrectly. Keep UTF-8 stdout/stderr, but let Python
    # use the Windows locale for subprocess decoding.
    environment.update({"PYTHONUTF8": "0" if os.name == "nt" else "1", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "FORCE_COLOR": "0"})
    additions = [str(context["python"].parent), str(context["python"].parent / "Scripts"), str(context["node"].parent)]
    environment["PATH"] = os.pathsep.join(additions + [environment.get("PATH", "")])
    if context.get("gltfpack"):
        environment["PPGL_GLTFPACK_BIN"] = str(context["gltfpack"])
    return environment


def launch_process(name: str, command: list[str], cwd: Path, environment: dict[str, str], log_path: Path, markers: list[str]) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    launched_at_ms = int(time.time() * 1000)
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            command, cwd=str(cwd), env=environment, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True,
        )
    return {
        "name": name, "pid": process.pid, "owned": True, "started_at": utc_now(),
        "creation_epoch_ms": launched_at_ms,
        "executable": command[0], "markers": markers, "log": str(log_path),
    }


def start(values: dict[str, str], *, no_browser: bool, lan: bool, timeout: float) -> int:
    checks, context = perform_checks(values, for_start=True)
    state_file: Path = context["state_file"]
    old = load_state(state_file)
    if old and any(process_matches(entry) for entry in old.get("services", []) if entry.get("owned")):
        print("[错误] MedVision 已由统一启动器运行，请先执行 stop.ps1。", file=sys.stderr)
        return 2
    if old:
        state_file.unlink(missing_ok=True)
    # Port checks above may see stale external services; only Ollama/PostgreSQL may be reused.
    print_checks(checks)
    if checks_failed(checks):
        print("[失败] 请修复必需项后重新启动。", file=sys.stderr)
        return 1

    environment = child_environment(values, context)
    launch_id = uuid.uuid4().hex
    log_dir: Path = context["log_dir"]
    services: list[dict[str, Any]] = []
    state = {"schema_version": 1, "launch_id": launch_id, "project_root": str(PROJECT_ROOT), "started_at": utc_now(), "services": services}
    atomic_json(state_file, state)
    try:
        if context["ollama_enabled"]:
            if context["ollama_online"]:
                services.append({"name": "Ollama", "owned": False, "status": "reused", "url": safe_url(context["ollama_url"])})
            else:
                ollama = command_path(values, "PPGL_OLLAMA_BIN", ("ollama.exe", "ollama"))
                if not ollama:
                    home = setting(values, "PPGL_OLLAMA_HOME")
                    candidate = resolve_path(home) / "ollama.exe" if home else None
                    ollama = candidate if candidate and candidate.is_file() else None
                if not ollama:
                    raise RuntimeError("Ollama 已启用但服务离线，且未找到 ollama.exe")
                parsed = urlsplit(context["ollama_url"])
                ollama_env = environment.copy()
                ollama_env["OLLAMA_HOST"] = f"{parsed.hostname}:{parsed.port or 11434}"
                entry = launch_process("Ollama", [str(ollama), "serve"], PROJECT_ROOT, ollama_env, log_dir / f"ollama-{launch_id}.log", ["ollama", "serve"])
                services.append(entry); atomic_json(state_file, state)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and http_json(context["ollama_url"] + "/api/tags") is None:
                    if not process_matches(entry):
                        raise RuntimeError("Ollama 启动失败，请查看日志")
                    time.sleep(0.4)
                if http_json(context["ollama_url"] + "/api/tags") is None:
                    raise RuntimeError("等待 Ollama 超时")

        backend_host = context["backend_host"]
        backend_port = context["backend_port"]
        backend_command = [
            str(context["python"]), "-m", "uvicorn", "backend.main:app",
            "--app-dir", str(BACKEND_DIR), "--host", backend_host, "--port", str(backend_port),
            "--log-level", setting(values, "PPGL_API_LOG_LEVEL", "info"),
        ]
        backend = launch_process("FastAPI", backend_command, BACKEND_DIR, environment, log_dir / f"fastapi-{launch_id}.log", ["backend.main:app", str(BACKEND_DIR)])
        services.append(backend); atomic_json(state_file, state)
        wait_http(f"http://127.0.0.1:{backend_port}/ready", [backend], timeout)

        frontend_host = "0.0.0.0" if lan else context["frontend_host"]
        frontend_port = context["frontend_port"]
        vite_config = FRONTEND_DIR / "vite.config.js"
        vite_entry = FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"
        frontend_command = [
            str(context["node"]), str(vite_entry), "--host", frontend_host,
            "--port", str(frontend_port), "--strictPort", "--config", str(vite_config),
        ]
        frontend = launch_process("Vue", frontend_command, FRONTEND_DIR, environment, log_dir / f"vue-{launch_id}.log", ["vite.config.js", str(frontend_port)])
        services.append(frontend); atomic_json(state_file, state)
        wait_http(f"http://127.0.0.1:{frontend_port}/", [frontend], timeout)

        state["ready_at"] = utc_now(); state["frontend_url"] = f"http://127.0.0.1:{frontend_port}/"
        atomic_json(state_file, state)
        print(f"[就绪] MedVision：http://127.0.0.1:{frontend_port}/")
        print(f"[就绪] FastAPI：http://127.0.0.1:{backend_port}/docs")
        if lan:
            print(f"[局域网] 前端已监听 0.0.0.0:{frontend_port}；还需管理员执行 configure-lan-access.ps1。")
        print(f"[日志] {log_dir}")
        print(f"[停止] {PROJECT_ROOT / 'scripts' / 'windows' / 'stop.ps1'}")
        if not no_browser:
            webbrowser.open(state["frontend_url"])
        return 0
    except Exception:
        atomic_json(state_file, state)
        stop(values, quiet=True)
        raise


def terminate_entry(entry: dict[str, Any], *, timeout: float = 8.0) -> str:
    if not entry.get("owned"):
        return "保留外部服务"
    pid = int(entry.get("pid") or 0)
    if not process_info(pid):
        return "进程已退出"
    if not process_matches(entry):
        return "PID 身份不匹配，已跳过"
    if os.name == "nt":
        # Stop only the verified PID. Some Windows installations deny taskkill for
        # detached processes while Stop-Process remains available to the owner.
        run_text([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            f"Stop-Process -Id {pid} -ErrorAction Stop",
        ], timeout=10)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process_info(pid):
        time.sleep(0.25)
    if process_info(pid):
        if not process_matches(entry):
            return "PID 已复用，拒绝强制终止"
        if os.name == "nt":
            run_text([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                f"Stop-Process -Id {pid} -Force -ErrorAction Stop",
            ], timeout=10)
        else:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        forced_deadline = time.monotonic() + 3.0
        while time.monotonic() < forced_deadline and process_info(pid):
            time.sleep(0.2)
        if process_info(pid):
            return "强制停止失败，进程仍在运行"
        return "超时后已强制停止项目进程"
    return "已优雅停止"


def stop(values: dict[str, str], *, quiet: bool = False) -> int:
    _, state_file, _ = state_paths(values)
    state = load_state(state_file)
    if not state:
        if not quiet:
            print("[状态] 没有统一启动器创建的运行中服务。")
        state_file.unlink(missing_ok=True)
        return 0
    if Path(str(state.get("project_root") or "")).resolve() != PROJECT_ROOT:
        print("[错误] PID 文件不属于当前项目，拒绝停止。", file=sys.stderr)
        return 2
    failures = 0
    for entry in reversed(state.get("services", [])):
        result = terminate_entry(entry)
        if not quiet:
            print(f"[停止] {entry.get('name', '服务')}：{result}")
        if "跳过" in result or "拒绝" in result or "失败" in result:
            failures += 1
    remaining = [entry for entry in state.get("services", []) if entry.get("owned") and process_matches(entry)]
    if not remaining:
        state_file.unlink(missing_ok=True)
    else:
        state["services"] = remaining
        atomic_json(state_file, state)
    return 1 if failures or remaining else 0


def status(values: dict[str, str]) -> int:
    _, state_file, _ = state_paths(values)
    state = load_state(state_file)
    if not state:
        print("[状态] MedVision 未由统一启动器运行。")
        return 1
    running = 0
    for entry in state.get("services", []):
        if not entry.get("owned"):
            print(f"[外部] {entry.get('name')}：复用，不受停止脚本管理")
            continue
        alive = process_matches(entry)
        running += int(alive)
        print(f"[{'运行' if alive else '失效'}] {entry.get('name')}：PID {entry.get('pid')}")
    print(f"[PID] {state_file}")
    return 0 if running else 1


def check_command(values: dict[str, str], *, json_output: bool) -> int:
    checks, context = perform_checks(values, for_start=False)
    print_checks(checks)
    if json_output:
        payload = {"checked_at": utc_now(), "project_root": str(PROJECT_ROOT), "checks": [asdict(item) for item in checks]}
        data_root: Path = context["data_root"]
        output = configured_path(values, "PPGL_TEST_RESULTS_DIR", data_root / "test-results") / "windows-environment-check.json"
        try:
            atomic_json(output, payload)
            print(f"[结果] {output}")
        except OSError as exc:
            print(f"[提示] 无法写入检查结果：{type(exc).__name__}")
    return 1 if checks_failed(checks) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MedVision Windows 统一启停与环境检查")
    subparsers = parser.add_subparsers(dest="command")
    check_parser = subparsers.add_parser("check", help="检查环境，不加载 GPU 模型")
    check_parser.add_argument("--json", action="store_true", help="将脱敏结果写入外部 test-results")
    start_parser = subparsers.add_parser("start", help="后台启动 FastAPI、Vue 和可选 Ollama")
    start_parser.add_argument("--no-browser", action="store_true")
    start_parser.add_argument("--lan", action="store_true", help="仅让 Vue 监听 0.0.0.0")
    start_parser.add_argument("--startup-timeout", type=float, default=90.0)
    subparsers.add_parser("stop", help="只停止 PID 文件中身份匹配的项目进程")
    subparsers.add_parser("status", help="显示统一启动器创建的服务")
    args = parser.parse_args()
    command = args.command or "start"
    values = read_dotenv(ENV_FILE)
    if command == "check":
        return check_command(values, json_output=args.json)
    if command == "stop":
        return stop(values)
    if command == "status":
        return status(values)
    # Running ``python start_system.py`` (or double-clicking the .py file on a
    # machine where Python files are associated with this environment) is the
    # daily-use shortcut. Optional arguments only exist when the explicit
    # ``start`` subcommand was parsed, so provide the same safe defaults here.
    return start(
        values,
        no_browser=getattr(args, "no_browser", False),
        lan=getattr(args, "lan", False),
        timeout=max(10.0, getattr(args, "startup_timeout", 90.0)),
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[取消] 用户中断操作。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"[错误] {type(exc).__name__}：{exc}", file=sys.stderr)
        raise SystemExit(1)
