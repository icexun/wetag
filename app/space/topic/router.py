"""会话事项路由 L1/L2。

同一群可并行多个事项，企微没有对应的原生字段，只能按可靠度从高到低判定：

L1 引用消息 → L2 外部标识（TAPD 编号 / 飞书链接）→ L2.5 超短应答归最近 → L3 语义判定 → L4 新建

L1/L2 是本模块的确定性规则，L3 的语义判定归 Capability（``TopicClassifier``）。
贯穿原则是**宁可新建，不错误合并**：错误合并会把两条需求的上下文搅在一起，
代价远高于多出一个事项。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from app.contracts.ids import TopicId
from app.contracts.ports import InboundRef, TopicClassifier
from app.contracts.space_context import SpaceContext
from app.space.repository import SpaceRepository, Topic
from app.space.topic.lifecycle import TopicLifecycle
from config.properties import TopicProperties

# 飞书文档：wiki / docx / docs / sheets / base 五种路径形态共用一个 token 位。
_FEISHU_DOC = re.compile(
    r"https?://[\w.-]*feishu\.cn/(wiki|docx|docs|sheets|base)/([A-Za-z0-9]{16,})"
)
# TAPD 实体链接，形如 .../prong/stories/view/1001234567890123456。
_TAPD_URL = re.compile(r"tapd\.cn/\S*?/(stories|bugs|tasks)/view/(\d+)")
# 口语写法：TAPD-1001 / tapd #1001 / 需求 1001 / 缺陷1001。
_TAPD_PLAIN = re.compile(r"(?:TAPD|tapd)[-_\s]*#?(\d{3,})")
_TAPD_CN = re.compile(r"(?:需求|故事|缺陷|bug|Bug|BUG)\s*#?(\d{4,})")

_MENTION_PREFIX = re.compile(r"^\s*(@[^\s]+\s*)+")

# 超短应答（"1"、"好的"、"确认"）：没有独立语义，一定是对上一条 bot 消息的回应。
# 走 L4 新建只会得到空记忆的孤儿 thread（表现为"忘了之前说什么"）。
_SHORT_REPLY = re.compile(
    r"^(?:[（(]?\d{1,2}[）)]?|好[的哒]?|行|可以|没问题|确认|收到|是的?|对|嗯+|ok(?:ay)?|yes|y)"
    r"[。！!~.…]*$",
    re.IGNORECASE,
)


class RouteLevel(str, Enum):
    L1_QUOTE = "L1_QUOTE"
    L2_REFERENCE = "L2_REFERENCE"
    L2_RECENT = "L2_RECENT"
    L3_SEMANTIC = "L3_SEMANTIC"
    L4_NEW = "L4_NEW"


@dataclass(frozen=True)
class TopicRoute:
    topic: Topic
    level: RouteLevel
    ref_keys: List[str] = field(default_factory=list)

    @property
    def is_new(self) -> bool:
        return self.level is RouteLevel.L4_NEW


class TopicRouter:
    def __init__(
        self,
        repository: SpaceRepository,
        classifier: Optional[TopicClassifier] = None,
    ):
        self._repo = repository
        self._classifier = classifier
        self._lifecycle = TopicLifecycle(repository)
        self._props = TopicProperties.instance()

    async def resolve(
        self, ref: InboundRef, context: Optional[SpaceContext] = None
    ) -> TopicRoute:
        ref_keys = extract_ref_keys(ref.text)

        # L1：引用了 WeTag 经手过的消息，归属最确定。
        if ref.quote_message_id:
            hit = await self._repo.find_topic_by_msg_id(
                ref.chat_id, ref.quote_message_id
            )
            if hit:
                await self._lifecycle.touch(hit, ref.event_id, ref_keys)
                return TopicRoute(hit, RouteLevel.L1_QUOTE, ref_keys)

        # L1 兜底：企微引用帧常不带被引用消息 msgid，用引用内容匹配本群 bot
        # 出站消息定位事项（用户引用的一般是 bot 的回复）。引用是渲染后的纯
        # 文本，markdown 标记被剥掉，只能按首行前缀匹配。
        if ref.quote_content:
            first_line = ref.quote_content.splitlines()[0].strip()[:40]
            if len(first_line) >= 4:
                hit = await self._repo.find_topic_by_outbound_text(
                    ref.chat_id, first_line
                )
                if hit:
                    await self._lifecycle.touch(hit, ref.event_id, ref_keys)
                    return TopicRoute(hit, RouteLevel.L1_QUOTE, ref_keys)

        # L2：命中 TAPD 编号或飞书链接。归档事项被再次明确提及时自动唤醒。
        if ref_keys:
            hit = await self._repo.find_topic_by_ref_keys(ref.chat_id, ref_keys)
            if hit:
                if hit.status != "ACTIVE":
                    await self._lifecycle.reactivate(hit)
                await self._lifecycle.touch(hit, ref.event_id, ref_keys)
                return TopicRoute(hit, RouteLevel.L2_REFERENCE, ref_keys)

        # L2.5：超短应答（"1"、"好的"）归入最近活跃事项——它不是新任务，
        # 是对该事项上一条 bot 消息的回应；新建只会得到空记忆孤儿 thread。
        if _SHORT_REPLY.match(_MENTION_PREFIX.sub("", ref.text or "").strip()):
            recent = await self._repo.list_active_topics(ref.chat_id, 1)
            if recent:
                await self._lifecycle.touch(recent[0], ref.event_id, ref_keys)
                return TopicRoute(recent[0], RouteLevel.L2_RECENT, ref_keys)

        # L3：语义判定，主责在 Capability。未接入时直接落到 L4。
        if self._classifier is not None:
            candidates = await self._repo.list_active_topics(
                ref.chat_id, self._props.max_candidates
            )
            if candidates:
                topic_id = await self._classifier.classify(
                    ref.text, candidates, context
                )
                hit = next((c for c in candidates if c.topic_id == topic_id), None)
                if hit:
                    await self._lifecycle.touch(hit, ref.event_id, ref_keys)
                    return TopicRoute(hit, RouteLevel.L3_SEMANTIC, ref_keys)

        topic = await self._repo.create_topic(
            topic_id=new_topic_id(),
            chat_id=ref.chat_id,
            title=derive_title(ref.text, self._props.title_max_length),
            ref_keys=ref_keys,
            known_msg_ids=[ref.event_id],
        )
        return TopicRoute(topic, RouteLevel.L4_NEW, ref_keys)


def extract_ref_keys(text: Optional[str]) -> List[str]:
    """从消息文本提取外部标识，归一化为 ``来源:类型:编号``。

    归一化的意义在于同一需求的链接写法与口语写法能落到同一个键上。
    """
    if not text:
        return []

    keys: List[str] = []
    for kind, token in _FEISHU_DOC.findall(text):
        keys.append(f"feishu:{kind}:{token}")
    for kind, number in _TAPD_URL.findall(text):
        keys.append(f"tapd:{kind[:-1]}:{number}")
    for pattern in (_TAPD_PLAIN, _TAPD_CN):
        for number in pattern.findall(text):
            keys.append(f"tapd:story:{number}")

    # 保序去重：靠前的标识通常是用户真正指向的那个。
    seen = set()
    unique: List[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def derive_title(text: Optional[str], max_length: int) -> str:
    """用首行生成事项名。每次回复首行会展示它，供用户核对归属是否正确。"""
    if not text:
        return "未命名事项"
    stripped = _MENTION_PREFIX.sub("", text).strip()
    first_line = next((ln.strip() for ln in stripped.splitlines() if ln.strip()), "")
    if not first_line:
        return "未命名事项"
    if len(first_line) > max_length:
        return first_line[: max_length - 1] + "…"
    return first_line


def new_topic_id() -> TopicId:
    return f"t_{uuid.uuid4().hex[:16]}"
