"""T050 进程入口验收：无内网包、无 config-service 的本地环境可启动。

- serve() 装配容器并把 WorkerPool / Outbox 派发循环 / uvicorn HTTP 挂入同一事件循环
- HTTP 入口经 AgentRunServer.as_fastapi_app() + uvicorn.Server.serve() 协程挂载
- 取消（SIGTERM 等价）后各组件有序收尾
"""
from __future__ import annotations

import asyncio
import socket

import asyncpg
import pytest

from app.bootstrap import build_http_server, serve
from app.container import Container
from migrations.version import EXPECTED_SCHEMA_VERSION


async def _save_ledger(conn):
    return await conn.fetch("SELECT * FROM org.schema_migration")


async def _restore_ledger(conn, saved):
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


async def _set_ledger_version(conn, version: int) -> None:
    await conn.execute("DELETE FROM org.schema_migration")
    await conn.execute(
        """
        INSERT INTO org.schema_migration
               (filename, version, scope, workspace_id, checksum)
        VALUES ($1, $2, 'workspace', '', 'x')
        """,
        f"{version:04d}_test.sql", version,
    )


@pytest.fixture()
async def org_version_ready(admin_dsn, org_ready):
    """固定版本台账为期望版本，结束后恢复现场（``org.schema_migration`` 跨测试共享）。"""
    conn = await asyncpg.connect(admin_dsn)
    saved = await _save_ledger(conn)
    await _set_ledger_version(conn, EXPECTED_SCHEMA_VERSION)
    await conn.close()
    yield
    conn = await asyncpg.connect(admin_dsn)
    await _restore_ledger(conn, saved)
    await conn.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_http_ready(port: int, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            return
        except OSError:
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.1)


async def test_serve_starts_with_http_in_local_env(pg_schema, org_version_ready):
    """完整启动路径：serve 自建容器（含 jobstore 推导），HTTP 协程挂载可连通。"""
    port = _free_port()
    ready = asyncio.Event()
    task = asyncio.create_task(
        serve(
            pg_schema.workspace,
            dsn=pg_schema.admin_dsn,
            http_host="127.0.0.1",
            http_port=port,
            ready=ready,
            pool_min_size=1,
            pool_max_size=2,
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        await _wait_http_ready(port)  # uvicorn 已挂入同一事件循环并监听
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_serve_without_http_starts_and_stops(pg_schema, org_version_ready):
    """无 HTTP 的纯 Runtime 形态：worker + outbox 循环启动，取消后干净收尾。"""
    container = await Container.build(
        pg_schema.workspace, dsn=pg_schema.admin_dsn, pool_min_size=1, pool_max_size=2
    )
    ready = asyncio.Event()
    task = asyncio.create_task(
        serve(container=container, with_http=False, ready=ready)
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        assert container.scheduler._scheduler.running
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not container.scheduler._scheduler.running
        await container.close()


async def test_build_http_server_wraps_fastapi(pg_schema, org_version_ready):
    """as_fastapi_app() 取出的 FastAPI 实例被包进 uvicorn.Server（未启动）。"""
    import fastapi
    import uvicorn

    container = await Container.build(
        pg_schema.workspace, dsn=pg_schema.admin_dsn, pool_min_size=1, pool_max_size=2
    )
    try:
        server = build_http_server(container, host="127.0.0.1", port=_free_port())
        assert isinstance(server, uvicorn.Server)
        assert isinstance(server.config.app, fastapi.FastAPI)
    finally:
        await container.close()
