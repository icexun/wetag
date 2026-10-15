"""按钮回调 → card_action 触发的 Task。

卡片事件只推送一次且必须 5 秒内响应，所以这条路径的顺序是固定的：
先 CAS 抢占处理权（幂等在此收口），再把原卡片更新为受理态，最后投递任务。
"""

from __future__ import annotations

from app.contracts.ports import SpaceContextProvider
from app.contracts.task import TaskSink
from app.gateway.cards.binding import CardBinding, CardClaimResult
from app.gateway.cards.builder import build_processing_card
from app.gateway.event import AgentEvent
from app.gateway.outbound import MessageSender

try:
    from agentrun.utils.log import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)


class CardActionHandler:
    def __init__(
        self,
        binding: CardBinding,
        space: SpaceContextProvider,
        task_sink: TaskSink,
        sender: MessageSender,
        approval_repo=None,
        task_repo=None,
        task_queue=None,
    ):
        self._binding = binding
        self._space = space
        self._sink = task_sink
        self._sender = sender
        # 审批卡片的决策落库与任务重入队；缺省时审批按钮只回显不生效
        self._approvals = approval_repo
        self._tasks = task_repo
        # 运行期内存队列：审批通过重入队后直接推回执行，不等重启 restore_pending
        self._task_queue = task_queue

    async def handle(self, event: AgentEvent) -> None:
        action = event.card_action
        outcome, card = await self._binding.claim(
            task_id=action.task_id,
            bot_id=event.bot_id,
            chat_id=event.conversation.id,
            actor_user_id=event.actor_user_id,
            event_key=action.event_key,
        )

        if outcome is CardClaimResult.DUPLICATE:
            # 按钮被重复点击。外部写操作只执行一次，受理更新也不重复发。
            logger.info("Duplicate card action on %s", action.task_id)
            return
        if outcome is not CardClaimResult.CLAIMED:
            logger.warning(
                "Rejected card action %s on %s: %s",
                action.event_key,
                action.task_id,
                outcome.value,
            )
            return

        # 审批卡片：决策落库 + 原任务重入队/终止，不再派发新任务
        if card.payload.get("kind") == "approval":
            await self._decide_approval(event, card, action.event_key)
            return

        # 通知卡片只有「知道了」，回显后结束，不派生任务
        if card.payload.get("kind") == "notice":
            await self._acknowledge(event, card, action.event_key, acted_text="已收到")
            return

        await self._acknowledge(event, card, action.event_key)

        resolution = await self._space.resolve_known_topic(
            event.to_ref(), card.topic_id
        )
        await self._sink.submit(
            event.with_context(resolution.context), resolution.context
        )

    async def _decide_approval(self, event: AgentEvent, card, event_key: str) -> None:
        """审批按钮：confirm → APPROVED 并把挂起任务重入队；cancel → REJECTED 并终止任务。

        重入队后 Worker 重跑 Agent，闸门重放消费（APPROVED → CONSUMED）才真正执行
        外部写——决策与执行分离，点击本身不产生副作用。
        """
        from uuid import UUID

        payload = card.payload
        approval_id = UUID(payload["approval_id"])
        task_id = payload.get("task_id") or ""
        decided_by = event.actor_user_id
        confirmed = event_key == "confirm"

        await self._acknowledge(
            event, card, event_key,
            # 卡片原地更新只在点击回调 5 秒窗口内可用（SDK update_template_card
            # 约束），任务跑完时窗口早已关闭，终态只能另发通知——这里把话说清楚
            acted_text="已确认，执行中（完成后另行通知）" if confirmed else "已取消",
        )
        if self._approvals is None or self._tasks is None:
            logger.warning("审批卡片已回显但决策依赖未装配：%s", approval_id)
            return

        decided = await (
            self._approvals.approve(approval_id, decided_by)
            if confirmed
            else self._approvals.reject(approval_id, decided_by)
        )
        if decided is None:
            # 已决/已过期：CAS 已抢占但审批侧状态未迁移，如实记录
            logger.warning("审批 %s 已决或过期，%s 不生效", approval_id, event_key)
            return
        if not task_id:
            return
        if confirmed:
            # 打标记：重跑不再发「已受理」回执（本卡片已回显「已确认，执行中」）；
            # approval_title 供任务终态通知引用（WorkerPool 完成/失败时另发一条，
            # 原地更新卡片在点击 5 秒窗口外不可用）
            updated = await self._tasks.requeue(
                UUID(task_id), 0,
                reason="审批通过，重新入队",
                payload_patch={
                    "resumed_from_approval": True,
                    "approval_title": card.payload.get("title") or "",
                },
            )
            # 运行期直接推回内存队列执行；push 失败只记日志，重启 restore_pending 兜底
            if updated is not None and self._task_queue is not None:
                try:
                    self._task_queue.push(updated)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("审批通过任务入队失败，待重启恢复: %s", exc)
            logger.info("审批 %s 通过，任务 %s 重入队", approval_id, task_id)
        else:
            await self._tasks.mark_dead(UUID(task_id), f"审批人 {decided_by} 拒绝")
            logger.info("审批 %s 拒绝，任务 %s 终止", approval_id, task_id)

    async def _acknowledge(
        self, event: AgentEvent, card, acted_key: str, acted_text: str = "已确认，处理中"
    ) -> None:
        """5 秒内把原卡片更新为受理态。

        这是交互受理结果，不是业务最终结果；慢操作的结论稍后另发一条消息。

        更新失败不能连坐业务任务：卡片已被 CAS 认领，此时中止会让这次点击
        既没回显也没执行，且重复点击会被判为 DUPLICATE 而无法补救。
        """
        try:
            await self._sender.update_card(
                event.event_id,
                build_processing_card(
                    task_id=card.task_id,
                    title=card.payload.get("title", "已收到"),
                    acted_key=acted_key,
                    acted_text=acted_text,
                ),
            )
        except Exception as exc:
            logger.error(
                "Failed to update card %s to processing state: %s",
                card.task_id,
                exc,
                exc_info=True,
            )
