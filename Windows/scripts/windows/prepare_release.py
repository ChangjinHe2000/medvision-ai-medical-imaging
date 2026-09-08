"""Build a fresh Windows source distribution without private runtime assets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys

from audit_release import scan


ROOT = Path(__file__).resolve().parents[2]
ROOT_FILES = {
    'README.md', 'WEIGHTS_AND_DATA.md', 'environment.yml', 'requirements.txt',
    '.env.example', '.gitignore', '.node-version', 'start_system.py', 'start.cmd', 'stop.cmd',
}
TREES = ('ai-backend', 'frontend-vue-prototype', 'scripts/windows', 'tests')
DOC_FILES = ('UPSTREAM.md', 'ENVIRONMENT.md', 'RELEASE.md')
SKIP_DIRS = {
    '.git', '.idea', '.vscode', '.venv', 'venv', 'node_modules', 'dist', 'target',
    '__pycache__', '.pytest_cache', '.ruff_cache', '.mypy_cache', 'htmlcov', 'coverage',
    'data', 'dataset', 'datasets', 'models', 'checkpoints', 'uploads', 'cases', 'logs',
    'outputs', 'runs', 'jobs', 'runtime', 'test-results', 'secrets', 'credentials',
}
SOURCE_SUFFIXES = {'.py', '.ps1', '.cmd', '.md', '.json', '.js', '.mjs', '.vue', '.css',
                   '.html', '.svg', '.png', '.jpg', '.jpeg', '.yml', '.yaml', '.txt', '.sql', '.toml'}
SKIP_FILES = {
    'ai-backend/knowledge_base/evaluation/results.json',
    'frontend-vue-prototype/src/assets/hero.png',
}


def linked(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def selected_files(root: Path) -> list[Path]:
    result = [root / name for name in sorted(ROOT_FILES) if (root / name).is_file()]
    # Preserve actual license notices, never synthesize a license for upstream code.
    for file in root.iterdir():
        if file.is_file() and file.name.upper().split('.')[0] in {'LICENSE', 'NOTICE', 'COPYING'}:
            result.append(file)
    result.extend(root / 'docs' / name for name in DOC_FILES if (root / 'docs' / name).is_file())
    for tree in TREES:
        for parent, directories, names in os.walk(root / tree, followlinks=False):
            directories[:] = [name for name in directories if name not in SKIP_DIRS]
            for directory in directories:
                candidate = Path(parent) / directory
                if linked(candidate):
                    raise ValueError('Refusing linked source directory')
            for name in sorted(names):
                file = Path(parent) / name
                relative = file.relative_to(root).as_posix()
                if relative in SKIP_FILES or '/knowledge_base/documents/' in relative or '/knowledge_base/parsed/' in relative or '/knowledge_base/index/' in relative:
                    continue
                if name.startswith('.env') and name != '.env.example':
                    continue
                if name.startswith('.tmp_') or '.local.' in name or name.endswith(('.local', '.bak', '.dump')):
                    continue
                if file.suffix.lower() in SOURCE_SUFFIXES or name in {'.gitignore', '.env.example'} or name.upper().split('.')[0] in {'LICENSE', 'NOTICE', 'COPYING'}:
                    result.append(file)
    for file in result:
        if linked(file) or not file.resolve().is_relative_to(root.resolve()):
            raise ValueError('Refusing source outside project')
    return sorted(set(result))


def build(root: Path, output: Path) -> dict:
    root, output = root.resolve(), output.resolve()
    if output == root or output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError('Output must be a new directory outside the source tree')
    if output.exists():
        raise ValueError('Output exists; choose a new empty destination name')
    files = selected_files(root)
    for required in ROOT_FILES:
        if root / required not in files:
            raise ValueError('Missing required publication file: ' + required)
    output.mkdir(parents=True, exist_ok=False)
    manifest = []
    for file in files:
        relative = file.relative_to(root)
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest.append({'path': relative.as_posix(), 'bytes': target.stat().st_size, 'sha256': digest})
    audit = scan(output, 100)
    report = {
        'repository': 'medvision-ai-medical-imaging-main-windows',
        'audit': audit,
        'files': manifest,
        'publication_ready': False,
        'manual_checks': [
            '填写 Windows 版 GitHub 账号与维护者署名',
            '确认原作者/合作者授权与项目 LICENSE',
            '在全新 Windows 环境验证安装和自备权重的真实工作流',
        ],
    }
    (output / 'PUBLICATION_CHECK.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description='生成排除私有资产的 Windows 源码发布目录')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build(ROOT, args.output)
    except (ValueError, OSError) as exc:
        print('发布目录生成失败：' + str(exc), file=sys.stderr)
        return 2
    summary = report['audit']['summary']
    print(f"Copied {len(report['files'])} files; errors={summary['errors']}; warnings={summary['warnings']}")
    print('Source snapshot prepared; upstream permission and publication metadata still require confirmation.')
    return 0 if summary['errors'] == 0 and summary['warnings'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
