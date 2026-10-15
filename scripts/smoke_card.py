"""预警卡片冒烟脚本。

连上长连接后立刻向指定群推一张多条目预警卡片，然后等你点按钮，验证：
按钮回调能收到、``card_task`` CAS 认领、5 秒内把原卡片更新为受理态、
重复点击被幂等吞掉。

仓储仍是内存替身，不依赖 PostgreSQL。

用法::

    WECOM_BOT_ID=... WECOM_BOT_SECRET=... SMOKE_CHAT_ID=... \\
        .venv/bin/python scripts/smoke_card.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("smoke-card")

os.environ.setdefault("ORGANIZATION_ID", "default")
os.environ.setdefault("WORKSPACE_ID", "demo")

from app.contracts.ids import ChatType  # noqa: E402
from app.gateway.actions import CardActionHandler  # noqa: E402
from app.gateway.cards.binding import CardBinding, new_card_task_id  # noqa: E402
from app.gateway.cards.builder import (  # noqa: E402
    ButtonStyle,
    CardButton,
    CardItem,
    build_alert_card,
)
from app.gateway.client import WecomClient  # noqa: E402
from app.gateway.feedback import FeedbackHandler  # noqa: E402
from app.gateway.inbound import InboundDispatcher  # noqa: E402
from app.gateway.outbound import MessageSender  # noqa: E402
from app.gateway.reply_window import ReplyWindow  # noqa: E402
from app.runtime.worker.queue import InMemoryTaskSink  # noqa: E402
from app.space.context_factory import SpaceContextFactory  # noqa: E402
from app.space.registry import ChatRegistry  # noqa: E402
from app.space.resolver import BotBindingRegistry  # noqa: E402
from app.space.topic.router import TopicRouter, new_topic_id  # noqa: E402
from tests.conftest import (  # noqa: E402
    FakeCardTaskRepository,
    FakeFeedbackRepository,
    FakeSpaceRepository,
)

CHAT_ID = os.environ["SMOKE_CHAT_ID"]

BUTTONS = (
    CardButton(key="useful", text="有用", style=ButtonStyle.BLUE),
    CardButton(key="false_alarm", text="误报", style=ButtonStyle.GREY),
    CardButton(key="detail", text="查看详情", style=ButtonStyle.BLACK),
)


async def main() -> None:
    bindings = BotBindingRegistry.from_config()
    binding = bindings.current

    space_repo = FakeSpaceRepository()
    space = SpaceContextFactory(space_repo, bindings, TopicRouter(space_repo))
    chats = ChatRegistry(space_repo)
    card_repo = FakeCardTaskRepository()
    cards = CardBinding(card_repo)

    wecom = WecomClient()
    window = ReplyWindow()
    sender = MessageSender(
        client=wecom.raw,
        reply_window=window,
        chat_directory=chats,
        workspace_id=binding.workspace_id,
        bot_id=binding.bot_id,
    )

    async def on_task(event, context) -> None:
        logger.info(
            "任务入列 kind=%s chat=%s topic=%s from=%s key=%s",
            event.event_type.value,
            context.chat_id,
            context.topic_id,
            context.actor_user_id,
            event.card_action.event_key if event.card_action else "-",
        )

    sink = InMemoryTaskSink(handler=on_task)
    inbound = InboundDispatcher(
        bot_id=binding.bot_id,
        space=space,
        task_sink=sink,
        sender=sender,
        card_actions=CardActionHandler(cards, space, sink, sender),
        feedbacks=FeedbackHandler(cards, FakeFeedbackRepository()),
        reply_window=window,
    )
    wecom.subscribe(inbound.on_message, inbound.on_event)

    wecom.raw.on(
        "event", lambda f: logger.info("RAW event %s", json.dumps(f, ensure_ascii=False))
    )

    # 该群刚才已在消息冒烟里交互过，这里补登记，让主动推送通过激活校验。
    await space_repo.register_ownership(
        chat_id=CHAT_ID,
        workspace_id=binding.workspace_id,
        bot_id=binding.bot_id,
        chat_type=ChatType.GROUP,
        actor_user_id="seed",
    )
    await space_repo.upsert_chat_group(
        chat_id=CHAT_ID,
        workspace_id=binding.workspace_id,
        bot_id=binding.bot_id,
        chat_type=ChatType.GROUP,
        actor_user_id="seed",
    )
    topic = await space_repo.create_topic(
        topic_id=new_topic_id(), chat_id=CHAT_ID, title="卡片冒烟"
    )

    await wecom.start()
    await asyncio.sleep(1)

    task_id = new_card_task_id()
    card_repo.seed(
        task_id=task_id,
        chat_id=CHAT_ID,
        topic_id=topic.topic_id,
        bot_id=binding.bot_id,
        workspace_id=binding.workspace_id,
        allowed_actions=[b.key for b in BUTTONS],
        payload={"title": "需求变更通知"},
    )

    card = build_alert_card(
        task_id=task_id,
        title="需求变更通知",
        desc="本次同步发现 3 处变更，请确认是否需要处理",
        source_desc="WeTag · demo",
        items=(
            CardItem(label="TAPD-10231", value="状态：规划中 → 开发中"),
            CardItem(label="TAPD-10245", value="还款日计算口径与文档不一致"),
            CardItem(label="PRD v3.2 §4.1", value="缺少灰度回滚方案"),
        ),
        buttons=BUTTONS,
    )

    result = await sender.push_card(CHAT_ID, card)
    logger.info(
        "卡片已推送 task_id=%s ok=%s errcode=%s %s",
        task_id,
        result.ok,
        result.errcode,
        result.errmsg,
    )
    logger.info("等待点击按钮……（Ctrl-C 退出）")

    try:
        while not wecom.evicted:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await wecom.stop()
        for tid, c in card_repo.cards.items():
            logger.info(
                "卡片终态 %s status=%s acted_by=%s key=%s",
                tid,
                c.status.value,
                c.acted_by,
                c.acted_key,
            )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
