"""事项路由 L3：语义判定（PRD A-6）。

L1（引用）/ L2（外部标识）都是确定性规则，认不出「这条需求」「刚创建的文档」
这类自然语言指代——没有 L3 时每条指代消息都新建事项，多轮对话断裂（Agent
拿到新 thread 的空记忆，表现为"忘了之前说什么"）。

判定纪律与路由层一致：**宁可新建，不错误合并**。模型无把握、答案不在候选里、
结构化调用失败，一律返回 None 交给 L4 新建。
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_SYSTEM = """你是群消息的事项归属判定器。给定一条新消息和该群的活跃事项列表，判断这条消息是否属于其中某个事项。

判定要点：
- 追问、确认、补充、指代（"这条""刚才""刚创建的"等）都算属于该事项
- 消息必须是同一件事的延续才算命中；只是话题相近不算
- 宁可判"都不属于"，也不错误合并

输出 JSON：{"topic_id": "命中事项的 topic_id 或 null"}"""


class TopicVerdict(BaseModel):
    topic_id: Optional[str] = None


class LlmTopicClassifier:
    """``TopicClassifier`` 协议的 LLM 实现：一次结构化调用判定归属。"""

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    async def classify(
        self,
        text: str,
        candidates: Sequence[Any],
        context: Any = None,
    ) -> Optional[str]:
        if not candidates:
            return None
        lines = [
            f'- topic_id={c.topic_id} 标题「{c.title}」标识 {",".join(c.ref_keys) or "无"}'
            for c in candidates
        ]
        user = "活跃事项：\n" + "\n".join(lines) + f"\n\n新消息：{text}"
        result = await self._agent.complete(TopicVerdict, system=_SYSTEM, user=user)
        if not result.ok or result.data is None:
            logger.info("L3 语义判定不可用，落 L4 新建：%s", result.reason)
            return None
        hit = result.data.topic_id
        if not hit:
            return None
        # 幻觉防护：只接受候选集合内的 topic_id
        valid = {c.topic_id for c in candidates}
        if hit not in valid:
            logger.warning("L3 判定返回候选外 topic_id=%s，按未命中处理", hit)
            return None
        return hit
