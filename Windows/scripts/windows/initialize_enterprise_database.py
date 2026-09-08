from __future__ import annotations

import argparse
from getpass import getpass
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "ai-backend"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from start_system import read_dotenv  # noqa: E402
from backend.config import load_settings  # noqa: E402
from backend.enterprise.service import EnterpriseService, EnterpriseServiceConfig  # noqa: E402


def load_project_environment() -> None:
    """Use the same local configuration as the Windows system launcher."""
    for name, value in read_dotenv(PROJECT_ROOT / ".env.local").items():
        # Explicit process variables remain the highest-priority override.
        os.environ.setdefault(name, value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="初始化 MedVision 全新企业数据库")
    parser.add_argument("--create-admin", action="store_true", help="数据库为空时同时创建初始管理员")
    parser.add_argument("--username", default="admin", help="初始管理员用户名")
    parser.add_argument("--display-name", default="系统管理员", help="初始管理员显示名称")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    load_project_environment()
    settings = load_settings()
    service = EnterpriseService(
        EnterpriseServiceConfig(
            database_url=settings.auth_database_url,
            session_hours=settings.auth_session_hours,
            session_cookie_name=settings.auth_session_cookie_name,
            csrf_cookie_name=settings.auth_csrf_cookie_name,
            cookie_secure=settings.auth_cookie_secure,
        )
    )
    try:
        service.initialize()
        print("企业数据库结构已初始化。")
        print(f"数据库连接：{settings.auth_database_url.split('@')[-1] if '@' in settings.auth_database_url else settings.auth_database_url}")
        if not args.create_admin:
            print("未创建账号。请重新运行并增加 --create-admin，通过命令行创建初始管理员。")
            return 0
        if service.count_users() != 0:
            print("数据库已经存在用户，拒绝重复创建初始管理员。")
            return 2
        password = getpass("请输入初始管理员密码（至少 12 个字符）：")
        confirmation = getpass("请再次输入密码：")
        if password != confirmation:
            print("两次密码不一致。")
            return 2
        user = service.create_user(
            username=args.username,
            display_name=args.display_name,
            password=password,
            role="admin",
            initial_setup=True,
        )
        print(f"初始管理员已创建：{user['username']}")
        return 0
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
