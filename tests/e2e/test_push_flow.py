"""§6.2 主动推送链路：定时扫描 → 变更检测 → 合并推送。

贯通 Phase 1~5 的组件：
SchedulerEngine.dispatch（Phase 2）→ task 表 → WorkerPool（Phase 1）
→ DocPollJob 零模型变更检测（Phase 2）→ 派生 index_refresh（Phase 1/2）
→ 变更通知 Envelope 经合并窗口聚合（Phase 3）→ 会话态 + 令牌桶 → 发送原语替身。

链路断言：
1. 调度到点只塞任务（ON CONFLICT 幂等键去重），不执行业务逻辑
2. 有变化时派生可追溯（parent_id + idem_key）的下游任务，无变化零产出
3. 同 merge_key 的多条变更通知在窗口内合并为一条，窗口不顺延
4. 到期后一条合并消息送达，payload 聚合全部条目
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.contracts.envelope import Envelope, MsgType, Priority
from app.contracts.job_result import JobResult
from app.contracts.task import TaskStatus
from app.runtime.jobs.doc_poll import DocPollJob
from app.runtime.outbox.dispatcher import Dispatcher
from app.runtime.outbox.merger import Merger
from app.runtime.outbox.session_guard import SessionGuard
from app.runtime.outbox.throttle import Throttle
from app.runtime.scheduler.engine import SchedulerEngine
from app.runtime.worker.pool import WorkerPool
from app.runtime.worker.queue import TaskQueue

CHAT_ID = "chat-e2e-push"
MERGE_KEY = f"{CHAT_ID}:doc_change"


class _FakeDocumentPort:
    """两篇文档均有新版本（相对空基线全部视为变化）。"""

    async def list_meta(self, doc_ids):
        return [{"doc_id": d, "version": "v2"} for d in doc_ids]


@pytest.fixture()
def sent():
    return []


@pytest.fixture()
def dispatcher(outbox_repo, sent):
    async def _send(msg):
        sent.append(msg)
        return f"platform-{len(sent)}"

    async def _recently_active(chat_id):
        return datetime.now(UTC)

    return Dispatcher(
        outbox_repo,
        merger=Merger(outbox_repo),  # 默认 60s 合并窗口
        guard=SessionGuard(outbox_repo, _recently_active),
        throttle=Throttle(),
        sender=_send,
    )


async def test_schedule_to_detect_to_merged_push(
    pg_schema, task_repo, outbox_repo, state_store, dispatcher, sent,
):
    ws = pg_schema.workspace
    await state_store.set("doc_poll:watch", {"doc_ids": ["d-1", "d-2"]})

    # ---- 1. 调度到点：塞任务 + 入队，同轮次重复派发被幂等键去重 ----
    queue = TaskQueue()
    engine = SchedulerEngine(ws, task_repo=task_repo, queue=queue, state=state_store)
    engine.add_job(DocPollJob(document_port=_FakeDocumentPort()))
    round_at = datetime.now(UTC).replace(second=0, microsecond=0)
    task = await engine.dispatch("doc_poll", scheduled_at=round_at)
    assert task is not None
    again = await engine.dispatch("doc_poll", scheduled_at=round_at)
    assert again is None  # 同轮次去重（US3-2）

    # ---- 2. Worker 执行 doc_poll：零模型检测出 2 篇变化，派生 2 个下游任务 ----
    async def index_handler(ctx):
        doc_id = ctx.task.payload["document_ids"][0]
        return JobResult(
            envelopes=[
                Envelope(
                    chat_id=CHAT_ID,
                    msg_type=MsgType.MARKDOWN,
                    priority=Priority.NORMAL,
                    merge_key=MERGE_KEY,
                    payload={"text": f"文档 {doc_id} 有变更"},
                )
            ]
        )

    pool = WorkerPool(
        task_repo,
        {
            "doc_poll": DocPollJob(document_port=_FakeDocumentPort()),
            "index_refresh": index_handler,
        },
        queue=queue,
        state=state_store,
        outbox_submit=dispatcher.submit,
        workers=1,
        renew_interval=3600,
    )
    done = await pool.execute(task)
    assert done is not None and done.status == TaskStatus.SUCCEEDED

    derived = [t for t in await task_repo.pending_tasks() if t.job_name == "index_refresh"]
    assert len(derived) == 2
    assert all(t.parent_id == task.id for t in derived)  # 可追溯（US2-2）
    assert all(t.idem_key for t in derived)

    # ---- 3. 执行两个下游任务：两条变更通知进同一合并窗口 ----
    for t in derived:
        result = await pool.execute(t)
        assert result is not None and result.status == TaskStatus.SUCCEEDED

    pending = await outbox_repo.due(datetime.now(UTC) + timedelta(seconds=61), limit=100)
    survivors = [m for m in pending if m.status.value == "PENDING"]
    assert len(survivors) == 1  # 合并为一条（US5-1）
    items = survivors[0].payload["items"]
    assert len(items) == 2
    assert {i["text"] for i in items} == {"文档 d-1 有变更", "文档 d-2 有变更"}

    # ---- 4. 窗口到期：会话态 + 令牌桶通过，一条合并消息送达 ----
    await dispatcher.dispatch_due(now=datetime.now(UTC) + timedelta(seconds=61))
    assert len(sent) == 1
    assert sent[0].payload["items"] == items
    assert (await outbox_repo.get(survivors[0].id)).status.value == "SENT"


async def test_schedule_no_change_produces_nothing(
    pg_schema, task_repo, outbox_repo, state_store, dispatcher, sent,
):
    """无变化：零模型开销、零派生、零出站（US2-3）。"""
    ws = pg_schema.workspace
    await state_store.set("doc_poll:watch", {"doc_ids": ["d-1"]})
    await state_store.set("doc_poll:versions", {"versions": {"d-1": "v2"}})  # 基线已是最新

    engine = SchedulerEngine(ws, task_repo=task_repo, queue=TaskQueue(), state=state_store)
    engine.add_job(DocPollJob(document_port=_FakeDocumentPort()))
    task = await engine.dispatch("doc_poll")

    pool = WorkerPool(
        task_repo,
        {"doc_poll": DocPollJob(document_port=_FakeDocumentPort())},
        state=state_store,
        outbox_submit=dispatcher.submit,
        workers=1,
        renew_interval=3600,
    )
    done = await pool.execute(task)
    assert done is not None and done.status == TaskStatus.SUCCEEDED
    assert await task_repo.pending_tasks() == []  # 无派生
    assert await outbox_repo.due(datetime.now(UTC) + timedelta(hours=1), limit=100) == []
