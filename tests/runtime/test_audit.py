"""covers: TC-024

TC-024 动作审计包含追溯字段且不泄露正文凭据（US3-7）
action=[tool_call, memory_write, external_req, approval] × outcome=[成功, 失败] 关键组合
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.runtime.audit import AuditWriter, digest


@pytest.fixture()
def audit(pool):
    return AuditWriter(pool)


class TestAuditTrail:
    async def test_all_actions_leave_traceable_record(self, audit, pool):
        task_id, trace_id = uuid4(), "trace-abc"
        for action in ("tool_call", "memory_write", "external_req", "approval"):
            for outcome in ("success", "failure"):
                await audit.write(
                    action=action,
                    outcome=outcome,
                    target=f"target-{action}",
                    trace_id=trace_id,
                    task_id=task_id,
                    chat_id="chat-1",
                    topic_id="topic-1",
                    rule_version="org:1/biz:2/grp:3",
                    params={"k": "v"},
                    result={"ok": outcome == "success"},
                )
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM audit_log WHERE trace_id = $1 ORDER BY id", trace_id
            )
        assert len(rows) == 8
        for row in rows:
            # 追溯字段齐全：trace / 业务线 / 群 / 事项 / 任务 / 规则版本
            assert row["trace_id"] == trace_id
            assert row["workspace_id"] == audit.workspace_id
            assert row["chat_id"] == "chat-1"
            assert row["topic_id"] == "topic-1"
            assert row["task_id"] == task_id
            assert row["rule_version"] == "org:1/biz:2/grp:3"

    async def test_digest_truncates_and_hides_full_payload(self, audit, pool):
        secret = "sk-" + "x" * 1000  # 长凭据，不得完整落库
        await audit.write(
            action="external_req",
            outcome="failure",
            target="tapd",
            params={"token": secret, "query": "q" * 500},
            result=secret,
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM audit_log LIMIT 1")
        assert secret not in row["params_digest"]
        assert secret not in row["result_digest"]
        assert len(row["params_digest"]) <= 220
        assert len(row["result_digest"]) <= 220

    async def test_invalid_action_rejected(self, audit):
        with pytest.raises(ValueError):
            await audit.write(action="drop_table", outcome="success")


class TestDigest:
    def test_short_value_kept(self):
        assert digest("abc") == "'abc'"

    def test_long_value_truncated_with_length_marker(self):
        text = "y" * 1000
        out = digest(text)
        assert len(out) <= 220
        assert "1002 chars" in out  # repr 含引号
