"""covers: TC-026, TC-027, TC-028

TC-026 连续三次定时失败告警并在成功后解除（US3-9）
TC-027 文档轮询过期时禁止输出"没有变化"结论（US3-10）
TC-028 代码同步过期结论附带快照时间（US3-11）
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.platform.degradation import FreshnessTracker, last_success_key
from app.platform.observability.alerting import AlertKind
from app.runtime.state import (
    clear_failure_streak,
    fail_streak_key,
    record_failure_streak,
)

DOC_POLL_MAX_AGE = timedelta(minutes=30)  # US3-10
REPO_SYNC_MAX_AGE = timedelta(hours=24)  # US3-11


class TestFailureStreak:
    """TC-026: 第三次失败告警标红；成功后自动解除。"""

    async def test_alert_at_third_failure_then_auto_resolve(self, state_store, alerts):
        for expected in (1, 2, 3):
            streak = await record_failure_streak(
                state_store, "doc_poll", workspace_id=state_store.workspace_id
            )
            assert streak == expected
            if expected < 3:
                assert not alerts  # 前两次不告警

        failure_alerts = [a for a in alerts if a.kind == AlertKind.FAILURE]
        assert len(failure_alerts) == 1
        assert "连续 3 次失败" in failure_alerts[0].title
        # 标红状态可查询
        state = await state_store.get(fail_streak_key("doc_poll"))
        assert state["alerted"] is True and state["count"] == 3

        # 第 4 次失败不重复告警
        await record_failure_streak(state_store, "doc_poll")
        assert len([a for a in alerts if a.kind == AlertKind.FAILURE]) == 1

        # 成功后标红自动解除
        was_alerted = await clear_failure_streak(
            state_store, "doc_poll", workspace_id=state_store.workspace_id
        )
        assert was_alerted is True
        assert await state_store.get(fail_streak_key("doc_poll")) is None
        resolved = [a for a in alerts if a.fields.get("resolved")]
        assert len(resolved) == 1

    async def test_clear_without_streak_is_noop(self, state_store, alerts):
        assert await clear_failure_streak(state_store, "never_failed") is False
        assert not alerts


class TestIndexFreshness:
    """TC-027: 超过 30 分钟未成功 → 过期；再次成功后解除。"""

    async def test_stale_after_threshold_and_cleared_on_success(self, state_store):
        tracker = FreshnessTracker(state_store)

        # 从未成功：过期（不得输出「没有变化」结论）
        assert await tracker.is_index_stale("doc_poll", DOC_POLL_MAX_AGE) is True

        # 最近一次成功在 30 分钟内：新鲜
        await tracker.record_success("doc_poll")
        assert await tracker.is_index_stale("doc_poll", DOC_POLL_MAX_AGE) is False

        # 模拟 31 分钟前的成功：过期
        old = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
        await state_store.set(last_success_key("doc_poll"), {"at": old})
        assert await tracker.is_index_stale("doc_poll", DOC_POLL_MAX_AGE) is True

        # 再次成功：解除过期
        await tracker.record_success("doc_poll")
        assert await tracker.is_index_stale("doc_poll", DOC_POLL_MAX_AGE) is False

    async def test_explicit_mark_stale(self, state_store):
        tracker = FreshnessTracker(state_store)
        await tracker.record_success("doc_poll")
        await tracker.mark_stale("doc_poll", reason="手动标记")
        assert await tracker.is_index_stale("doc_poll", DOC_POLL_MAX_AGE) is True
        await tracker.record_success("doc_poll")
        assert await tracker.is_index_stale("doc_poll", DOC_POLL_MAX_AGE) is False


class TestSnapshotAge:
    """TC-028: 代码同步过期时结论须附快照时间。"""

    async def test_snapshot_age_reported(self, state_store):
        tracker = FreshnessTracker(state_store)
        assert await tracker.snapshot_age("repo_sync") is None  # 从未成功

        old = datetime.now(UTC) - timedelta(hours=25)
        await state_store.set(last_success_key("repo_sync"), {"at": old.isoformat()})

        age = await tracker.snapshot_age("repo_sync")
        assert age is not None and age >= timedelta(hours=25)
        # 超过 24 小时未成功 → 过期，结论需注明快照时间（age 即依据）
        assert await tracker.is_index_stale("repo_sync", REPO_SYNC_MAX_AGE) is True
