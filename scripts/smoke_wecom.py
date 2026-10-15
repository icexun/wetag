"""企微长连接冒烟脚本。

用真实 Bot 凭据建一次长连接，把收到的每一帧打出来，并对 @ 消息回一句。
仓储用 tests 下的内存替身，因此不依赖 PostgreSQL——本脚本验证的是
「长连接是否通、回调是否到、回复是否能发出去」，不验证落库。

用法::

    WECOM_BOT_ID=... WECOM_BOT_SECRET=... .venv/bin/python scripts/smoke_wecom.py
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
logger = logging.getLogger("smoke")

os.environ.setdefault("ORGANIZATION_ID", "default")
os.environ.setdefault("WORKSPACE_ID", "demo")

from app.gateway.actions import CardActionHandler  # noqa: E402
from app.gateway.cards.binding import CardBinding  # noqa: E402
from app.gateway.client import WecomClient  # noqa: E402
from app.gateway.feedback import FeedbackHandler  # noqa: E402
from app.gateway.inbound import InboundDispatcher  # noqa: E402
from app.gateway.outbound import MessageSender  # noqa: E402
from app.gateway.reply_window import ReplyWindow  # noqa: E402
from app.runtime.worker.queue import InMemoryTaskSink  # noqa: E402
from app.space.context_factory import SpaceContextFactory  # noqa: E402
from app.space.registry import ChatRegistry  # noqa: E402
from app.space.resolver import BotBindingRegistry  # noqa: E402
from app.space.topic.router import TopicRouter  # noqa: E402
from tests.conftest import (  # noqa: E402
    FakeCardTaskRepository,
    FakeFeedbackRepository,
    FakeSpaceRepository,
)


async def main() -> None:
    bindings = BotBindingRegistry.from_config()
    binding = bindings.current
    logger.info("bot=%s workspace=%s", binding.bot_id, binding.workspace_id)

    space_repo = FakeSpaceRepository()
    space = SpaceContextFactory(space_repo, bindings, TopicRouter(space_repo))
    chats = ChatRegistry(space_repo)
    cards = CardBinding(FakeCardTaskRepository())

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
            "任务入列 kind=%s chat=%s topic=%s from=%s text=%r",
            event.event_type.value,
            context.chat_id,
            context.topic_id,
            context.actor_user_id,
            event.message.text if event.message else "",
        )
        result = await sender.reply_stream(
            event.event_id,
            f"WeTag 长连接已通。\n- 业务线：{context.workspace_id}\n"
            f"- 会话事项：{context.topic_id}\n- 你说的是：{event.message.text}",
            finish=True,
        )
        logger.info("回复结果 ok=%s errcode=%s %s", result.ok, result.errcode, result.errmsg)

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

    # 客户端自己也订阅了这些事件，这里加一份只为把原始帧打全。
    wecom.raw.on(
        "message", lambda f: logger.info("RAW message %s", json.dumps(f, ensure_ascii=False))
    )
    wecom.raw.on(
        "event", lambda f: logger.info("RAW event %s", json.dumps(f, ensure_ascii=False))
    )

    await wecom.start()
    logger.info("已连接，等待群内 @ 消息……（Ctrl-C 退出）")

    try:
        while not wecom.evicted:
            await asyncio.sleep(1)
        logger.warning("连接被新订阅者踢掉，退出")
    except asyncio.CancelledError:
        pass
    finally:
        await wecom.stop()
        logger.info(
            "累计收到任务 %d 条，登记群 %s",
            len(sink.accepted),
            list(space_repo.chat_groups),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
