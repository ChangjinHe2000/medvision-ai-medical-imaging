from __future__ import annotations

import os
from pathlib import Path
import sys

import psycopg


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from start_system import read_dotenv

MIGRATION = PROJECT_ROOT / "ai-backend" / "migrations" / "20260829_01_pgvector_knowledge.sql"


def main() -> int:
    for name, value in read_dotenv(PROJECT_ROOT / ".env.local").items():
        os.environ.setdefault(name, value)
    database_url = os.environ.get("PPGL_RAG_DATABASE_URL", "").strip()
    if not database_url:
        print("错误：请先在 .env.local 配置 PPGL_RAG_DATABASE_URL。")
        return 2
    database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    sql = MIGRATION.read_text(encoding="utf-8")
    try:
        with psycopg.connect(database_url, connect_timeout=10) as connection:
            connection.execute(sql)
    except Exception as exc:
        print(f"知识库数据库初始化失败：{type(exc).__name__}")
        return 1
    print("PostgreSQL + pgvector 知识库结构初始化完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
