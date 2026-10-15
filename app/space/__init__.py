"""Space 模块：空间归属、配置继承、会话事项路由。

拥有 ``chat_group`` / ``topic`` / ``topic_correction`` 三张表，以及
``org.chat_ownership`` 的登记动作。
"""

from app.space.config_assembler import ConfigVersionProvider
from app.space.context_factory import Resolution, SpaceContextFactory
from app.space.ownership import ChatOwnership
from app.space.registry import ChatRegistry
from app.space.repository import ChatGroup, SpaceRepository, Topic
from app.space.resolver import (
    BotBinding,
    BotBindingRegistry,
    ScopeType,
    UnknownBotError,
)
from app.space.topic import (
    CorrectionCollector,
    RouteLevel,
    TopicLifecycle,
    TopicRoute,
    TopicRouter,
)

__all__ = [
    "BotBinding",
    "BotBindingRegistry",
    "ChatGroup",
    "ChatOwnership",
    "ChatRegistry",
    "ConfigVersionProvider",
    "CorrectionCollector",
    "Resolution",
    "RouteLevel",
    "ScopeType",
    "SpaceContextFactory",
    "SpaceRepository",
    "Topic",
    "TopicLifecycle",
    "TopicRoute",
    "TopicRouter",
    "UnknownBotError",
]
