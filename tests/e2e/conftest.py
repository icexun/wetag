"""tests/e2e 共享 fixture：连接池与仓储面。"""
from __future__ import annotations

import pytest

from app.platform.db.pool import create_pool
from app.runtime.repository import OutboxRepository, TaskRepository
from app.runtime.state import RuntimeStateRepository


@pytest.fixture()
async def pool(pg_schema):
    p = await create_pool(pg_schema.admin_dsn, pg_schema.workspace, min_size=1, max_size=4)
    yield p
    await p.close()


@pytest.fixture()
def task_repo(pool):
    return TaskRepository(pool)


@pytest.fixture()
def outbox_repo(pool):
    return OutboxRepository(pool)


@pytest.fixture()
def state_store(pool):
    return RuntimeStateRepository(pool)
