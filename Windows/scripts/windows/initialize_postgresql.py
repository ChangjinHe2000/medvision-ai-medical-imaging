from __future__ import annotations

import argparse
from getpass import getpass
import sys

import psycopg
from psycopg import sql


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a fresh MedVision PostgreSQL database and pgvector schema."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--admin-user", default="postgres")
    parser.add_argument("--database", default="medvision")
    parser.add_argument("--app-role", default="medvision_app")
    return parser.parse_args()


def prompt_password(label: str, *, confirm: bool = False) -> str:
    password = getpass(label)
    if not password:
        raise ValueError("Password cannot be empty.")
    if confirm:
        repeated = getpass("Repeat the new application database password: ")
        if password != repeated:
            raise ValueError("The application database passwords do not match.")
        if len(password) < 16:
            raise ValueError("The application database password must contain at least 16 characters.")
    return password


def main() -> int:
    args = parse_args()
    admin_password = prompt_password("PostgreSQL administrator password: ")
    app_password = prompt_password(
        "New MedVision application database password: ",
        confirm=True,
    )

    admin_connection = psycopg.connect(
        host=args.host,
        port=args.port,
        dbname="postgres",
        user=args.admin_user,
        password=admin_password,
        connect_timeout=5,
        autocommit=True,
    )
    try:
        with admin_connection.cursor() as cursor:
            role_exists = cursor.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s",
                (args.app_role,),
            ).fetchone()
            if role_exists:
                raise RuntimeError(
                    "The requested application role already exists; refusing to change its password automatically."
                )

            database_exists = cursor.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (args.database,),
            ).fetchone()
            if database_exists:
                raise RuntimeError(
                    "The requested database already exists; refusing to reuse or overwrite it automatically."
                )

            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(args.app_role),
                    sql.Literal(app_password),
                )
            )
            try:
                cursor.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(args.database),
                        sql.Identifier(args.app_role),
                    )
                )
            except Exception:
                cursor.execute(
                    sql.SQL("DROP ROLE {}").format(sql.Identifier(args.app_role))
                )
                raise
    finally:
        admin_connection.close()

    database_connection = psycopg.connect(
        host=args.host,
        port=args.port,
        dbname=args.database,
        user=args.admin_user,
        password=admin_password,
        connect_timeout=5,
        autocommit=True,
    )
    try:
        with database_connection.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cursor.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS rag AUTHORIZATION {}").format(
                    sql.Identifier(args.app_role)
                )
            )
    finally:
        database_connection.close()

    verification_connection = psycopg.connect(
        host=args.host,
        port=args.port,
        dbname=args.database,
        user=args.app_role,
        password=app_password,
        connect_timeout=5,
    )
    try:
        with verification_connection.cursor() as cursor:
            vector_version = cursor.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
            schema_ready = cursor.execute(
                "SELECT has_schema_privilege(current_user, 'rag', 'USAGE')"
            ).fetchone()
    finally:
        verification_connection.close()

    if not vector_version or not schema_ready or not schema_ready[0]:
        raise RuntimeError("Database verification did not confirm pgvector and rag schema access.")

    print("MedVision PostgreSQL bootstrap completed.")
    print(f"Database: {args.database}")
    print(f"Application role: {args.app_role}")
    print(f"pgvector: {vector_version[0]}")
    print("No password was written to disk. Add it to the ignored local environment file manually.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"Initialization failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
