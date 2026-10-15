"""SpaceContext：organizationId / workspaceId / botId / chatId / topicId /
actorUserId / effectiveConfigVersion（§4.2）。

贯穿全链路的归属凭证。由 Space 的 ``context_factory`` 在入口生成（唯一构造点），
Runtime 随 Task 持久化，Capability 与 Control 在入口强制校验。**不可由 Agent
参数覆盖**，所以本类是 frozen 的。
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, ConfigDict

from app.contracts.errors import IsolationViolation, SpaceMismatchError
from app.contracts.ids import (
    BotId,
    ChatId,
    OrganizationId,
    TopicId,
    UserId,
    WorkspaceId,
)


class SpaceContext(BaseModel):
    """贯穿全链路的归属凭证。

    §4.2 要求恒成立的等式，任一环不等立即拒绝执行并告警::

        event.aibotid = process.botId = workspace.botId
                      = chatGroup.botId = topic 所属 workspace 的 botId
                      = capabilityExecution.workspaceId 对应的 botId

    用 pydantic 而非 dataclass，是因为 Runtime 要把它随 ``task`` 表一行持久化，
    出入库各需一次结构化校验的序列化。
    """

    model_config = ConfigDict(frozen=True)

    organization_id: OrganizationId
    workspace_id: WorkspaceId
    bot_id: BotId
    chat_id: ChatId
    topic_id: TopicId | None = None
    actor_user_id: UserId | None = None
    # 单调递增的配置版本号，由 ConfigVersionProvider.effective_version() 给出
    effective_config_version: int | None = None

    def assert_bot(self, observed_bot_id: str, where: str) -> None:
        """校验 §4.2 等式的一环。不等即 ``SpaceMismatchError``，不做容错。"""
        if observed_bot_id != self.bot_id:
            raise SpaceMismatchError(where, self.bot_id, observed_bot_id)

    def assert_workspace(self, observed_workspace_id: str, where: str) -> None:
        """校验归属未被跨业务线串改。"""
        if observed_workspace_id != self.workspace_id:
            raise SpaceMismatchError(where, self.workspace_id, observed_workspace_id)

    def require_same_bot(self, bot_id: str) -> None:
        self.assert_bot(bot_id, "space_context.require_same_bot")

    def require_same_workspace(self, workspace_id: str) -> None:
        self.assert_workspace(workspace_id, "space_context.require_same_workspace")

    def require_consistent(self, *, event_bot_id: str, process_bot_id: str) -> None:
        """三方等式：``event.aibotid == process.botId == self.bot_id``。

        单独成方法而非两次 ``assert_bot``，是因为三方不等时无法指认哪一方是
        「期望值」——报错必须把三个值一起摊开才有排查价值。
        """
        if not (event_bot_id == process_bot_id == self.bot_id):
            raise SpaceMismatchError(
                "SpaceContext 等式不成立: "
                f"event_bot={event_bot_id!r} process_bot={process_bot_id!r} "
                f"ctx_bot={self.bot_id!r}"
            )

    def require_workspace(self, workspace_id: str) -> None:
        """任务领取处校验：上下文业务线必须等于本进程业务线。"""
        if self.workspace_id != workspace_id:
            raise IsolationViolation(
                f"workspace 不匹配: ctx={self.workspace_id!r} process={workspace_id!r}"
            )

    def log_fields(self) -> dict[str, str | None]:
        """§9.2 结构化日志的固定字段集。"""
        return {
            "organization_id": self.organization_id,
            "workspace_id": self.workspace_id,
            "chat_id": self.chat_id,
            "topic_id": self.topic_id,
        }

    def to_dict(self) -> Dict[str, Any]:
        """跨进程边界的 camelCase 形态：随 Task 落库、随事件回传。"""
        return {
            "organizationId": self.organization_id,
            "workspaceId": self.workspace_id,
            "botId": self.bot_id,
            "chatId": self.chat_id,
            "topicId": self.topic_id,
            "actorUserId": self.actor_user_id,
            "effectiveConfigVersion": self.effective_config_version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SpaceContext":
        return cls(
            organization_id=data["organizationId"],
            workspace_id=data["workspaceId"],
            bot_id=data["botId"],
            chat_id=data["chatId"],
            topic_id=data.get("topicId"),
            actor_user_id=data.get("actorUserId"),
            effective_config_version=data.get("effectiveConfigVersion"),
        )


__all__ = ["SpaceContext"]
