"""JobResult：derived_tasks + envelopes，Job 与 Worker 的唯一交界面。

Job 不直接调 Agent、不直接发消息，只在此声明派生任务与待发消息（US2-2）。

``completed=False`` 时上层**禁止**把结果渲染成「无变化 / 一切正常」，必须连同
``incomplete_reason`` 一起明示未完成（§9.3）。这条纪律是整套自动化结论能不能被
信任的前提：检测没跑成和跑完没发现，在群里长得一模一样，但含义正相反。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.contracts.envelope import Envelope
from app.contracts.task import TaskSpec


class JobResult(BaseModel):
    """一次 Job / Task 执行的结果。

    ``derived_tasks`` 是**声明**而非已落库的任务：Worker 收到后统一写 task 表，
    幂等由 ``TaskSpec.idem_key`` 保证。
    """

    derived_tasks: list[TaskSpec] = Field(default_factory=list)
    envelopes: list[Envelope] = Field(default_factory=list)
    state_patch: dict[str, Any] = Field(default_factory=dict)
    completed: bool = True
    incomplete_reason: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def incomplete(cls, reason: str, **kw: Any) -> "JobResult":
        """构造一个未完成结果。强制带 reason，杜绝静默的空结果。"""
        return cls(completed=False, incomplete_reason=reason, **kw)

    @property
    def has_effects(self) -> bool:
        return bool(self.derived_tasks or self.envelopes or self.state_patch)

    @property
    def token_cost(self) -> int:
        """确定性 Job 零 token（US2-3）；Agentic Job 由执行侧回填。"""
        return int(self.metrics.get("token_cost", 0))


__all__ = ["JobResult"]
