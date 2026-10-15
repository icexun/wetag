"""Space 的多业务线绑定表。

三条业务线一起声明，隔离用例才能同时拿到「本进程的 Bot」与「他人的 Bot」。
"""

from __future__ import annotations

from app.space.context_factory import SpaceContextFactory
from app.space.resolver import BotBinding, BotBindingRegistry, ScopeType
from app.space.topic.router import TopicRouter

BINDINGS = {
    "bot_alpha": BotBinding("bot_alpha", ScopeType.WORKSPACE, "alpha"),
    "bot_beta": BotBinding("bot_beta", ScopeType.WORKSPACE, "beta"),
    "bot_pub": BotBinding("bot_pub", ScopeType.ORGANIZATION, "public"),
}


def factory_for(space_repo, bot_id="bot_alpha") -> SpaceContextFactory:
    bindings = BotBindingRegistry(dict(BINDINGS), bot_id)
    return SpaceContextFactory(space_repo, bindings, TopicRouter(space_repo))
