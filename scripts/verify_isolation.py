"""§10.2 第 0 项验收：用 workspace A 的角色访问 workspace B，期望权限错误。连真实 PG 实例运行。

用法::

    PG_ADMIN_DSN=postgresql://wetag_admin:admin_pw@127.0.0.1:55432/wetag \\
      python scripts/verify_isolation.py --role-a demo --role-b other \\
      --password-a pw_mx

任一条 FAIL 即 ``exit(1)``。这个脚本也是 Demo 现场的隔离证明——它证明的是
**权限层**的隔离，不是代码里的 if 判断：即使业务代码写错 schema 名，DB 也会拒绝。
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

from app.platform.db.schema import ws_role, ws_schema  # noqa: E402

# 期望被拒绝时可接受的 SQLSTATE
_DENIED = {
    "42501",  # insufficient_privilege
    "3F000",  # invalid_schema_name（USAGE 都没有时 schema 直接不可见）
    "42P01",  # undefined_table（同上，表在不可见 schema 内）
}


class Checker:
    def __init__(self) -> None:
        self.failed = 0

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        mark = "✅ PASS" if ok else "❌ FAIL"
        print(f"{mark}  {name}" + (f"   [{detail}]" if detail else ""))
        if not ok:
            self.failed += 1

    async def allow(self, conn: asyncpg.Connection, name: str, sql: str, *args) -> None:
        """期望成功。"""
        try:
            await conn.execute(sql, *args)
            self.record(name, True)
        except Exception as exc:  # noqa: BLE001
            self.record(name, False, f"{type(exc).__name__}: {exc}")

    async def deny(self, conn: asyncpg.Connection, name: str, sql: str, *args) -> None:
        """期望被 DB **权限层**拒绝。执行成功 = 隔离失效。"""
        try:
            await conn.execute(sql, *args)
        except asyncpg.PostgresError as exc:
            code = getattr(exc, "sqlstate", "")
            self.record(name, code in _DENIED, f"SQLSTATE={code}")
        except Exception as exc:  # noqa: BLE001
            self.record(name, False, f"非 PG 错误：{type(exc).__name__}: {exc}")
        else:
            self.record(name, False, "语句竟然执行成功——隔离失效")


async def run(args: argparse.Namespace) -> int:
    ws_a, ws_b = args.role_a, args.role_b
    schema_a, schema_b = ws_schema(ws_a), ws_schema(ws_b)
    role_a = ws_role(ws_a)

    host_dsn = args.dsn
    # 用 A 的业务线角色连（而非 admin）——这才是生产进程的真实身份
    dsn_a = _swap_credentials(host_dsn, role_a, args.password_a)

    print(f"\n=== 隔离验收：以 {role_a} 身份，目标 {schema_a}（自己）/ {schema_b}（他人）===\n")
    conn = await asyncpg.connect(dsn_a, timeout=10)
    c = Checker()
    try:
        # --- 0. 连接级默认：search_path 不含 public ---
        sp = await conn.fetchval("SHOW search_path")
        c.record(
            "search_path 不含 public",
            "public" not in sp.split(","),
            f"search_path={sp}",
        )
        c.record(
            f"对他人 schema 无 USAGE（{schema_b}）",
            not await conn.fetchval(
                "SELECT has_schema_privilege($1, $2, 'USAGE')", role_a, schema_b
            ),
        )
        c.record(
            "对 public 无 CREATE",
            not await conn.fetchval(
                "SELECT has_schema_privilege($1, 'public', 'CREATE')", role_a
            ),
        )

        # --- 1. 自己 schema：可读可写 ---
        await c.allow(
            conn,
            f"自己 schema 可读（{schema_a}.requirement）",
            f"SELECT count(*) FROM {schema_a}.requirement WHERE workspace_id = $1",
            ws_a,
        )
        await c.allow(
            conn,
            "自己 schema 可写（INSERT requirement）",
            f"""
            INSERT INTO {schema_a}.requirement
                   (workspace_id, tapd_id, title, status, status_category)
            VALUES ($1, '__isolation_probe__', 'probe', 'ACTIVE', 'ACTIVE')
            ON CONFLICT (workspace_id, tapd_id) DO NOTHING
            """,
            ws_a,
        )
        # §9.4-1：APScheduler 要在自己 schema 建 apscheduler_jobs，故 CREATE 必须给
        await c.allow(
            conn,
            "自己 schema 可建表（APScheduler 需要，0007 显式接受）",
            f"CREATE TABLE IF NOT EXISTS {schema_a}.isolation_probe_tbl (id int)",
        )

        # --- 2. 跨 schema 三条全拒 ---
        await c.deny(
            conn,
            f"跨 schema 读被拒（{schema_b}.requirement）",
            f"SELECT count(*) FROM {schema_b}.requirement",
        )
        await c.deny(
            conn,
            f"跨 schema 写被拒（{schema_b}.requirement）",
            f"""
            INSERT INTO {schema_b}.requirement
                   (workspace_id, tapd_id, title, status, status_category)
            VALUES ('{ws_b}', '__evil__', 'evil', 'ACTIVE', 'ACTIVE')
            """,
        )
        # CREATE 权限只在自己 schema 内有效——这是 R11 的证明
        await c.deny(
            conn,
            f"跨 schema 建表被拒（{schema_b}.evil）",
            f"CREATE TABLE {schema_b}.evil (id int)",
        )

        # --- 3. org schema：只读 + chat_ownership 仅 INSERT ---
        await c.allow(conn, "org 可 SELECT", "SELECT count(*) FROM org.chat_ownership")
        await c.allow(
            conn,
            "org.chat_ownership 可 INSERT（认领群）",
            """
            INSERT INTO org.chat_ownership (chat_id, workspace_id, bot_id)
            VALUES ($1, $2, 'bot_probe')
            ON CONFLICT (chat_id) DO NOTHING
            """,
            f"__probe_{ws_a}__",
            ws_a,
        )
        # 认领过的群不能被改走或删掉——§4.2「一个群只归一条业务线」靠这个才是硬的
        await c.deny(
            conn,
            "org.chat_ownership UPDATE 被拒（不能抢别人的群）",
            "UPDATE org.chat_ownership SET workspace_id = $1 WHERE chat_id <> ''",
            ws_a,
        )
        await c.deny(
            conn,
            "org.chat_ownership DELETE 被拒",
            "DELETE FROM org.chat_ownership WHERE chat_id <> ''",
        )
        await c.deny(
            conn,
            "org.schema_migration 篡改被拒",
            "UPDATE org.schema_migration SET version = 999",
        )

        # --- 4. 清理探针 ---
        await conn.execute(
            f"DELETE FROM {schema_a}.requirement "
            f"WHERE workspace_id = $1 AND tapd_id = '__isolation_probe__'",
            ws_a,
        )
        await conn.execute(f"DROP TABLE IF EXISTS {schema_a}.isolation_probe_tbl")
    finally:
        await conn.close()

    print()
    if c.failed:
        print(f"❌ 隔离验收未通过：{c.failed} 项失败")
        return 1
    print("✅ 隔离验收全部通过（§10.2 第 0 项）")
    return 0


def _swap_credentials(dsn: str, user: str, password: str) -> str:
    """把 DSN 里的用户名口令换成业务线角色。避免让调用方手写第二条 DSN。"""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(dsn)
    host = parts.hostname or "127.0.0.1"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{user}:{password}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def role_dsn(base_dsn: str, workspace: str, password: str) -> str:
    """由管理连接串派生业务线角色连接串。"""
    return _swap_credentials(base_dsn, ws_role(workspace), password)


def main() -> int:
    ap = argparse.ArgumentParser(description="WeTag 业务线隔离验收")
    ap.add_argument("--role-a", default="demo", help="发起方业务线")
    ap.add_argument("--role-b", default="other", help="被访问方业务线（应全部拒绝）")
    ap.add_argument("--password-a", default=os.environ.get("PG_WS_PASSWORD", "pw_mx"))
    ap.add_argument("--dsn", default=os.environ.get("PG_ADMIN_DSN", ""))
    args = ap.parse_args()
    if not args.dsn:
        raise SystemExit("需要 PG_ADMIN_DSN（或 --dsn）提供主机与库名")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
