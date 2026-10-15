"""连接池在真 PostgreSQL 上的会话纪律（``-m pg``）。

这一条**只有真库能证明**，而它曾经是错的：asyncpg 归还连接时跑 ``RESET ALL``，
把 ``init`` 钩子里 ``SET`` 的会话参数全部抹掉。症状是第一条查询永远正确、第二条
开始偶发失灵，取决于池里哪条连接被复用——最难查的那一类。

被抹掉的两个参数恰好都是安全纪律：

* ``search_path`` 退回 ``"$user", public``——``public`` 回来了，漏写 schema
  前缀的 SQL 不再报错，而是静默落到 public。
* ``statement_timeout`` 退回 ``0``——**没有超时**。一条失控的查询能占住连接
  到天亮。

所以必须验证的不是"第一次 acquire 对不对"，而是"归还再取还对不对"。
"""

from __future__ import annotations

import pytest

from app.platform.db.pool import IDLE_IN_TX_TIMEOUT_MS, Database

pytestmark = pytest.mark.pg


async def _session(db: Database) -> dict[str, str]:
    async with db.acquire() as conn:
        return {
            k: await conn.fetchval(f"SHOW {k}")
            for k in (
                "search_path",
                "statement_timeout",
                "idle_in_transaction_session_timeout",
                "timezone",
            )
        }


async def test_session_settings_survive_being_returned_to_the_pool(
    pg_db: Database,
) -> None:
    """取 → 还 → 再取，四个会话参数必须一字不差。

    ``_session`` 自己就走完了一次完整的 acquire/release，所以第二次调用拿到的
    就是"被 RESET ALL 洗过一遍"的连接。两次结果相同才算通过。
    """
    first = await _session(pg_db)
    second = await _session(pg_db)
    third = await _session(pg_db)

    assert first == second == third, (
        "会话参数没扛过连接归还——检查是不是在 init 钩子里用 SET 而非 server_settings"
    )
    assert "public" not in second["search_path"], "public 回到了 search_path"
    assert second["statement_timeout"] not in ("0", "0ms"), "statement_timeout 被清零"
    assert second["timezone"] == "UTC"
    assert second["idle_in_transaction_session_timeout"] == (
        f"{IDLE_IN_TX_TIMEOUT_MS // 1000}s"
    )


async def test_settings_hold_on_a_connection_that_the_pool_had_to_grow(
    pg_db: Database,
) -> None:
    """并发逼池扩容，新建的连接也要带齐参数。

    ``min_size`` 之外的连接是运行时新建的。只验证第一条连接的话，扩容出来的那
    几条可能什么参数都没有——而它们只在高峰期才被用到，也就是最不该出问题的时候。
    """
    import asyncio

    results = await asyncio.gather(*(_session(pg_db) for _ in range(4)))
    assert all(r == results[0] for r in results), f"扩容出的连接参数不一致：{results}"
    assert "public" not in results[0]["search_path"]


async def test_statement_timeout_actually_fires(pg_db: Database) -> None:
    """配对正向：超时参数不只是"读回来对"，而是真的会杀语句。

    只断言 ``SHOW statement_timeout`` 的话，把值设成一个库不认的字符串也能通过。
    这里让一条必然超时的语句跑，期望被库端掐断。
    """
    import asyncpg

    db = Database(
        pg_db._dsn,  # noqa: SLF001 —— 复用同一个 DSN，只为改超时值
        pg_db.workspace_id,
        pool_min=1,
        pool_max=1,
        statement_timeout_ms=150,
    )
    await db.connect()
    try:
        # 先走一轮 acquire/release，确认超时在**被重置过**的连接上依然有效
        await _session(db)
        with pytest.raises(asyncpg.exceptions.QueryCanceledError):
            await db.fetchval("SELECT pg_sleep(3)")
    finally:
        await db.close()
