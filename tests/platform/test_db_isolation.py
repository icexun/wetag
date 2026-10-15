"""covers: TC-001, TC-092

TC-001 业务线身份只能读写自身数据且越权显式拒绝（US1-1）
TC-092 时间值一律以 UTC 存储与比较（NFR-006）
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from app.contracts.errors import IsolationViolation
from app.platform.db.pool import create_pool
from app.platform.db.repository import BaseRepository


class TestCrossWorkspaceDenied:
    """TC-001: role=[ws_a, ws_b] × target=[自身, 他方] × op=[读, 写] 全量。"""

    async def test_own_schema_read_write_ok(self, ws_pair):
        a, _ = ws_pair
        conn = await asyncpg.connect(a.role_dsn)
        try:
            await conn.execute(f"SET search_path = {a.schema}, org")
            await conn.execute(
                "INSERT INTO task(id, workspace_id, job_name, kind, trigger_source, status) "
                f"VALUES('{uuid4()}', '{a.workspace}', 'probe', 'deterministic', 'schedule', 'PENDING')"
            )
            count = await conn.fetchval("SELECT count(*) FROM task")
            assert count == 1
        finally:
            await conn.close()

    async def test_cross_read_denied(self, ws_pair):
        a, b = ws_pair
        conn = await asyncpg.connect(a.role_dsn)
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.fetchval(f"SELECT count(*) FROM {b.schema}.task")
        finally:
            await conn.close()

    async def test_cross_write_denied(self, ws_pair):
        a, b = ws_pair
        conn = await asyncpg.connect(a.role_dsn)
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    f"INSERT INTO {b.schema}.task(id, workspace_id, job_name, kind, trigger_source, status) "
                    f"VALUES('{uuid4()}', '{b.workspace}', 'x', 'deterministic', 'schedule', 'PENDING')"
                )
        finally:
            await conn.close()

    async def test_repository_maps_privilege_error_to_isolation_violation(self, ws_pair):
        """Repository 基类把权限错误翻译为 IsolationViolation（US1-2 的入口）。"""
        a, b = ws_pair
        pool = await create_pool(a.role_dsn, a.workspace, min_size=1, max_size=2)
        try:
            repo = BaseRepository(pool)
            with pytest.raises(IsolationViolation):
                await repo.fetch(
                    f"SELECT * FROM {b.schema}.task WHERE workspace_id = $1"
                )
        finally:
            await pool.close()

    async def test_repository_workspace_mismatch_rejected(self, ws_pair):
        a, _ = ws_pair
        pool = await create_pool(a.role_dsn, a.workspace, min_size=1, max_size=2)
        try:
            with pytest.raises(IsolationViolation):
                BaseRepository(pool, workspace_id="someone-else")
        finally:
            await pool.close()


class TestUtcStorage:
    """TC-092: 宿主机/会话时区非 UTC 时，存储与比较不受影响。"""

    async def test_timestamptz_roundtrip_in_utc(self, pg_schema):
        conn = await asyncpg.connect(pg_schema.admin_dsn)
        try:
            # 会话时区设为 UTC+8，模拟非 UTC 宿主
            await conn.execute("SET timezone = 'Asia/Shanghai'")
            await conn.execute(f"SET search_path = {pg_schema.schema}, org")

            instant = datetime.now(UTC).replace(microsecond=0)
            task_id = uuid4()
            await conn.execute(
                "INSERT INTO task(id, workspace_id, job_name, kind, trigger_source, status, scheduled_at) "
                "VALUES($1, $2, 'probe', 'deterministic', 'schedule', 'PENDING', $3)",
                task_id, pg_schema.workspace, instant,
            )
            got = await conn.fetchval(
                "SELECT scheduled_at FROM task WHERE id = $1", task_id
            )
            # 存储层时间值为 UTC 瞬时值，读回不受会话时区影响
            assert got.utcoffset() == timedelta(0)
            assert got == instant

            # 时间比较在 UTC 口径下进行
            is_past = await conn.fetchval(
                "SELECT scheduled_at < now() FROM task WHERE id = $1", task_id
            )
            assert is_past is True
        finally:
            await conn.close()
