"""Control 仓储面：Rules 读写 + 审批 / 记忆 / 待办等台账的统一导出入口。

Rules 存储：
- 组织级 / 业务线级在 org.org_config（跨业务线共享表，角色仅 SELECT；
  写入归控制台 / 管理通道，用 admin 连接）。org_config 无 workspace_id 列，
  查询经 `_org_*` 直连（不走基类 $1 自动绑定——未使用的占位参数会让
  asyncpg 报 IndeterminateDatatypeError），权限错误仍翻译为 IsolationViolation。
- 群级在本业务线 runtime_state（群成员经 Bot 修改，工作线角色可写）。
"""
from __future__ import annotations

import json
from typing import Any

from app.control.rules.assembler import RuleLayer
from app.platform.db.pool import Pool
from app.platform.db.repository import _CAUGHT_ERRORS, _translate, BaseRepository

# 审批仓储归 control/approval/pending.py 实现，此处再导出保持「Control 仓储面」单一入口
from app.control.approval.pending import (  # noqa: E402,F401
    PendingApproval,
    PendingApprovalRepository,
)

# 记忆台账仓储归 control/memory/policy/ 实现，此处再导出（同一入口约定）
from app.control.memory.policy.conflict_mark import (  # noqa: E402,F401
    MemoryConflict,
    MemoryConflictRepository,
)
from app.control.memory.policy.pending_review import (  # noqa: E402,F401
    PendingMemory,
    PendingMemoryRepository,
)

# 待办引用台账归 control/ledger/todo_ref.py 实现，此处再导出（同一入口约定）
from app.control.ledger.todo_ref import (  # noqa: E402,F401
    TodoRef,
    TodoRefRepository,
    TodoRefStatus,
)

ORG_RULES_KEY = "rules.org"


def biz_rules_key(workspace_id: str) -> str:
    return f"rules.biz.{workspace_id}"


def group_rules_key(chat_id: str) -> str:
    return f"rules:group:{chat_id}"


class RulesRepository(BaseRepository):
    """Rules 四层中三层（组织/业务线/群）的读取面；事项层由 Space 提供者注入。"""

    def __init__(self, pool: Pool, workspace_id: str | None = None) -> None:
        super().__init__(pool, workspace_id)

    # ---- org.org_config 直连（无 workspace_id 列，不走 $1 自动绑定） ----

    async def _org_fetchval(self, sql: str, *args: Any) -> Any:
        try:
            async with self.acquire() as conn:
                return await conn.fetchval(sql, *args)
        except _CAUGHT_ERRORS as exc:
            raise _translate(exc) from exc

    async def _org_fetchrow(self, sql: str, *args: Any) -> Any:
        try:
            async with self.acquire() as conn:
                return await conn.fetchrow(sql, *args)
        except _CAUGHT_ERRORS as exc:
            raise _translate(exc) from exc

    # ---- 组织级（org.org_config，带版本号） ----

    async def org_version(self) -> int | None:
        """轻量版本探测：版本未变时装配侧不重读内容（US6-3）。"""
        return await self._org_fetchval(
            "SELECT version FROM org.org_config WHERE config_key = $1",
            ORG_RULES_KEY,
        )

    async def load_org_rules(self) -> RuleLayer | None:
        row = await self._org_fetchrow(
            "SELECT config_value, version FROM org.org_config WHERE config_key = $1",
            ORG_RULES_KEY,
        )
        if row is None:
            return None
        return RuleLayer("org", row["config_value"], int(row["version"]))

    # ---- 业务线级（org.org_config，key 含业务线名） ----

    async def biz_version(self) -> int | None:
        return await self._org_fetchval(
            "SELECT version FROM org.org_config WHERE config_key = $1",
            biz_rules_key(self.workspace_id),
        )

    async def load_biz_rules(self) -> RuleLayer | None:
        row = await self._org_fetchrow(
            "SELECT config_value, version FROM org.org_config WHERE config_key = $1",
            biz_rules_key(self.workspace_id),
        )
        if row is None:
            return None
        return RuleLayer("biz", row["config_value"], int(row["version"]))

    # ---- 群级（runtime_state，每次装配直读 —— US6-2 立即生效） ----

    async def load_group_rules(self, chat_id: str) -> RuleLayer | None:
        value = await self.fetchval(
            "SELECT state_value FROM runtime_state WHERE workspace_id = $1 AND state_key = $2",
            group_rules_key(chat_id),
        )
        if not value:
            return None
        data = json.loads(value) if isinstance(value, str) else dict(value)
        content = data.get("content")
        if content is None:
            return None
        return RuleLayer("group", str(content), int(data.get("version", 0)))

    # ---- 写入面（控制台 / 管理通道；测试亦用） ----

    async def save_org_rules(self, content: str, *, updated_by: str | None = None) -> int:
        """写组织级规则，版本单调递增；返回新版本号。需对 org_config 有写权限的连接。"""
        return int(
            await self._org_fetchval(
                """
                INSERT INTO org.org_config (config_key, config_value, version, updated_by)
                VALUES ($1, $2, 1, $3)
                ON CONFLICT (config_key) DO UPDATE
                SET config_value = $2, version = org.org_config.version + 1,
                    updated_by = $3, updated_at = now()
                RETURNING version
                """,
                ORG_RULES_KEY,
                content,
                updated_by,
            )
        )

    async def save_biz_rules(self, content: str, *, updated_by: str | None = None) -> int:
        """写业务线级规则，版本单调递增；返回新版本号。"""
        return int(
            await self._org_fetchval(
                """
                INSERT INTO org.org_config (config_key, config_value, version, updated_by)
                VALUES ($1, $2, 1, $3)
                ON CONFLICT (config_key) DO UPDATE
                SET config_value = $2, version = org.org_config.version + 1,
                    updated_by = $3, updated_at = now()
                RETURNING version
                """,
                biz_rules_key(self.workspace_id),
                content,
                updated_by,
            )
        )

    async def save_group_rules(self, chat_id: str, content: str) -> int:
        """写群级规则（群成员经 Bot 修改），版本单调递增；返回新版本号。"""
        return int(
            await self.fetchval(
                """
                INSERT INTO runtime_state (workspace_id, state_key, state_value)
                VALUES ($1, $2, jsonb_build_object('content', $3::text, 'version', 1))
                ON CONFLICT (workspace_id, state_key) DO UPDATE
                SET state_value = jsonb_build_object(
                        'content', $3::text,
                        'version', COALESCE((runtime_state.state_value->>'version')::int, 0) + 1),
                    updated_at = now()
                RETURNING (state_value->>'version')::int
                """,
                group_rules_key(chat_id),
                content,
            )
        )


__all__ = [
    "ORG_RULES_KEY",
    "MemoryConflict",
    "MemoryConflictRepository",
    "PendingApproval",
    "PendingApprovalRepository",
    "PendingMemory",
    "PendingMemoryRepository",
    "RulesRepository",
    "TodoRef",
    "TodoRefRepository",
    "TodoRefStatus",
    "biz_rules_key",
    "group_rules_key",
]
