"""asyncpg 连接池（§9.4-2、§9.3）。

一进程一个池（D-2），**不是每次查询新建连接**。三条纪律在这里落成代码：

1. **会话参数**（``search_path`` 不含 public、``statement_timeout``、
   ``idle_in_transaction_session_timeout``）走 ``server_settings`` 而非 ``SET``——
   归还连接时的 ``RESET ALL`` 会把 ``SET`` 出来的值全部抹掉，只有启动包里的值扛得住。
2. **获取连接超时**（``PG_ACQUIRE_TIMEOUT_S``）即判定池耗尽，抛 ``PoolExhausted``
   让任务回 ``PENDING``，而不是无限等待把整个进程拖死。
3. **短事务**：``tx()`` 超过 ``PG_TX_SOFT_LIMIT_MS`` 打 WARN 带调用栈，用于抓
   "把整个 Workflow 包进一个事务"的违规（§9.4-3 / doc2 §A.10-1）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg

from app.contracts.errors import PoolExhausted
from app.platform.db.schema import search_path

logger = logging.getLogger(__name__)


# 会话参数**必须**走 ``server_settings``，不能在 ``init`` 钩子里 ``SET``。
#
# asyncpg 归还连接时会跑 ``RESET ALL``（``Pool.release`` → ``Connection.reset``），
# 而 ``init`` 只在建连接那一次执行。于是 ``SET`` 出来的值只对**第一次** acquire
# 有效，第二次拿到的连接是：
#
#     search_path = "$user", public        ← public 回来了
#     statement_timeout = 0                ← 没有超时了
#
# 两条都恰好是本文件存在的理由。而 ``RESET ALL`` 重置到的正是启动包里带的值，
# 所以 ``server_settings`` 反而是唯一能扛过归还的写法。
#
# 这个坑的症状极其难认：第一条查询永远是对的，第二条开始偶发地"表不存在"或
# "本该超时的语句跑了十分钟"，取决于池里哪条连接被复用。
IDLE_IN_TX_TIMEOUT_MS = 15_000


def _server_settings(sp: str, stmt_timeout_ms: int) -> dict[str, str]:
    """启动包里带的会话参数。``RESET ALL`` 会重置回这里的值，而不是库默认值。"""
    return {
        # search_path 不含 public：漏写 schema 前缀的 SQL 直接报错，不静默落到 public
        "search_path": sp,
        "statement_timeout": str(int(stmt_timeout_ms)),
        # 事务里挂着不动的连接会耗干池（长事务的最后一道兜底）
        "idle_in_transaction_session_timeout": str(IDLE_IN_TX_TIMEOUT_MS),
        "timezone": "UTC",
    }


async def _init_connection(conn: asyncpg.Connection) -> None:
    """每条新连接的初始化：只装编解码器。

    codec 是**客户端**状态，不受 ``RESET ALL`` 影响，所以留在 ``init`` 里是对的。
    会话参数一律不在这里 ``SET``——见上面那段注释。
    """
    # jsonb 直接进出 dict/list，业务层不手写 json.dumps。
    #
    # 反过来说：注册了 codec 之后，业务层**再也不能**传已经 dumps 过的字符串。
    # 那会被二次编码成一个 jsonb **标量字符串**，库里存的是 ``"[{...}]"`` 而不是
    # ``[{...}]``。读回来时 codec 一次 loads 得到 str，业务层若再 loads 一次就
    # "看起来正常"——但库端所有 jsonb 运算全废：``jsonb_array_length`` 报
    # "cannot get array length of a scalar"，``->``/``@>``/jsonb GIN 全部失效。
    #
    # ``default=str`` 兜住 datetime 一类不可序列化对象：证据和 decisions 里偶尔
    # 混进 datetime，缺了它就会在写入处抛 TypeError。
    def _encode(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    await conn.set_type_codec(
        "jsonb", encoder=_encode, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=_encode, decoder=json.loads, schema="pg_catalog"
    )


class Database:
    """业务线专属连接池。

    ``workspace_id`` 决定 ``search_path``；连接串本身用的是业务线专属角色，
    **权限层面**也不存在访问他人 schema 的可能（0007）。两层叠加。
    """

    def __init__(
        self,
        dsn: str,
        workspace_id: str,
        *,
        pool_min: int = 2,
        pool_max: int = 10,
        connect_timeout_s: float = 5.0,
        command_timeout_s: float = 15.0,
        statement_timeout_ms: int = 10_000,
        acquire_timeout_s: float = 5.0,
        tx_soft_limit_ms: int = 2_000,
    ) -> None:
        self._dsn = dsn
        self.workspace_id = workspace_id
        self._search_path = search_path(workspace_id)
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._connect_timeout_s = connect_timeout_s
        self._command_timeout_s = command_timeout_s
        self._statement_timeout_ms = statement_timeout_ms
        self._acquire_timeout_s = acquire_timeout_s
        self._tx_soft_limit_ms = tx_soft_limit_ms
        self._pool: asyncpg.Pool | None = None

    # -- 生命周期 ---------------------------------------------------------

    async def connect(self) -> None:
        if self._pool is not None:
            return

        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._pool_min,
            max_size=self._pool_max,
            timeout=self._connect_timeout_s,
            command_timeout=self._command_timeout_s,
            server_settings=_server_settings(
                self._search_path, self._statement_timeout_ms
            ),
            init=_init_connection,
        )
        logger.info(
            "pg pool ready workspace_id=%s search_path=%s size=%d..%d",
            self.workspace_id,
            self._search_path,
            self._pool_min,
            self._pool_max,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() 尚未调用")
        return self._pool

    # -- 连接获取 ---------------------------------------------------------

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        """取一条连接。

        等待超过 ``acquire_timeout_s`` 视为池耗尽：抛 ``PoolExhausted``（它是
        ``PortUnavailable`` 的子类，``retryable=True``），上层把任务回
        ``PENDING`` 稍后重试——§9.3 明确要求不能无限等待。
        """
        try:
            conn = await asyncio.wait_for(
                self.pool.acquire(), timeout=self._acquire_timeout_s
            )
        except asyncio.TimeoutError as exc:
            logger.error(
                "pg pool exhausted workspace_id=%s waited=%.1fs size=%s idle=%s",
                self.workspace_id,
                self._acquire_timeout_s,
                self.pool.get_size(),
                self.pool.get_idle_size(),
            )
            raise PoolExhausted(
                "postgres",
                "POOL_EXHAUSTED",
                f"acquire 超过 {self._acquire_timeout_s}s",
                retryable=True,
            ) from exc
        try:
            yield conn
        finally:
            await self.pool.release(conn)

    # -- 事务 -------------------------------------------------------------

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[asyncpg.Connection]:
        """一个短事务。

        **纪律**：一个 Workflow step = 一个 tx；tx 块内禁止 ``await`` LLM / CLI / HTTP。
        ``Workflow.execute`` 本身永不开 tx。超过软阈值会打 WARN 并带调用栈，
        就是为了让"把 Workflow 包进一个事务"这类违规在日志里自己现形。
        """
        started = time.monotonic()
        async with self.acquire() as conn:
            async with conn.transaction():
                yield conn
        elapsed_ms = (time.monotonic() - started) * 1000
        if elapsed_ms > self._tx_soft_limit_ms:
            logger.warning(
                "long transaction %.0fms > %dms workspace_id=%s\n%s",
                elapsed_ms,
                self._tx_soft_limit_ms,
                self.workspace_id,
                "".join(traceback.format_stack(limit=12)),
            )

    # -- 便捷查询（自动提交，不开显式事务）--------------------------------

    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        async with self.acquire() as conn:
            return await conn.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> asyncpg.Record | None:
        async with self.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        async with self.acquire() as conn:
            return await conn.execute(sql, *args)


# ``Pool`` 是 ``Database`` 的别名。Runtime 与 Control 侧按「连接池」称呼它，
# Capability 与 Space 侧按「数据库访问面」称呼它——指的是同一个对象：一条业务线
# 一个池，池本身绑定 workspace 与 search_path。保留两个名字而不强行统一，是因为
# 改注解这件事漏一处不报错、改对了也没人看得出来，收益抵不上风险。
Pool = Database


async def create_pool(
    dsn: str,
    workspace_id: str,
    *,
    min_size: int = 2,
    max_size: int = 10,
    acquire_timeout: float = 5.0,
    **kwargs: Any,
) -> Database:
    """建池并完成首次连接。测试与 ``bootstrap`` 的统一入口。"""
    db = Database(
        dsn,
        workspace_id,
        pool_min=min_size,
        pool_max=max_size,
        acquire_timeout_s=acquire_timeout,
        **kwargs,
    )
    await db.connect()
    return db


__all__ = ["Database", "Pool", "create_pool", "IDLE_IN_TX_TIMEOUT_MS"]
