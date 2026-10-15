"""trace_id 传播；thread_id 命名空间分离（sched: / chat:，§9.2）。

定时任务用一次性 thread（sched:{ws}:{job}:{run_id}，用完即弃）；
会话任务用 chat:{chat_id}:{topic_id}，避免全局 memory 字典无界增长。
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


def new_trace_id() -> str:
    return uuid4().hex


def chat_thread_id(chat_id: str, topic_id: str | None = None) -> str:
    return f"chat:{chat_id}:{topic_id or 'default'}"


def sched_thread_id(workspace_id: str, job_name: str, run_id: str) -> str:
    return f"sched:{workspace_id}:{job_name}:{run_id}"


@dataclass(frozen=True)
class TraceContext:
    """一次任务执行的链路上下文，贯穿 audit_log / outbox / 告警。"""

    trace_id: str
    workspace_id: str
    chat_id: str | None = None
    topic_id: str | None = None
    task_id: str | None = None

    @classmethod
    def create(
        cls,
        workspace_id: str,
        chat_id: str | None = None,
        topic_id: str | None = None,
        task_id: str | None = None,
    ) -> "TraceContext":
        return cls(
            trace_id=new_trace_id(),
            workspace_id=workspace_id,
            chat_id=chat_id,
            topic_id=topic_id,
            task_id=task_id,
        )
