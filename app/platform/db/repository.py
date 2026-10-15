"""Repository 基类：把"每条 SQL 都带 workspace_id"变成机械可查的纪律。

D-4 的隔离是三层叠加，缺一层都不够：

1. **权限层**（0007）：业务线角色对他人 schema 无任何权限。
2. **连接层**（pool.py）：``search_path`` 只有自己的 schema + org，不含 public。
3. **语句层**（本文件）：``_bind()`` 强制 SQL 文本里出现 ``workspace_id``；
   ``_guard_rows()`` 对返回行做**行级**二次校验。

第 3 层看似冗余——但 org schema 里的表是所有业务线共享的，权限层拦不住
"读到别人的 chat_ownership"。行级校验是唯一能覆盖这种情况的一层。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Iterable, Sequence

import asyncpg

from app.contracts.errors import IsolationViolation, StorageUnavailable
from app.platform.db.pool import Database
from app.platform.db.schema import safe_identifier

logger = logging.getLogger(__name__)


class Repository:
    """所有 DAO 的基类。子类通过 ``self.ws`` 拿到本业务线 id。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    @property
    def ws(self) -> str:
        return self.db.workspace_id

    # -- 语句层护栏 -------------------------------------------------------

    def _bind(self, sql: str) -> str:
        """校验 SQL 带了 workspace_id 条件。

        这是**文本级**检查，故意做得粗糙但无法绕过：任何忘记加过滤条件的查询
        在第一次跑到时就会抛异常，而不是安静地返回别人的数据。

        确实需要不带过滤的查询（如读 ``org.schema_migration``）必须显式走
        :meth:`_unfiltered`，那个名字在全仓 grep 一次就能审完。
        """
        if "workspace_id" not in sql:
            raise IsolationViolation(
                "repository.sql", "SQL 必须包含 workspace_id 过滤", sql.strip()[:120]
            )
        return sql

    def _unfiltered(self, sql: str) -> str:
        """显式豁免 workspace_id 检查。

        **只有**读取全局元数据（版本台账、org_config）才允许用。名字刻意难看，
        code review 时 grep ``_unfiltered`` 就能把所有豁免点一次看完。
        """
        return sql

    def _guard_rows(
        self, rows: Iterable[asyncpg.Record], *, where: str, column: str = "workspace_id"
    ) -> list[asyncpg.Record]:
        """行级二次校验。

        任何一行的 ``workspace_id`` 与本进程不一致，立即抛 ``IsolationViolation``——
        **不过滤降级**。因为那意味着上游过滤已经失效，静默丢弃只会掩盖故障。
        """
        checked: list[asyncpg.Record] = []
        for row in rows:
            if column in row.keys():
                actual = row[column]
                if actual != self.ws:
                    logger.error(
                        "cross-workspace row leaked at %s expected=%s actual=%s",
                        where,
                        self.ws,
                        actual,
                    )
                    raise IsolationViolation(where, self.ws, actual)
            checked.append(row)
        return checked

    def _guard_row(
        self, row: asyncpg.Record | None, *, where: str
    ) -> asyncpg.Record | None:
        if row is None:
            return None
        return self._guard_rows([row], where=where)[0]

    # -- 查询入口（全部经过 _bind）----------------------------------------

    async def fetch(self, sql: str, *args: Any, where: str = "repository.fetch") -> list[asyncpg.Record]:
        rows = await self.db.fetch(self._bind(sql), *args)
        return self._guard_rows(rows, where=where)

    async def fetchrow(
        self, sql: str, *args: Any, where: str = "repository.fetchrow"
    ) -> asyncpg.Record | None:
        row = await self.db.fetchrow(self._bind(sql), *args)
        return self._guard_row(row, where=where)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self.db.fetchval(self._bind(sql), *args)

    async def execute(self, sql: str, *args: Any) -> str:
        return await self.db.execute(self._bind(sql), *args)

    # -- 事务内执行：由调用方持有 conn，保证一个 step 一个 tx ------------

    async def fetch_in(
        self,
        conn: asyncpg.Connection,
        sql: str,
        *args: Any,
        where: str = "repository.fetch_in",
    ) -> list[asyncpg.Record]:
        rows = await conn.fetch(self._bind(sql), *args)
        return self._guard_rows(rows, where=where)

    async def fetchrow_in(
        self,
        conn: asyncpg.Connection,
        sql: str,
        *args: Any,
        where: str = "repository.fetchrow_in",
    ) -> asyncpg.Record | None:
        row = await conn.fetchrow(self._bind(sql), *args)
        return self._guard_row(row, where=where)

    async def execute_in(
        self, conn: asyncpg.Connection, sql: str, *args: Any
    ) -> str:
        return await conn.execute(self._bind(sql), *args)

    @staticmethod
    def labels_of(values: Sequence[str] | None) -> list[str]:
        """``text[]`` 参数的规范化：去空、去重、保持确定顺序（便于测试断言）。"""
        return sorted({v for v in (values or []) if v})


__all__ = ["Repository"]


# ---------------------------------------------------------------------------
# 位置参数风格的基类
# ---------------------------------------------------------------------------

_PRIVILEGE_ERRORS = (
    asyncpg.InsufficientPrivilegeError,
    asyncpg.UndefinedTableError,
    asyncpg.UndefinedObjectError,
)

# 连接级错误（非服务端 SQL 错误）：存储不可达，走 US1-5/US1-6 语义
_STORAGE_ERRORS = (asyncpg.InterfaceError, OSError)

_CAUGHT_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError)


def _translate(exc: BaseException) -> Exception:
    if isinstance(exc, _PRIVILEGE_ERRORS):
        return IsolationViolation(f"疑似跨业务线越权: {exc}")
    if isinstance(exc, _STORAGE_ERRORS):
        return StorageUnavailable(f"存储暂时不可达: {type(exc).__name__}: {exc}")
    return exc


class BaseRepository:
    """Control / Runtime 侧 DAO 的基类，``$1`` 固定为 ``workspace_id``。

    与上面的 :class:`Repository` 是同一条纪律的两种写法，差别只在绑定方式：
    ``Repository`` 让子类在 SQL 文本里写出 ``workspace_id`` 并做行级回读校验，
    适合要读 org schema 共享表的 Capability；``BaseRepository`` 由基类把
    ``workspace_id`` 塞进第一个位置参数，子类 SQL 从 ``$2`` 开始数，适合只碰
    自己 schema 的 Runtime / Control。

    两者共存是有意的：合并成一个类就得让一半调用方改写 SQL 的参数序号，而
    序号错位不会报错、只会静默查错行——这是最不该为了「看起来统一」去冒的险。
    """

    def __init__(self, db: Database, workspace_id: str | None = None) -> None:
        self._db = db
        # 默认取池绑定的业务线；显式传入时必须一致，防串线
        ws = workspace_id or db.workspace_id
        if ws != db.workspace_id:
            raise IsolationViolation(
                f"Repository workspace {ws!r} 与连接池 {db.workspace_id!r} 不一致"
            )
        self.workspace_id = safe_identifier(ws, where="repository.workspace")

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        async with self._db.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def short_tx(self) -> AsyncIterator[asyncpg.Connection]:
        """短事务。约束：禁止在 ``short_tx()`` 内 await 模型调用（§9.4-3）。"""
        async with self._db.tx() as conn:
            yield conn

    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        try:
            async with self.acquire() as conn:
                return await conn.fetch(sql, self.workspace_id, *args)
        except _CAUGHT_ERRORS as exc:
            raise _translate(exc) from exc

    async def fetchrow(self, sql: str, *args: Any) -> asyncpg.Record | None:
        try:
            async with self.acquire() as conn:
                return await conn.fetchrow(sql, self.workspace_id, *args)
        except _CAUGHT_ERRORS as exc:
            raise _translate(exc) from exc

    async def fetchval(self, sql: str, *args: Any) -> Any:
        try:
            async with self.acquire() as conn:
                return await conn.fetchval(sql, self.workspace_id, *args)
        except _CAUGHT_ERRORS as exc:
            raise _translate(exc) from exc

    async def execute(self, sql: str, *args: Any) -> str:
        try:
            async with self.acquire() as conn:
                return await conn.execute(sql, self.workspace_id, *args)
        except _CAUGHT_ERRORS as exc:
            raise _translate(exc) from exc
