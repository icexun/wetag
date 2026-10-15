"""启动期 schema 版本校验（§9.4-5）。

独立部署的 DB **不能**靠删文件重建，所以版本不匹配时直接拒绝启动，
而不是尝试自动迁移——自动迁移在多进程同时启动时是灾难。

读的是 ``org.schema_migration``：业务线角色对 org 有 SELECT，用业务线自己的
连接就能查到自己该跑到哪一版。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.platform.db.pool import Database
from migrations.version import (
    EXPECTED_SCHEMA_VERSION,
    WORKSPACE_MIGRATIONS,
    expected_checksums,
)

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

_SQL_APPLIED_VERSION = """
SELECT coalesce(max(version), 0) AS version, count(*) AS applied
  FROM org.schema_migration
 WHERE scope = 'workspace' AND workspace_id = $1
"""


async def assert_schema_version(db: Database, *, exit_on_mismatch: bool = True) -> int:
    """校验本业务线 schema 已迁移到期望版本。

    :returns: 实际版本号。
    :raises SystemExit: 版本不匹配且 ``exit_on_mismatch``（默认）。
    """
    row = await db.fetchrow(_SQL_APPLIED_VERSION, db.workspace_id)
    actual = int(row["version"]) if row else 0
    applied = int(row["applied"]) if row else 0
    expected_count = len(WORKSPACE_MIGRATIONS)

    ok = actual == EXPECTED_SCHEMA_VERSION and applied == expected_count
    if ok:
        logger.info(
            "schema version ok workspace_id=%s version=%d files=%d",
            db.workspace_id,
            actual,
            applied,
        )
        return actual

    msg = (
        f"schema 版本不匹配：workspace_id={db.workspace_id} "
        f"期望 version={EXPECTED_SCHEMA_VERSION}/{expected_count} 个文件，"
        f"实际 version={actual}/{applied} 个文件。"
        f"请先执行 scripts/init_workspace.py --workspace {db.workspace_id}"
    )
    logger.critical(msg)
    if exit_on_mismatch:
        raise SystemExit(msg)
    return actual


class SchemaVersionMismatch(RuntimeError):
    """结构漂移：期望版本与库内版本不一致，进程不得启动。"""

    def __init__(self, expected: int, actual: int | None) -> None:
        self.expected = expected
        self.actual = actual
        detail = "库内无任何已应用迁移" if actual is None else f"库内已应用至 v{actual}"
        super().__init__(
            f"schema 版本漂移: 期望 v{expected}，{detail}。"
            "请先执行 scripts/init_workspace.py 应用缺失迁移后再启动。"
        )


class SchemaChecksumMismatch(RuntimeError):
    """内容漂移：版本号对得上，但建库时用的迁移文件与磁盘上的不是同一份。

    版本号相同不代表结构相同。某个 workspace 的 0002 就是这么漏过去的——文件被就地重写，
    序号还是 2，``max(version)`` 校验一路绿灯，而库里躺着老形状的表，直到第一次
    查询才炸在 ``column "priority" does not exist``。校验发生在启动，失败就得在
    启动暴露，不能等到最需要它出结论的那一刻。
    """

    def __init__(self, workspace_id: str | None, drifted: dict[str, tuple[str, str]]) -> None:
        self.workspace_id = workspace_id
        self.drifted = drifted
        lines = "\n".join(
            f"  {name}: 库内 {recorded[:12]}… ≠ 文件 {ondisk[:12]}…"
            for name, (recorded, ondisk) in sorted(drifted.items())
        )
        who = f"workspace_id={workspace_id} " if workspace_id else ""
        super().__init__(
            f"schema 内容漂移 {who}（版本号一致但迁移文件内容不同）:\n{lines}\n"
            "已发布迁移只增不改。若确认库内为空可重建，执行 "
            "scripts/repair_0002_drift.py --workspace <ws>；否则新增迁移文件对齐。"
        )


async def _assert_checksums(
    conn: "asyncpg.Connection", workspace_id: str | None
) -> None:
    """比对台账里记的 checksum 与磁盘文件。缺行不在此处报——版本校验已覆盖。"""
    if workspace_id:
        rows = await conn.fetch(
            "SELECT filename, checksum FROM org.schema_migration "
            "WHERE scope = 'workspace' AND workspace_id = $1",
            workspace_id,
        )
    else:
        rows = await conn.fetch(
            "SELECT DISTINCT filename, checksum FROM org.schema_migration "
            "WHERE scope = 'workspace'"
        )

    want = expected_checksums(WORKSPACE_MIGRATIONS)
    drifted: dict[str, tuple[str, str]] = {}
    for row in rows:
        name = row["filename"]
        ondisk = want.get(name)
        if ondisk is not None and row["checksum"] != ondisk:
            drifted[name] = (row["checksum"], ondisk)
    if drifted:
        raise SchemaChecksumMismatch(workspace_id, drifted)


async def verify_schema_version(
    conn: "asyncpg.Connection",
    expected: int = EXPECTED_SCHEMA_VERSION,
    *,
    workspace_id: str | None = None,
    verify_checksums: bool = True,
) -> int:
    """裸连接版的版本校验，供 ``init_workspace`` 与集成测试使用。

    与 :func:`assert_schema_version` 的区别只在入参：那个走业务线连接池并按
    ``workspace_id`` 分组统计，这个拿一条裸连接查全局最大版本，用在还没有池的
    场合（建库脚本、测试夹具）。

    版本号过关后再比 checksum（``verify_checksums``）：序号相同但文件被改过的库
    结构其实对不上，只比版本号发现不了——见 :class:`SchemaChecksumMismatch`。
    给了 ``workspace_id`` 就只查这条业务线，同库里别人的漂移不牵连本进程启动。
    """
    # 只比 workspace 迁移：org 迁移（scope='org'）序号可能超过 workspace 最大
    # 版本号（如 0010 > v9），按设计不抬 EXPECTED_SCHEMA_VERSION，不能进 max()
    actual: int | None = await conn.fetchval(
        "SELECT max(version) FROM org.schema_migration WHERE scope = 'workspace'"
    )
    if actual != expected:
        raise SchemaVersionMismatch(expected, actual)
    if verify_checksums:
        await _assert_checksums(conn, workspace_id)
    return actual


__all__ = [
    "SchemaChecksumMismatch",
    "SchemaVersionMismatch",
    "assert_schema_version",
    "verify_schema_version",
]
