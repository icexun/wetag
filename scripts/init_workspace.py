"""新增业务线：建 schema、建角色、跑 migrations、初始化仓库目录。

用 asyncpg 而非 psql——本机不装 psql 也能跑，且能把 checksum 校验做进同一个进程。

用法::

    export PG_ADMIN_DSN=postgresql://wetag_admin:admin_pw@127.0.0.1:55432/wetag
    python scripts/init_workspace.py --workspace demo --password pw_demo --apply-org
    python scripts/init_workspace.py --workspace other --password pw_other
    python scripts/init_workspace.py --verify-version --workspace demo

纪律（§9.4-5）：

* 已发布迁移文件**只增不改**。本脚本比对 checksum，改过的老文件直接拒绝执行，
  并提示新增 ``0008_*.sql``。
* 迁移与版本台账写在**同一个事务**里，不会出现"跑了一半版本号没记上"。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import asyncpg  # noqa: E402

from app.platform.db.schema import (  # noqa: E402
    ORG_SCHEMA,
    render_migration,
    safe_identifier,
    ws_role,
    ws_schema,
)
from migrations.version import (  # noqa: E402
    EXPECTED_SCHEMA_VERSION,
    ORG_MIGRATIONS,
    WORKSPACE_MIGRATIONS,
    migration_checksum as _checksum,
)

MIGRATIONS_DIR = REPO_ROOT / "migrations"


def _version_of(filename: str) -> int:
    return int(filename.split("_", 1)[0])


def _read(filename: str) -> str:
    path = MIGRATIONS_DIR / filename
    if not path.exists():
        raise SystemExit(f"迁移文件缺失：{path}")
    return path.read_text(encoding="utf-8")


async def _ensure_org_schema_migration(conn: asyncpg.Connection) -> None:
    """裸库首次运行时 org.schema_migration 还不存在，先建最小形态。"""
    await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {ORG_SCHEMA}")
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS org.schema_migration (
            filename     text        NOT NULL,
            version      integer     NOT NULL,
            scope        text        NOT NULL,
            workspace_id text        NOT NULL DEFAULT '',
            checksum     text        NOT NULL,
            applied_at   timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (filename, workspace_id)
        )
        """
    )


async def _apply(
    conn: asyncpg.Connection,
    filename: str,
    *,
    scope: str,
    workspace_id: str,
    password: str | None,
) -> str:
    """跑一个迁移文件。返回 ``applied`` / ``skipped``。"""
    raw = _read(filename)
    checksum = _checksum(raw)

    recorded = await conn.fetchrow(
        "SELECT checksum FROM org.schema_migration WHERE filename=$1 AND workspace_id=$2",
        filename,
        workspace_id,
    )
    if recorded is not None:
        if recorded["checksum"] != checksum:
            raise SystemExit(
                f"❌ {filename} 已发布但内容被修改（checksum 不符）。\n"
                f"   已发布迁移只增不改——请新增 migrations/0008_*.sql 做增量变更。"
            )
        return "skipped"

    sql = render_migration(raw, workspace_id) if scope == "workspace" else raw

    # 迁移 + 版本台账写在同一事务：不会出现跑了一半版本号没记上
    async with conn.transaction():
        if scope == "workspace":
            # 0007 通过会话参数拿 schema/role/口令——口令不写进 SQL 文件
            await conn.execute(
                "SELECT set_config('wetag.ws_schema', $1, true),"
                "       set_config('wetag.ws_role',   $2, true),"
                "       set_config('wetag.ws_password', $3, true)",
                ws_schema(workspace_id),
                ws_role(workspace_id),
                password or "",
            )
        await conn.execute(sql)
        await conn.execute(
            """
            INSERT INTO org.schema_migration
                   (filename, version, scope, workspace_id, checksum)
            VALUES ($1, $2, $3, $4, $5)
            """,
            filename,
            _version_of(filename),
            scope,
            workspace_id,
            checksum,
        )
    return "applied"


def has_statements(sql: str) -> bool:
    """剔除行注释与空白后仍含语句。占位空脚本执行时为 no-op，直接跳过。"""
    body = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    return bool(body.strip())


async def apply_migrations(
    conn: asyncpg.Connection,
    workspace: str,
    role_password: str | None = None,
) -> None:
    """对单条业务线应用全部 workspace 迁移。幂等（checksum 已登记则跳过）。"""
    workspace = safe_identifier(workspace)
    for name in WORKSPACE_MIGRATIONS:
        await _apply(
            conn,
            name,
            scope="workspace",
            workspace_id=workspace,
            password=role_password or f"{ws_role(workspace)}_local",
        )
    await _grant_existing_tables(conn, workspace)


async def _grant_existing_tables(conn: asyncpg.Connection, workspace_id: str) -> None:
    """0007 若在建表前跑过，补授已有表的权限（幂等，可重复执行）。"""
    # set_config(..., is_local=true) 必须在事务内才有效
    async with conn.transaction():
        await conn.execute(
            "SELECT set_config('wetag.ws_schema', $1, true),"
            "       set_config('wetag.ws_role', $2, true)",
            ws_schema(workspace_id),
            ws_role(workspace_id),
        )
        await conn.execute(_read("0007_roles_and_grants.sql"))


async def verify_version(conn: asyncpg.Connection, workspace_id: str | None) -> int:
    rows = await conn.fetch(
        """
        SELECT workspace_id, max(version) AS version, count(*) AS files
          FROM org.schema_migration
         WHERE scope = 'workspace'
           AND ($1::text IS NULL OR workspace_id = $1)
         GROUP BY workspace_id
         ORDER BY workspace_id
        """,
        workspace_id,
    )
    expected_files = len(WORKSPACE_MIGRATIONS)
    failed = 0
    if not rows:
        print("⚠️  没有任何业务线迁移记录")
        return 1
    for r in rows:
        ok = r["version"] == EXPECTED_SCHEMA_VERSION and r["files"] == expected_files
        mark = "✅" if ok else "❌"
        print(
            f"{mark} workspace={r['workspace_id']:<8} version={r['version']}"
            f"/{EXPECTED_SCHEMA_VERSION} files={r['files']}/{expected_files}"
        )
        failed += 0 if ok else 1
    return failed


async def main() -> int:
    ap = argparse.ArgumentParser(description="WeTag 业务线初始化 / 迁移运行器")
    ap.add_argument("--workspace", help="业务线 id，如 demo")
    ap.add_argument("--password", help="业务线 DB 角色口令（首次必填）")
    ap.add_argument("--apply-org", action="store_true", help="同时跑 org schema 迁移")
    ap.add_argument("--verify-version", action="store_true", help="只校验版本，不改库")
    ap.add_argument("--dsn", default=os.environ.get("PG_ADMIN_DSN", ""))
    args = ap.parse_args()

    if not args.dsn:
        raise SystemExit("需要 PG_ADMIN_DSN（或 --dsn）。生产业务进程不应配置它。")

    conn = await asyncpg.connect(args.dsn, timeout=10)
    try:
        if args.verify_version:
            return await verify_version(conn, args.workspace)

        await _ensure_org_schema_migration(conn)

        if args.apply_org:
            for f in ORG_MIGRATIONS:
                state = await _apply(
                    conn, f, scope="org", workspace_id="", password=None
                )
                print(f"  org        {f:<34} {state}")

        if not args.workspace:
            print("未指定 --workspace，仅处理 org 迁移")
            return 0

        ws = safe_identifier(args.workspace, where="init.workspace")
        print(f"\nworkspace={ws} schema={ws_schema(ws)} role={ws_role(ws)}")
        for f in WORKSPACE_MIGRATIONS:
            state = await _apply(
                conn, f, scope="workspace", workspace_id=ws, password=args.password
            )
            print(f"  {ws:<10} {f:<34} {state}")

        # 建表在 0007 之后新增的情况下补授权（0007 的 GRANT ON ALL TABLES 只覆盖当时已有的表）
        await _grant_existing_tables(conn, ws)
        print(f"  {ws:<10} grants re-applied")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
