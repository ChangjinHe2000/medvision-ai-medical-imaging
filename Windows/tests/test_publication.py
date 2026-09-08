from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
from unittest.mock import patch, MagicMock

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts' / 'windows'
sys.path.insert(0, str(SCRIPTS))
import prepare_release
from audit_release import scan


def test_release_selection_excludes_private_files_and_retains_templates(tmp_path):
    entries = [
        '.env.local', '.env.example', 'README.md', 'ai-backend/backend/main.py',
        'ai-backend/backend/.env.local.bak', 'ai-backend/backend/secret.key',
        'ai-backend/backend/patient.nii.gz', 'ai-backend/backend/model.pth',
        'ai-backend/backend/database.db-wal', 'ai-backend/backend/.tmp_maintenance.py',
        'ai-backend/uploads/private.txt', 'ai-backend/knowledge_base/documents/private.txt',
        'frontend-vue-prototype/node_modules/dependency/index.js',
        'frontend-javaweb/boot.txt', 'frontend-vue-prototype/src/main.js',
    ]
    for entry in entries:
        path = tmp_path / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture', encoding='utf-8')
    selected = {p.relative_to(tmp_path).as_posix() for p in prepare_release.selected_files(tmp_path)}
    assert selected == {'.env.example', 'README.md', 'ai-backend/backend/main.py', 'frontend-vue-prototype/src/main.js'}


def test_release_refuses_existing_or_nested_output(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    existing = tmp_path / 'already-present'
    existing.mkdir()
    for output in (root, root / 'nested', tmp_path, existing):
        with pytest.raises(ValueError):
            prepare_release.build(root, output)


def test_audit_finds_old_account_notes_and_never_echoes_secrets(tmp_path):
    password = 'fixture-private-password'
    (tmp_path / 'old-config.txt').write_text(
        'postgresql+psycopg://app:' + password + '@localhost/db\n'
        '/Users/example/project\n' + 'wx' + 'a' * 16 + '\n' + '139' + '12345678',
        encoding='utf-8',
    )
    findings = scan(tmp_path, 100)['findings']
    assert {'credential_url', 'personal_home_path', 'wechat_appid', 'phone_like_value'} <= {v['kind'] for v in findings}
    assert password not in str(findings)


def test_rag_initializer_loads_local_file_without_overriding_process_env(tmp_path):
    spec = importlib.util.spec_from_file_location('rag_init_test', SCRIPTS / 'initialize-rag-database.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    migration = tmp_path / 'migration.sql'
    migration.write_text('SELECT 1;', encoding='utf-8')
    connection = MagicMock()
    with (
        patch.dict(os.environ, {'PPGL_RAG_DATABASE_URL': 'postgresql://explicit/test'}, clear=True),
        patch.object(module, 'read_dotenv', return_value={'PPGL_RAG_DATABASE_URL': 'postgresql://local/test'}),
        patch.object(module, 'MIGRATION', migration),
        patch.object(module.psycopg, 'connect', return_value=connection) as connect,
    ):
        assert module.main() == 0
        connect.assert_called_once_with('postgresql://explicit/test', connect_timeout=10)
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(module, 'read_dotenv', return_value={'PPGL_RAG_DATABASE_URL': 'postgresql://local/test'}),
        patch.object(module, 'MIGRATION', migration),
        patch.object(module.psycopg, 'connect', return_value=connection) as connect,
    ):
        assert module.main() == 0
        connect.assert_called_once_with('postgresql://local/test', connect_timeout=10)
