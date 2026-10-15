"""covers: TC-003

TC-003 不允许改写他人已登记的群归属（US1-3）
Given: org.chat_ownership 已将群登记给 a
When: b 身份尝试修改该群归属
Then: 操作被拒绝；b 仍可读取组织级数据及首次登记未归属的群
"""
from __future__ import annotations

import asyncpg
import pytest


async def test_cross_workspace_cannot_rewrite_ownership(ws_pair):
    a, b = ws_pair
    chat_claimed = "chat-claimed-by-a"
    chat_new = "chat-new"

    admin = await asyncpg.connect(a.admin_dsn)
    try:
        # Given: 群已登记给 a
        await admin.execute(
            "INSERT INTO org.chat_ownership(chat_id, workspace_id, bot_id) "
            "VALUES($1, $2, 'bot-a') ON CONFLICT (chat_id) DO NOTHING",
            chat_claimed, a.workspace,
        )

        conn_b = await asyncpg.connect(b.role_dsn)
        try:
            # Then: b 改写归属被拒（权限错误）
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn_b.execute(
                    "UPDATE org.chat_ownership SET workspace_id = $1 WHERE chat_id = $2",
                    b.workspace, chat_claimed,
                )
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn_b.execute(
                    "DELETE FROM org.chat_ownership WHERE chat_id = $1", chat_claimed
                )

            # Then: b 仍可读取组织级数据
            owner = await conn_b.fetchval(
                "SELECT workspace_id FROM org.chat_ownership WHERE chat_id = $1",
                chat_claimed,
            )
            assert owner == a.workspace

            # Then: b 可首次登记未归属的群（INSERT-only 授权）
            await conn_b.execute(
                "INSERT INTO org.chat_ownership(chat_id, workspace_id, bot_id) "
                "VALUES($1, $2, 'bot-b')",
                chat_new, b.workspace,
            )

            # 重复登记不覆盖：ON CONFLICT DO NOTHING 后回读仍归 a
            await conn_b.execute(
                "INSERT INTO org.chat_ownership(chat_id, workspace_id, bot_id) "
                "VALUES($1, $2, 'bot-b') ON CONFLICT (chat_id) DO NOTHING",
                chat_claimed, b.workspace,
            )
            owner = await conn_b.fetchval(
                "SELECT workspace_id FROM org.chat_ownership WHERE chat_id = $1",
                chat_claimed,
            )
            assert owner == a.workspace
        finally:
            await conn_b.close()
    finally:
        await admin.execute(
            "DELETE FROM org.chat_ownership WHERE chat_id = ANY($1)",
            [chat_claimed, chat_new],
        )
        await admin.close()
