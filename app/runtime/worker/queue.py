"""内存优先级队列 + 启动时从 task 表恢复。

队列只是索引，事实以 DB 为准：pop 出来的任务仍须 CAS 领取成功才算数。
堆序为 (priority, scheduled_at, seq)：数字小者优先，用户触发(1)先于定时(5)（US3-1）。
"""
from __future__ import annotations

import asyncio
import heapq
import time
from itertools import count
from typing import TYPE_CHECKING
from uuid import UUID

from app.contracts.space_context import SpaceContext
from app.contracts.task import (
    AGENT_JOB_NAME,
    PRIORITY_USER,
    SubmitResult,
    Task,
    TaskKind,
    TaskSpec,
    TriggerSource,
)

if TYPE_CHECKING:
    from app.runtime.repository import TaskRepository


class InMemoryTaskSink:
    """不落库的 TaskSink，仅供冒烟脚本与 Channel 层单测使用。

    去重只看进程内的 event_id 集合，因此重启即失忆；真实投递走
    ``PersistentTaskSink``，幂等由 task 表的唯一键保证。
    """

    def __init__(self, handler=None) -> None:
        self._seen: set[str] = set()
        self._handler = handler
        self.accepted: list[tuple[object, SpaceContext]] = []

    async def submit(self, event, context: SpaceContext) -> SubmitResult:
        if event.event_id in self._seen:
            return SubmitResult.DUPLICATE
        self._seen.add(event.event_id)
        self.accepted.append((event, context))
        if self._handler is not None:
            await self._handler(event, context)
        return SubmitResult.ACCEPTED


class TaskQueue:
    """进程内优先级队列；未到 scheduled_at 的任务在 pop 时等待而非提前出队。"""

    def __init__(self) -> None:
        self._heap: list[tuple[int, float, int, Task]] = []
        self._ids: set[UUID] = set()
        self._seq = count()
        self._event = asyncio.Event()

    def __len__(self) -> int:
        return len(self._heap)

    def push(self, task: Task) -> bool:
        """入队；同 id 重复 push 返回 False（队列层去重，DB 层另有 idem_key）。"""
        if task.id in self._ids:
            return False
        heapq.heappush(
            self._heap,
            (task.priority, task.scheduled_at.timestamp(), next(self._seq), task),
        )
        self._ids.add(task.id)
        self._event.set()
        return True

    async def pop(self, timeout: float | None = None) -> Task:
        """取优先级最高且已到期的任务；无任务或均未到期则等待，超时抛 TimeoutError。

        扫描堆中全部已到期任务取堆序最优者：堆顶高优先级任务未到期
        （如重试退避）不阻塞其后已到期的任务。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            now = time.time()
            wait: float | None = None
            if self._heap:
                due_idx = self._first_due(now)
                if due_idx is not None:
                    _, _, _, task = self._remove_at(due_idx)
                    self._ids.discard(task.id)
                    return task
                wait = max(min(ts for _, ts, _, _ in self._heap) - now, 0.0)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("TaskQueue.pop 超时")
                wait = remaining if wait is None else min(wait, remaining)
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), wait)
            except TimeoutError:
                pass  # 到期重查堆；deadline 判定在循环头

    def _first_due(self, now: float) -> int | None:
        """已到期任务中堆序最优者的下标；无已到期任务则 None。"""
        best: int | None = None
        for i, entry in enumerate(self._heap):
            if entry[1] <= now and (best is None or entry[:3] < self._heap[best][:3]):
                best = i
        return best

    def _remove_at(self, idx: int) -> tuple[int, float, int, Task]:
        """删除堆中任意下标元素并保持堆序。"""
        entry = self._heap[idx]
        last = self._heap.pop()
        if idx < len(self._heap):
            self._heap[idx] = last
            heapq.heapify(self._heap)
        return entry

    async def restore_pending(self, repo: TaskRepository) -> int:
        """启动恢复：把 DB 中全部 PENDING 任务重新入队（US3-5 的重排队落点）。"""
        tasks = await repo.pending_tasks()
        for task in tasks:
            self.push(task)
        return len(tasks)


class PersistentTaskSink:
    """Channel 入站事件的唯一去向：落 ``task`` 表一行，再推进内存队列。

    实现 ``app.contracts.task.TaskSink``。Channel 不认识 Task、不认识队列，
    它只调 ``submit``——所以 Channel 与 Runtime 之间除这个 Protocol 外没有
    第二个接触面。

    去重靠 ``task.idem_key`` 的唯一索引（裁决 #6：Channel 不自建 event_inbox），
    ``TaskRepository.create`` 撞上唯一索引时返回 ``None``，在这里翻译成
    ``DUPLICATE``。企微重复回调是正常现象，不告警。
    """

    def __init__(self, repo: "TaskRepository", queue: TaskQueue) -> None:
        self._repo = repo
        self._queue = queue

    async def submit(self, event, context: SpaceContext) -> SubmitResult:
        context.require_workspace(self._repo.workspace_id)

        spec = TaskSpec(
            job_name=_JOB_NAME_BY_EVENT.get(
                getattr(event.event_type, "value", str(event.event_type)),
                AGENT_JOB_NAME,
            ),
            kind=TaskKind.AGENTIC,
            trigger_source=_TRIGGER_BY_EVENT.get(
                getattr(event.event_type, "value", str(event.event_type)),
                TriggerSource.MENTION,
            ),
            idem_key=event.event_id,
            chat_id=context.chat_id,
            topic_id=context.topic_id,
            payload={"event": _event_payload(event), "context": context.to_dict()},
            priority=PRIORITY_USER,
        )

        task = await self._repo.create(spec)
        if task is None:
            return SubmitResult.DUPLICATE
        self._queue.push(task)
        return SubmitResult.ACCEPTED


# 事件类型 → task.job_name。会话入口统一走 AGENT_JOB_NAME（WorkerPool 只在这个
# 名字下注册了 agent 处理器）；具体走哪条 Workflow 由 Agent/router 二次判定，
# 这里只给一个不会丢任务的起点。
_JOB_NAME_BY_EVENT = {
    "message": AGENT_JOB_NAME,
    "card_action": AGENT_JOB_NAME,
}

_TRIGGER_BY_EVENT = {
    "message": TriggerSource.MENTION,
    "card_action": TriggerSource.CARD_ACTION,
}


def _event_payload(event) -> dict:
    """只搬运 Worker 重建上下文所需的字段，不整体序列化 AgentEvent。

    AgentEvent 是 Channel 的内部形态（§4 import 规则不允许 Runtime 认识它），
    落库的是它的投影；多存的字段将来改 Channel 时会变成迁移负担。
    """
    payload: dict = {
        "event_id": event.event_id,
        "event_type": getattr(event.event_type, "value", str(event.event_type)),
        "bot_id": event.bot_id,
        "actor_user_id": event.actor_user_id,
        "occurred_at": event.occurred_at,
    }
    if event.message is not None:
        payload["message"] = {
            "text": event.message.text,
            "msgtype": event.message.msgtype,
            "quote_message_id": event.message.quote_message_id,
            "quote_content": event.message.quote_content,
        }
    if event.card_action is not None:
        payload["card_action"] = {
            "task_id": event.card_action.task_id,
            "event_key": event.card_action.event_key,
            "selected_items": event.card_action.selected_items,
        }
    return payload
