"""启动期 schema 版本校验（``app/platform/db/migration_check.py``，§9.4-5）。

独立部署的 DB 不能靠删文件重建，所以版本不匹配的唯一正确反应是**拒绝启动**。
这里守两件事：

1. **不匹配必须 ``SystemExit``**，不能只打日志继续跑。继续跑的后果是进程带着
   缺表/缺列的 schema 服务流量，第一条 SQL 才炸——而那时它已经收下了任务、
   发过消息、写过审计。启动期硬失败是唯一能把故障挡在副作用之前的位置。
2. **版本号对不代表迁移全跑过**。``version=7`` 也可能是手工只跑了 0007 而漏了
   0003，此时最大版本号照样是 7。所以文件数也要比——这是这个校验里唯一不显然
   的一条，也是最容易在"修一下线上"之后留下的状态。
"""


from typing import Any

import pytest

from app.platform.db.migration_check import assert_schema_version
from migrations.version import EXPECTED_SCHEMA_VERSION, WORKSPACE_MIGRATIONS

_FULL = len(WORKSPACE_MIGRATIONS)


class FakeDatabase:
    """只提供 ``migration_check`` 用到的两个面：``workspace_id`` 与 ``fetchrow``。"""

    def __init__(self, version: int | None, applied: int = _FULL, *, workspace_id: str = "alpha") -> None:
        self.workspace_id = workspace_id
        self._row = None if version is None else {"version": version, "applied": applied}
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any):
        self.queries.append((sql, args))
        return self._row


async def test_exact_match_passes_and_returns_the_version() -> None:
    db = FakeDatabase(EXPECTED_SCHEMA_VERSION, _FULL)
    assert await assert_schema_version(db) == EXPECTED_SCHEMA_VERSION


async def test_a_lagging_schema_refuses_to_start() -> None:
    """落后一版就拒绝启动，且错误信息里带上修复命令。

    这条消息是运维在容器日志里看到的**唯一**线索，缺了 workspace 名或命令名，
    多业务线部署下没法知道该对哪个业务线跑什么。
    """
    db = FakeDatabase(EXPECTED_SCHEMA_VERSION - 1, _FULL - 1)
    with pytest.raises(SystemExit) as exc:
        await assert_schema_version(db)

    msg = str(exc.value)
    assert "alpha" in msg
    assert "init_workspace.py" in msg
    assert str(EXPECTED_SCHEMA_VERSION) in msg


async def test_a_fresh_database_refuses_to_start() -> None:
    """台账为空（version 0）不是"新装，先跑起来再说"，同样拒绝。"""
    with pytest.raises(SystemExit):
        await assert_schema_version(FakeDatabase(0, 0))


async def test_missing_row_is_treated_as_version_zero() -> None:
    """``fetchrow`` 返回 None（表里没有本业务线的记录）也必须拒绝，而不是崩在 None 下标上。"""
    with pytest.raises(SystemExit):
        await assert_schema_version(FakeDatabase(None))


async def test_right_version_but_a_skipped_file_still_refuses() -> None:
    """``version=7`` 但只跑了 5 个文件 ⇒ 拒绝。

    这是"手工修了一下线上"最典型的残留：``max(version)`` 照样是 7，只比版本号
    的实现会放行，而 0003 建的表压根不存在。
    """
    db = FakeDatabase(EXPECTED_SCHEMA_VERSION, _FULL - 1)
    with pytest.raises(SystemExit) as exc:
        await assert_schema_version(db)
    assert f"/{_FULL - 1} 个文件" in str(exc.value)


async def test_more_files_than_expected_also_refuses() -> None:
    """跑过的文件比期望的多 ⇒ 代码回滚了但 DB 没有。同样不该放行。"""
    with pytest.raises(SystemExit):
        await assert_schema_version(FakeDatabase(EXPECTED_SCHEMA_VERSION, _FULL + 1))


async def test_a_database_ahead_of_the_code_also_refuses() -> None:
    """DB 已经是 8 而代码只认到 7 ⇒ 拒绝，比较必须是 ``==`` 而不是 ``>=``。

    这是回滚的标准形态：代码退回上一版，DB 没跟着退。而新迁移可能已经删了列或
    改了约束，旧代码带着它启动，第一条写入才炸——且此时 DB 里已经是不可逆的
    新结构。``>=`` 的实现在这一幕里完全静默。
    """
    with pytest.raises(SystemExit):
        await assert_schema_version(
            FakeDatabase(EXPECTED_SCHEMA_VERSION + 1, _FULL)
        )


async def test_exit_can_be_suppressed_for_diagnostics() -> None:
    """``--verify-version`` 一类脚本要能读到实际版本号再自己决定退出码。"""
    db = FakeDatabase(3, 3)
    assert await assert_schema_version(db, exit_on_mismatch=False) == 3


async def test_the_check_is_scoped_to_this_workspace_only() -> None:
    """查询必须同时按 ``scope='workspace'`` 和本业务线过滤。

    漏掉 workspace_id 过滤的后果很隐蔽：业务线 beta 已迁移到 7，业务线 alpha 一行都
    没有，而 ``max(version)`` 取到的是全表最大值 7——alpha 会带着空 schema 启动成功。
    """
    db = FakeDatabase(EXPECTED_SCHEMA_VERSION, _FULL, workspace_id="alpha")
    await assert_schema_version(db)

    sql, args = db.queries[0]
    assert "workspace_id = $1" in sql
    assert "scope = 'workspace'" in sql
    assert args == ("alpha",)


# --------------------------------------------------------------------------
# covers: TC-004
# --------------------------------------------------------------------------


import asyncpg

from app.platform.db.migration_check import (
    SchemaVersionMismatch,
    verify_schema_version,
)


@pytest.fixture()
async def org_conn(admin_dsn, org_ready):
    """给出一条能随意改写版本台账的管理连接，退出时恢复原样。

    台账表是 ``org.schema_migration``（``org.schema_version`` 已并入其中），
    主键是 (filename, workspace_id)，所以造漂移要插具体的迁移文件行。
    """
    conn = await asyncpg.connect(admin_dsn)
    saved = await conn.fetch("SELECT * FROM org.schema_migration")
    yield conn
    await conn.execute("DELETE FROM org.schema_migration")
    for row in saved:
        await conn.execute(
            """
            INSERT INTO org.schema_migration
                   (filename, version, scope, workspace_id, checksum, applied_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            row["filename"], row["version"], row["scope"],
            row["workspace_id"], row["checksum"], row["applied_at"],
        )
    await conn.close()


async def _record(conn, version: int) -> None:
    await conn.execute(
        """
        INSERT INTO org.schema_migration
               (filename, version, scope, workspace_id, checksum)
        VALUES ($1, $2, 'workspace', 'alpha', 'x')
        """,
        f"{version:04d}_test.sql", version,
    )


async def test_version_match_passes(org_conn):
    await org_conn.execute("DELETE FROM org.schema_migration")
    await _record(org_conn, EXPECTED_SCHEMA_VERSION)
    assert await verify_schema_version(org_conn) == EXPECTED_SCHEMA_VERSION


async def test_version_drift_refused_with_diff(org_conn):
    await org_conn.execute("DELETE FROM org.schema_migration")
    await _record(org_conn, EXPECTED_SCHEMA_VERSION - 1)
    with pytest.raises(SchemaVersionMismatch) as exc_info:
        await verify_schema_version(org_conn)
    msg = str(exc_info.value)
    assert str(EXPECTED_SCHEMA_VERSION) in msg
    assert str(EXPECTED_SCHEMA_VERSION - 1) in msg  # 输出期望与实际差异


async def test_no_migration_applied_refused(org_conn):
    await org_conn.execute("DELETE FROM org.schema_migration")
    with pytest.raises(SchemaVersionMismatch) as exc_info:
        await verify_schema_version(org_conn)
    assert exc_info.value.actual is None
