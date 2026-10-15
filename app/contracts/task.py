"""Task 模型 + TaskKind / TriggerSource / TaskStatus 枚举（§4.3）。

两类工作负载严格区分：

* ``DeterministicJob``——零 token，固定频率跑（doc_poll / tapd_sync / repo_sync /
  todo_readback）。只做「取数 + 比对 + 落库 + 判断是否有事发生」。
* ``AgenticTask``——只有「有事发生」才由 Job 派生出来，或由用户消息触发。

Capability **不自建 Scheduler / Queue / Worker**（裁决 #20），只消费 Task、
返回 ``JobResult``。本文件因此只定义数据形状，不含任何调度逻辑。

命名上有一处需要留意：``TaskKind`` 描述的是**负载性质**（确定性 / Agentic），
与 ``task`` 表的 ``kind`` 列 CHECK 约束一一对应；而 Capability 侧「这是哪种
业务工作流」由 ``WorkflowKind`` 承担，落库时写进 ``task.job_name``。两者不是
一个维度，不要混用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from app.contracts.ids import IdemKey, TaskId, TraceId
from app.contracts.space_context import SpaceContext


class TaskKind(StrEnum):
    """负载性质。取值与 0002 的 ``kind`` CHECK 约束一致（小写）。"""

    DETERMINISTIC = "deterministic"
    AGENTIC = "agentic"


class TriggerSource(StrEnum):
    MENTION = "mention"
    SCHEDULE = "schedule"
    CARD_ACTION = "card_action"
    DERIVED = "derived"


class TaskStatus(StrEnum):
    """§4.3 状态机。``PENDING`` 是可重试的等待态（如池耗尽回退）。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAIT_CONFIRM = "WAIT_CONFIRM"  # 等审批：非失败非终止，不计 attempt
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEAD = "DEAD"  # 终止态：不再被任何重试或补偿领取


class WorkflowKind(StrEnum):
    """Agentic 工作流的业务类型。与 ``capability/router.py`` 的分派表一一对应。

    落库时写进 ``task.job_name``，因此取值即 job_name 的合法取值域之一。
    """

    REQUIREMENT_CREATE = "requirement_create"
    REQUIREMENT_UPDATE = "requirement_update"
    DOC_UPDATE = "doc_update"
    QUERY_ANSWER = "query_answer"


class JobKind(StrEnum):
    """Deterministic Job 的类型（零 token）。与 ``runtime/jobs/`` 文件名对应。"""

    DOC_POLL = "doc_poll"
    TAPD_SYNC = "tapd_sync"
    REPO_SYNC = "repo_sync"
    TODO_READBACK = "todo_readback"
    INDEX_REFRESH = "index_refresh"
    LOOP_DIGEST = "loop_digest"


# 优先级约定：数字小者优先；用户触发=1，定时=5（US3-1）
PRIORITY_USER = 1
PRIORITY_SCHEDULED = 5

# 会话任务的统一 job_name：gateway 入站（@ / 卡片动作）落 task 时使用，
# WorkerPool 的 agent 处理器也注册在这个名字下。定义在 contracts：
# Channel/Runtime（queue.py）与装配层（container.py）都只允许从这里读，
# 各写一份字面量迟早漂移成「未注册的 job」。
AGENT_JOB_NAME = "agent_reply"


class TaskSpec(BaseModel):
    """派生任务的声明形态，``JobResult.derived_tasks`` 的元素；落库时才成为 Task。"""

    job_name: str
    kind: TaskKind
    trigger_source: TriggerSource = TriggerSource.DERIVED
    parent_id: UUID | None = None
    idem_key: str | None = None
    chat_id: str | None = None
    topic_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = PRIORITY_SCHEDULED
    max_attempt: int = 3
    scheduled_at: datetime | None = None


class Task(BaseModel):
    """``task`` 表的内存形态（data-model.md「任务」）。"""

    id: UUID
    workspace_id: str
    job_name: str
    kind: TaskKind
    trigger_source: TriggerSource
    parent_id: UUID | None = None
    idem_key: str | None = None
    chat_id: str | None = None
    topic_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = PRIORITY_SCHEDULED
    status: TaskStatus = TaskStatus.PENDING
    attempt: int = 0
    max_attempt: int = 3
    progress: str | None = None
    error: str | None = None
    rule_version: str | None = None
    lease_expire_at: datetime | None = None
    scheduled_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    token_cost: int | None = None
    trace_id: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AgenticTask:
    """Capability 侧消费的执行单元：一次有 token 消耗的工作流调用。

    与 ``Task`` 的关系是「投影」——Worker 从 ``task`` 表取出 ``Task`` 后，按
    ``job_name`` 解析出 ``WorkflowKind``，连同 ``SpaceContext`` 组成本对象交给
    Capability。Capability 因此看不到 lease / attempt 这些调度字段。

    ``idem_key`` 是全局幂等纪律（§4.6-1）的载体：重试与补偿任务必须复用同一个
    key，不允许「先查后写」的应用层判重。
    """

    task_id: TaskId
    kind: WorkflowKind
    context: SpaceContext
    payload: dict[str, Any] = field(default_factory=dict)
    idem_key: IdemKey | None = None
    trace_id: TraceId | None = None
    status: TaskStatus = TaskStatus.PENDING
    attempt: int = 0
    scheduled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DeterministicJob:
    """零 token 的确定性作业。只允许「取数 + 比对 + 落库 + 判断是否有事发生」。"""

    kind: JobKind
    context: SpaceContext
    payload: dict[str, Any] = field(default_factory=dict)
    trace_id: TraceId | None = None


class SubmitResult(StrEnum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    """``task.idem_key`` 唯一索引命中。重复回调属正常现象，不告警。"""
    REJECTED = "REJECTED"


class TaskSink(Protocol):
    """入站事件的唯一去向。

    实现方为 Runtime：把事件落成 ``task`` 表一行，``idem_key`` 取事件 id
    完成去重（裁决 #6，Channel 不再自建 ``event_inbox``）。

    ``event`` 的静态类型是 Channel 的 AgentEvent，但契约不反向依赖业务模块
    （§4 import 规则），因此这里按结构约定而不标注具体类型。
    """

    async def submit(self, event, context: SpaceContext) -> SubmitResult: ...


def spec_from_agentic(task: AgenticTask) -> TaskSpec:
    """把 Capability 进程内的 ``AgenticTask`` 降为跨模块的派生任务声明。

    两者描述的是同一件事的两个阶段：``AgenticTask`` 带着 ``SpaceContext`` 和已
    分配的 task_id，是工作流手里正在跑的东西；``TaskSpec`` 是交给 Runtime 落库
    的声明——没有 id（由 DB 生成），归属拆成 chat_id / topic_id，幂等靠 idem_key。
    ``JobResult.derived_tasks`` 走的是后者，所以跨界时必须过这一道。
    """
    return TaskSpec(
        job_name=task.kind.value,
        kind=TaskKind.AGENTIC,
        trigger_source=TriggerSource.DERIVED,
        idem_key=task.idem_key,
        chat_id=task.context.chat_id,
        topic_id=task.context.topic_id,
        payload={
            **task.payload,
            # 归属穿透：每条 @机器人 的消息在 inbound 就记了发起人
            # （payload.event.actor_user_id 与 SpaceContext.actor_user_id），
            # 派生任务必须带下去，否则后续回复、预警、待办都不知道
            # 「是谁发起的」。上游已穿透的原始发起人优先——派生链上
            # context.actor 会逐跳退化成系统占位。
            "initiator_user_id": (
                task.payload.get("initiator_user_id")
                or task.context.actor_user_id
                or ""
            ),
        },
        priority=PRIORITY_USER,
        scheduled_at=task.scheduled_at,
    )


__all__ = [
    "PRIORITY_SCHEDULED",
    "PRIORITY_USER",
    "AgenticTask",
    "DeterministicJob",
    "JobKind",
    "SubmitResult",
    "Task",
    "TaskKind",
    "TaskSink",
    "TaskSpec",
    "TaskStatus",
    "TriggerSource",
    "spec_from_agentic",
    "WorkflowKind",
]
