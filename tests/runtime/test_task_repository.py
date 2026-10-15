"""covers: TC-018, TC-019

TC-018 四类来源统一入库并保留优先级（US3-1）
TC-019 同版本变更全生命周期只创建一次任务（US3-2）
"""
from __future__ import annotations

from uuid import uuid4

from app.contracts.task import (
    PRIORITY_SCHEDULED,
    PRIORITY_USER,
    TaskKind,
    TaskSpec,
    TaskStatus,
    TriggerSource,
)


def _spec(source: TriggerSource, **kw) -> TaskSpec:
    return TaskSpec(job_name="probe", kind=TaskKind.DETERMINISTIC, trigger_source=source, **kw)


class TestUnifiedIntake:
    """TC-018: trigger_source=[MENTION, SCHEDULE, CARD_ACTION, DERIVED] 全量。"""

    async def test_four_sources_same_shape(self, task_repo):
        parent = await task_repo.create(_spec(TriggerSource.MENTION, priority=PRIORITY_USER))
        specs = [
            _spec(TriggerSource.MENTION, chat_id="c1", topic_id="t1", priority=PRIORITY_USER),
            _spec(TriggerSource.SCHEDULE, priority=PRIORITY_SCHEDULED),
            _spec(TriggerSource.CARD_ACTION, chat_id="c1", priority=PRIORITY_USER),
            _spec(TriggerSource.DERIVED, parent_id=parent.id, chat_id="c1"),
        ]
        created = [await task_repo.create(s) for s in specs]
        assert all(t is not None for t in created)
        assert all(t.status == TaskStatus.PENDING for t in created)

        by_source = {t.trigger_source: t for t in created}
        # 来源、群、事项、优先级、父任务引用均保留
        assert by_source[TriggerSource.MENTION].chat_id == "c1"
        assert by_source[TriggerSource.MENTION].topic_id == "t1"
        assert by_source[TriggerSource.DERIVED].parent_id == parent.id
        # 用户触发优先级高于定时触发
        assert by_source[TriggerSource.MENTION].priority < by_source[TriggerSource.SCHEDULE].priority
        # 同一任务形态：四种来源落同一张表
        assert await task_repo.count_by_status(TaskStatus.PENDING) == 5


class TestIdempotency:
    """TC-019: 同 idem_key 只创建一次；新版本（新 idem_key）可再创建。"""

    async def test_same_idem_key_created_once(self, task_repo):
        key = f"doc:doc-1:v1:{uuid4()}"
        first = await task_repo.create(_spec(TriggerSource.DERIVED, idem_key=key))
        assert first is not None
        # 重复探测同版本：不再创建
        for _ in range(3):
            dup = await task_repo.create(_spec(TriggerSource.DERIVED, idem_key=key))
            assert dup is None
        assert await task_repo.count_by_status(TaskStatus.PENDING) == 1

    async def test_new_version_creates_new_task(self, task_repo):
        base = uuid4().hex
        v1 = await task_repo.create(_spec(TriggerSource.DERIVED, idem_key=f"doc:d1:v1:{base}"))
        v2 = await task_repo.create(_spec(TriggerSource.DERIVED, idem_key=f"doc:d1:v2:{base}"))
        assert v1 is not None and v2 is not None and v1.id != v2.id

    async def test_null_idem_key_never_conflicts(self, task_repo):
        a = await task_repo.create(_spec(TriggerSource.SCHEDULE))
        b = await task_repo.create(_spec(TriggerSource.SCHEDULE))
        assert a is not None and b is not None and a.id != b.id

    async def test_no_app_level_check_before_write(self, task_repo, pool):
        """去重由唯一索引保证：并发创建同键也只有一行。"""
        import asyncio

        key = f"race:{uuid4()}"
        results = await asyncio.gather(
            *(task_repo.create(_spec(TriggerSource.DERIVED, idem_key=key)) for _ in range(5))
        )
        assert sum(r is not None for r in results) == 1
